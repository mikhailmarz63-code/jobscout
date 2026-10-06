#!/usr/bin/env python3
"""The board. Four map views, a ranked queue, and the review buttons.

Localhost by default. `--lan` binds every interface so it works from a phone,
and in exchange the token stops being a write-only credential: **every** request
needs it, reads included. On localhost the operating system is the boundary; on
a network there is no boundary, and this page carries company names, salaries
and, from Hacker News, real application addresses.

No CDN, no tiles, no external anything. The world is drawn as SVG from
`world.json`, which the browser fetches once, so the Content-Security-Policy
never has to loosen.

    python3 board.py              # http://127.0.0.1:8765
    python3 board.py --lan        # the same board, from your phone
    python3 board.py --port 9000
"""
from __future__ import annotations

import argparse
import gzip
import re
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
from functools import lru_cache
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, job_description, now, session   # noqa: E402
from eligibility import SENDABLE             # noqa: E402
from sources import settings                 # noqa: E402
from world import OUT as WORLD_JSON          # noqa: E402
import ats                                   # noqa: E402
# The queries and the writes moved to views.py on 2026-09-08 so LifeHub's Jobs
# tab is the same code, not a second copy of the same SQL. This file is the HTTP
# shell over them; nothing below re-implements a query.
from views import (                          # noqa: E402
    CV_MAX_CHARS, HttpError, JOB_SELECT, SLUG_RE, answers, bundle, cv_path,
    jobs, judge_result, kit_pack, map_layers, repeated_questions, resolve_url,
    save_cv, settle, stats,
)

TOKEN_FILE = HERE / ".board-token"
# Browser-extension origins allowed to call the API cross-site: one
# `chrome-extension://<id>` per line, written by hand after "Load unpacked"
# shows the id. Gitignored like the token. Empty file or no file = none.
EXT_FILE = HERE / ".board-extension"
CERT_FILE = HERE / ".board-cert.pem"
KEY_FILE = HERE / ".board-key.pem"
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_lock = threading.Lock()


def token() -> str:
    """The bearer secret, created 0600 *atomically*.

    It used to be written and then chmod-ed, which leaves the file
    world-readable for the window between the two calls. On a shared machine
    that window is enough. os.open with the mode set means the file never
    exists with wider permissions than it should.
    """
    if not TOKEN_FILE.exists():
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    return TOKEN_FILE.read_text().strip()


def ext_origins() -> set[str]:
    try:
        return {ln.strip() for ln in EXT_FILE.read_text().splitlines()
                if ln.strip().startswith("chrome-extension://")}
    except OSError:
        return set()



LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def pair_allowed(client_ip: str, origin: str, allowed: set[str], ext_param: str = "") -> bool:
    """The one unauthenticated read: the allow-listed extension, from this
    machine, asking for the token it will use from then on.

    The extension names itself either by the Origin header (Chrome sends it)
    or by `?ext=<id>` (Brave sends no Origin from extension contexts -- found
    by echoing the request, not by reading docs). A web page can also send
    `?ext=`, but it cannot *read* the answer: the response carries a CORS
    header only for the allow-listed origin, so the browser withholds the
    body from everyone else. A process on this machine could read it -- and
    could also just read `.board-token`, so nothing new is exposed to it."""
    if client_ip not in LOOPBACK:
        return False
    if origin and origin in allowed:
        return True
    return bool(ext_param) and re.fullmatch(r"[a-p]{32}", ext_param) is not None \
        and f"chrome-extension://{ext_param}" in allowed


def ensure_cert() -> bool:
    """A self-signed certificate for --lan, generated once with openssl.

    This exists because the security review confirmed the same finding four
    times: --lan binds 0.0.0.0 and speaks plain HTTP, so the bearer token rides
    the network in clear text -- in the URL on the first load, and in a cookie
    on every request after. On a shared network that is the whole database to
    anyone listening, and this board carries salaries, company names and real
    application email addresses.

    A self-signed certificate means the browser objects once and he clicks
    through. That is a genuinely worse experience and a genuinely better
    outcome: an eavesdropper on the coffee-shop wifi sees TLS rather than a
    token. The alternative on offer was a printed warning, which stops nobody.
    """
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    names = sorted(a for a in local_addresses() if a not in ("::1", "[::1]"))
    alt = ",".join(
        (f"IP:{n}" if re.match(r"^[\d.]+$", n) else f"DNS:{n}") for n in names)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
             "-days", "365", "-subj", "/CN=jobscout",
             "-addext", f"subjectAltName={alt}"],
            check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"could not generate a certificate: {exc}", file=sys.stderr)
        return False
    KEY_FILE.chmod(0o600)
    CERT_FILE.chmod(0o600)
    return True


def local_addresses() -> set[str]:
    """Every name this machine legitimately answers to. A page that resolves
    its own domain to this LAN address still arrives with the wrong Host and is
    refused."""
    names = {"localhost", "127.0.0.1", "[::1]", "::1"}
    try:
        host = socket.gethostname()
        names.add(host)
        names.add(f"{host}.local")
        for info in socket.getaddrinfo(host, None):
            names.add(info[4][0])
    except OSError:
        pass
    return names


@lru_cache(maxsize=1)
def world_gzipped() -> bytes:
    """Compressed once and held. 200 KB becomes 67 KB, and it never changes
    between runs."""
    return gzip.compress(WORLD_JSON.read_bytes(), 6)



# --------------------------------------------------------------- server --

STATIC = HERE / "static"
CTYPES = {".html": "text/html; charset=utf-8",
          ".js": "application/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8",
          ".json": "application/json; charset=utf-8",
          ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
          ".woff2": "font/woff2", ".pdf": "application/pdf"}
COMPRESSIBLE = {".html", ".js", ".css", ".json", ".svg"}
IMMUTABLE = {".woff2", ".png", ".ico"}


@lru_cache(maxsize=64)
def _asset(path: str, mtime_ns: int, size: int, gz: bool) -> tuple[bytes, str]:
    """File bytes (optionally gzipped) and a weak ETag, cached per (path,
    mtime, size) -- so an edited file is re-read, an unchanged one is not.
    Static files used to be `read_bytes()` from disk on every request with no
    cache header at all."""
    body = Path(path).read_bytes()
    etag = 'W/"' + hashlib.blake2b(f"{mtime_ns}-{size}".encode(),
                                    digest_size=8).hexdigest() + '"'
    return (gzip.compress(body, 6) if gz else body), etag


def static_path(rel: str) -> Path | None:
    """A request path under /static/, confined to the folder. `..` segments
    survive the URL regex, so the resolved path is checked, not the string."""
    target = (STATIC / rel).resolve()
    if not target.is_relative_to(STATIC.resolve()):
        return None
    return target



class Handler(BaseHTTPRequestHandler):
    server_version = "jobscout"
    allowed_hosts: set[str] = set()

    def log_message(self, *a):        # quiet; the terminal is for the run log
        pass

    # --- plumbing ------------------------------------------------------------

    def _authorised(self) -> bool:
        """Constant-time throughout.

        Plain `==` on a secret short-circuits on the first differing byte, so
        response time leaks a prefix and the token becomes guessable one
        character at a time. Over a LAN that is a practical attack, and --lan
        is the mode this board is most exposed in.
        """
        want = token()
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("t", [""])[0]
        if supplied and secrets.compare_digest(supplied, want):
            return True
        # The browser helper: a SameSite=Strict cookie is never sent
        # cross-site, and ?t= in every URL would land in its history. A custom
        # header cannot be added by a cross-site page without a preflight this
        # server refuses for every origin but the allow-listed extension.
        header = self.headers.get("X-Jobscout-Token", "")
        if header and secrets.compare_digest(header, want):
            return True
        raw = self.headers.get("Cookie", "")
        if raw:
            cookie = SimpleCookie()
            cookie.load(raw)
            if "jobscout" in cookie:
                return secrets.compare_digest(cookie["jobscout"].value, want)
        return False

    def _host_ok(self) -> bool:
        """Checked in *every* mode, not only --lan.

        It used to return True immediately on localhost, which left the door
        open to DNS rebinding: an attacker's page whose domain resolves to
        127.0.0.1 reaches this server with its own Host header. Checking the
        header always costs nothing and closes it.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in self.allowed_hosts

    def _ext_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        return origin if origin and origin in ext_origins() else None

    def _send(self, code: int, body: bytes, ctype: str, *,
              gzipped: bool = False, set_cookie: bool = False,
              headers: dict | None = None, csp: bool = True) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if csp:
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        ext = self._ext_origin()
        if ext:
            # Only ever the one allow-listed extension origin, never *: every
            # other origin still gets no CORS header at all.
            self.send_header("Access-Control-Allow-Origin", ext)
            self.send_header("Vary", "Origin")
        if set_cookie:
            # Move the token out of the address bar, and out of the phone's
            # history, the moment the first request lands.
            # A *session* cookie: no Max-Age, so it dies with the browser.
            # It used to last a week, which meant a cookie captured once stayed
            # usable for seven days. The printed URL always carries ?t=, so
            # nothing is lost by making him re-open it.
            self.send_header("Set-Cookie",
                             "jobscout=" + token() + "; Path=/; HttpOnly; "
                             "SameSite=Strict")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, set_cookie: bool = False,
              headers: dict | None = None) -> None:
        self._send(200, json.dumps(payload, default=str).encode(),
                   "application/json; charset=utf-8", set_cookie=set_cookie,
                   headers=headers)

    def _text(self, code: int, message: str) -> None:
        self._send(code, message.encode(), "text/plain; charset=utf-8")

    def _file(self, path: Path, *, set_cookie: bool = False) -> None:
        """One static file: whitelisted by extension, confined to its folder,
        revalidated with an ETag, gzipped when the client accepts it. Fonts and
        images are immutable (their names never change); text assets are
        `no-cache`, which means "ask every time, 304 if unchanged"."""
        ext = path.suffix.lower()
        ctype = CTYPES.get(ext)
        if not ctype or not path.is_file():
            raise HttpError(404, "no")
        st = path.stat()
        wants_gz = (ext in COMPRESSIBLE and st.st_size > 1024
                    and "gzip" in self.headers.get("Accept-Encoding", ""))
        body, etag = _asset(str(path), st.st_mtime_ns, st.st_size, wants_gz)
        cache = ("public, max-age=31536000, immutable" if ext in IMMUTABLE
                 else "no-cache")
        headers = {"ETag": etag, "Cache-Control": cache, "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            self._send(304, b"", ctype, headers=headers)
            return
        self._send(200, body, ctype, gzipped=wants_gz, set_cookie=set_cookie,
                   headers=headers)

    @staticmethod
    def _int(value, what: str = "job id") -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HttpError(400, f"bad {what}") from None

    # --- dispatch ------------------------------------------------------------

    def do_OPTIONS(self):                                # noqa: N802
        """CORS preflight, answered for the allow-listed extension only.
        Every other origin gets a 403 and no Access-Control header, which is
        what makes the X-Jobscout-Token header a real CSRF defence."""
        ext = self._ext_origin()
        if not ext or not self._host_ok():
            self._text(403, "no")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", ext)
        self.send_header("Access-Control-Allow-Headers", "X-Jobscout-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                                   # noqa: N802
        self.do_GET()          # _send skips the body for HEAD

    def do_GET(self):                                    # noqa: N802
        if not self._host_ok():
            self._text(403, "wrong host")
            return
        if urlparse(self.path).path == "/api/pair":
            self.get_pair()
            return
        if not self._authorised():
            self._text(401, "add ?t=<token> (printed when the board started)")
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        for pattern, name in GET_ROUTES:
            m = pattern.match(parsed.path)
            if m:
                try:
                    getattr(self, name)(m, query)
                except HttpError as exc:
                    self._text(exc.code, exc.message)
                return
        self._text(404, "no")

    def do_POST(self):                                   # noqa: N802
        if not self._host_ok() or not self._authorised():
            self._text(403, "no")
            return
        if not self._csrf_ok():
            self._text(403, "cross-site write refused")
            return
        parsed = urlparse(self.path)
        # Bounded before the read. An unbounded Content-Length lets any client
        # that reaches this port make the process allocate until it dies.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._text(400, "bad length")
            return
        if length < 0 or length > MAX_BODY:
            self._text(413, "too large")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._text(400, "bad json")
            return
        if not isinstance(payload, dict):
            self._text(400, "expected an object")
            return
        for pattern, name in POST_ROUTES:
            m = pattern.match(parsed.path)
            if m:
                try:
                    getattr(self, name)(m, payload)
                except HttpError as exc:
                    self._text(exc.code, exc.message)
                return
        self._text(404, "no")

    def _csrf_ok(self) -> bool:
        """The write path, protected against a page in another tab.

        `SameSite=Strict` is **not** enough here, and that was the critical
        finding: same-site is scheme + registrable domain, and it deliberately
        ignores the port. Any other thing the candidate runs on localhost — a dev
        server on :3000, a notebook on :8888 — is "same site" as this board,
        so its cookie rides along on a cross-page POST.

        Two independent defences, both required:

        1. **A custom header.** A cross-origin page cannot set one on a simple
           request; adding it forces a CORS preflight, which this server never
           answers permissively, so the request never arrives.
        2. **Sec-Fetch-Site**, where the browser sends it — `same-origin` is
           the only acceptable value for a write.
        """
        supplied = self.headers.get("X-Jobscout-Token", "")
        if not supplied or not secrets.compare_digest(supplied, token()):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site in (None, "same-origin", "none"):
            return True
        # The browser helper is genuinely cross-site. It is let through only
        # when its Origin is on the allow-list -- the token alone is not
        # enough, and no other origin ever is.
        return site == "cross-site" and self._ext_origin() is not None

    # --- GET -----------------------------------------------------------------

    def get_index(self, m, query):
        self._file(STATIC / "index.html", set_cookie="t" in query)

    def get_static(self, m, query):
        target = static_path(m.group(1))
        if target is None:
            raise HttpError(404, "no")
        self._file(target)

    def get_kit(self, m, query):
        job_id = self._int(m.group(1))
        with _lock, session() as conn:
            pack = kit_pack(conn, job_id)
        if pack is None:
            self._send(404, b"{}", "application/json")
            return
        self._json(pack)

    def get_applied(self, m, query):
        import applied
        with _lock, session() as conn:
            self._json({"items": applied.pipeline(conn),
                        "summary": applied.summary(conn)})

    def get_csrf(self, m, query):
        # The double-submit half of the CSRF defence. Safe to serve: a
        # cross-origin page can *make* this request but cannot *read* the
        # response, because no CORS header is ever sent. Reaching it at all
        # already requires the cookie.
        self._json({"token": token()})

    def get_world(self, m, query):
        self._send(200, world_gzipped(), "application/json", gzipped=True,
                   headers={"Cache-Control": "no-cache"})

    def get_jobs(self, m, query):
        def num(key, default, lo, hi):
            try:
                return min(hi, max(lo, int(query.get(key, [default])[0])))
            except ValueError:
                return default
        limit = num("limit", 400, 1, 1000)
        offset = num("offset", 0, 0, 100000)
        with _lock, session() as conn:
            items, total = jobs(conn,
                                state=query.get("state", [""])[0],
                                band=query.get("band", [""])[0],
                                country=query.get("country", [""])[0],
                                reach=query.get("reach", [""])[0],
                                q=query.get("q", [""])[0][:80],
                                sort=query.get("sort", ["score"])[0],
                                limit=limit, offset=offset,
                                show_ghost=query.get("show_ghost", ["0"])[0] not in ("0", "", "false"))
        # The body stays a bare array (the client that exists reads it that
        # way); the page arithmetic rides in headers.
        self._json(items, headers={"X-Total-Count": str(total),
                                   "X-Offset": str(offset),
                                   "X-Limit": str(limit)})

    def get_map(self, m, query):
        with _lock, session() as conn:
            self._json(map_layers(conn))

    def get_stats(self, m, query):
        with _lock, session() as conn:
            self._json(stats(conn))

    def get_job(self, m, query):
        job_id = self._int(m.group(1))
        with _lock, session() as conn:
            row = conn.execute(f"{JOB_SELECT} AND j.id = ?", (job_id,)).fetchone()
            if not row:
                self._send(404, b"{}", "application/json")
                return
            detail = dict(row)
            detail["sources"] = [dict(p) for p in conn.execute(
                "SELECT source, url, location_raw, apply_url, apply_email, "
                "posted_at FROM posting WHERE job_id = ?", (job_id,))]
            detail["description"] = job_description(conn, job_id)
            detail["judge"] = judge_result(conn, job_id)
            self._json(detail)

    def get_cv_list(self, m, query):
        self._json({"variants": ats.list_variants(), "canonical": ats.CANONICAL,
                    "budget": ats.BUDGET})

    def get_cv(self, m, query):
        p = cv_path(m.group(1))
        if p is None:
            raise HttpError(404, "no such variant")
        cv = ats.load_cv(p)
        pdf = ats.default_pdf(m.group(1))
        self._json({"slug": m.group(1), "markdown": p.read_text(), "model": cv.to_dict(),
                    "plain": ats.plain_text(cv), "pdf": str(pdf) if pdf else None,
                    "pdf_url": f"/pdf/{m.group(1)}" if pdf else None,
                    "mtime": int(p.stat().st_mtime)})

    def get_pdf(self, m, query):
        slug = m.group(1)
        pdf = ats.default_pdf(slug) if SLUG_RE.match(slug) else None
        if pdf is None:
            raise HttpError(404, "no PDF rendered for that variant yet")
        # No CSP on the PDF response: the browser's viewer is not this page.
        self._send(200, pdf.read_bytes(), "application/pdf", csp=False,
                   headers={"Content-Disposition": f'inline; filename="{pdf.name}"',
                            "Cache-Control": "no-cache"})

    def get_ats(self, m, query):
        target = m.group(1)
        variant = query.get("variant", [ats.CANONICAL])[0]
        if cv_path(variant) is None:
            raise HttpError(404, "no such variant")
        want_pdf = query.get("pdf", ["1"])[0] not in ("0", "false", "")
        pdf = ats.default_pdf(variant) if want_pdf else None
        if target == "self":
            self._json(ats.self_report(variant, pdf))
            return
        job_id = self._int(target)
        with _lock, session() as conn:
            rep = ats.build(conn, job_id, variant, pdf)
        if rep is None:
            self._send(404, b"{}", "application/json")
            return
        self._json(rep)

    def get_pair(self):
        """Hands the helper its token. Loopback only, allow-listed Origin only,
        and it leaves a timestamp so `helper.py status` can say it happened."""
        ext = parse_qs(urlparse(self.path).query).get("ext", [""])[0]
        if not pair_allowed(self.client_address[0], self.headers.get("Origin", ""), ext_origins(), ext):
            self._text(403, "pairing is for the allow-listed extension, from this machine")
            return
        with _lock, session() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('extension_paired_at', ?)",
                         (str(now()),))
        self._json({"token": token(), "base": f"http://127.0.0.1:{self.server.server_address[1]}"})

    def get_resolve(self, m, query):
        url = query.get("url", [""])[0][:2000]
        with _lock, session() as conn:
            self._json(resolve_url(conn, url))

    def _answers(self, job_id: int | None, labels) -> None:
        if not isinstance(labels, list) or len(labels) > 100:
            raise HttpError(400, "labels must be a list of at most 100 fields")
        with _lock, session() as conn:
            self._json(answers(conn, job_id, labels))

    def get_answers(self, m, query):
        raw = query.get("job_id", [""])[0]
        job_id = self._int(raw) if raw.strip() else None
        try:
            labels = json.loads(query.get("labels", ["[]"])[0])
        except json.JSONDecodeError:
            raise HttpError(400, "labels must be JSON") from None
        self._answers(job_id, labels)

    def get_questions(self, m, query):
        with _lock, session() as conn:
            self._json({"items": repeated_questions(conn)})

    def get_bundle(self, m, query):
        job_id = self._int(query.get("job_id", [""])[0])
        with _lock, session() as conn:
            out = bundle(conn, job_id)
        if out is None:
            self._send(404, b"{}", "application/json")
            return
        self._json(out)

    # --- POST ----------------------------------------------------------------

    def post_applied(self, m, payload):
        import applied
        job_id = self._int(payload.get("job_id", 0))
        lane = str(payload.get("lane", "manual"))
        if lane not in applied.LANES:
            raise HttpError(400, "bad lane")
        with _lock, session() as conn:
            try:
                result = applied.mark(conn, job_id, lane,
                                      str(payload.get("note", ""))[:500])
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        self._json(result)

    def post_outcome(self, m, payload):
        import applied
        job_id = self._int(payload.get("job_id", 0))
        result = str(payload.get("result", ""))
        if result not in applied.OUTCOMES:
            raise HttpError(400, "bad outcome")
        with _lock, session() as conn:
            ok = applied.outcome(conn, job_id, result,
                                 str(payload.get("note", ""))[:500])
        self._json({"ok": ok})

    def post_review(self, m, payload):
        """One job, or a batch: `{"job_id", "state", "note"}` or
        `{"items": [{"job_id", "state", "note"}, ...]}` (≤ 200)."""
        items = payload.get("items")
        single = items is None
        if single:
            items = [payload]
        if not isinstance(items, list) or len(items) > 200:
            raise HttpError(400, "items must be a list of at most 200")
        settled, missing = [], []
        with _lock, session() as conn:
            for item in items:
                if not isinstance(item, dict):
                    raise HttpError(400, "each item must be an object")
                job_id = self._int(item.get("job_id", 0))
                state = str(item.get("state", ""))
                if state not in (*SENDABLE, "BLOCKED", "UNKNOWN"):
                    raise HttpError(400, "unknown state")
                if settle(conn, job_id, state, str(item.get("note", ""))):
                    settled.append(job_id)
                else:
                    missing.append(job_id)
        if single and missing:
            raise HttpError(404, "no such job")
        out = {"ok": True, "settled": settled, "missing": missing}
        if single:
            out.update(job_id=settled[0], state=str(items[0].get("state", "")))
        self._json(out)


    def post_answers(self, m, payload):
        raw = payload.get("job_id")
        job_id = self._int(raw) if raw not in (None, "", 0) else None
        self._answers(job_id, payload.get("labels"))

    def post_cv(self, m, payload):
        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            raise HttpError(400, "markdown must be a string")
        with _lock:
            self._json(save_cv(m.group(1), markdown))

    def post_cv_apply(self, m, payload):
        """Apply the improver's patches to the text the editor holds. Nothing
        is saved -- the result goes back into the editor for him to read."""
        if cv_path(m.group(1)) is None:
            raise HttpError(404, "no such variant")
        markdown = payload.get("markdown")
        patches = payload.get("patches")
        if not isinstance(markdown, str) or not isinstance(patches, list) or len(patches) > 50:
            raise HttpError(400, "need markdown and a list of at most 50 patches")
        if len(markdown) > CV_MAX_CHARS:
            raise HttpError(413, "too large")
        new, applied, rejected = ats.apply_patches(markdown, [p for p in patches if isinstance(p, dict)])
        self._json({"markdown": new, "applied": applied, "rejected": rejected,
                    "model": ats.parse_cv(new).to_dict()})

    def post_cv_render(self, m, payload):
        p = cv_path(m.group(1))
        if p is None:
            raise HttpError(404, "no such variant")
        sys.path.insert(0, str(HERE.parent / "builders"))
        import build_resume_from_md as builder
        with _lock:
            result = builder.build_pdf(p)
        self._json({**result, "ok": all(c["ok"] for c in result["checks"]),
                    "url": f"/pdf/{m.group(1)}"})


MAX_BODY = 64 * 1024

GET_ROUTES = [
    (re.compile(r"^/(?:index\.html)?$"), "get_index"),
    (re.compile(r"^/static/([A-Za-z0-9_][A-Za-z0-9_./-]*)$"), "get_static"),
    (re.compile(r"^/api/kit/([^/]+)$"), "get_kit"),
    (re.compile(r"^/api/applied$"), "get_applied"),
    (re.compile(r"^/api/csrf$"), "get_csrf"),
    (re.compile(r"^/api/world$"), "get_world"),
    (re.compile(r"^/api/jobs$"), "get_jobs"),
    (re.compile(r"^/api/map$"), "get_map"),
    (re.compile(r"^/api/stats$"), "get_stats"),
    (re.compile(r"^/api/job/([^/]+)$"), "get_job"),
    (re.compile(r"^/api/cv$"), "get_cv_list"),
    (re.compile(r"^/api/cv/([^/]+)$"), "get_cv"),
    (re.compile(r"^/api/ats/([^/]+)$"), "get_ats"),
    (re.compile(r"^/pdf/([^/]+)$"), "get_pdf"),
    (re.compile(r"^/api/resolve$"), "get_resolve"),
    (re.compile(r"^/api/answers$"), "get_answers"),
    (re.compile(r"^/api/bundle$"), "get_bundle"),
    (re.compile(r"^/api/questions$"), "get_questions"),
]

POST_ROUTES = [
    (re.compile(r"^/api/applied$"), "post_applied"),
    (re.compile(r"^/api/outcome$"), "post_outcome"),
    (re.compile(r"^/api/review$"), "post_review"),
    (re.compile(r"^/api/cv/([^/]+)$"), "post_cv"),
    (re.compile(r"^/api/cv/([^/]+)/apply$"), "post_cv_apply"),
    (re.compile(r"^/api/cv/([^/]+)/render$"), "post_cv_render"),
    (re.compile(r"^/api/answers$"), "post_answers"),
]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--keep-token", action="store_true",
                    help="reuse the existing token in --lan mode instead of "
                         "minting a fresh one, so an old bookmark still works.")
    ap.add_argument("--no-tls", action="store_true",
                    help="serve --lan over plain HTTP. The token then crosses "
                         "the network in clear text; only for a network you "
                         "genuinely control.")
    ap.add_argument("--standalone", action="store_true",
                    help="run this board anyway. LifeHub is the UI now; this "
                         "exists so the board stays a recoverable backup.")
    args = ap.parse_args(argv)

    # LifeHub absorbed this UI on 2026-09-08. The board is kept, working and
    # tested, because a second way in matters when the first one breaks -- but
    # two servers on one database, each with its own token and its own idea of
    # the queue, is how the two of them drift apart. Starting it is now a
    # decision, not a default.
    if not args.standalone:
        print("The board is no longer the UI. Open LifeHub instead:\n"
              "\n    http://127.0.0.1:8790  ->  the Jobs tab\n"
              "\nIt reads and writes this same jobscout.db, through the same\n"
              "queries (views.py) and the same writes (applied.py). The Chrome\n"
              "helper points at it too.\n"
              "\nTo run this board anyway -- it still works, and it is the\n"
              "backup if LifeHub is broken:\n"
              "\n    python3 board.py --standalone\n", file=sys.stderr)
        return 2

    if not WORLD_JSON.exists():
        print("world.json missing — run `python3 world.py` first", file=sys.stderr)
        return 1

    # A new secret for every --lan session, unless he asks otherwise.
    #
    # TLS stops the token being read off the wire, but it does not help if it
    # leaks some other way -- a screenshot of the URL, a phone's history, a
    # shoulder. A static token is a permanent skeleton key to the whole
    # database; one that dies when he stops the server is not.
    if args.lan and not args.keep_token:
        TOKEN_FILE.unlink(missing_ok=True)

    Handler.allowed_hosts = local_addresses()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)

    # TLS on the LAN only. On localhost the loopback interface never leaves the
    # machine, and 127.0.0.1 is already a secure context, so a certificate
    # would buy nothing but a warning to click through.
    scheme = "http"
    if args.lan and not args.no_tls:
        if ensure_cert():
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(CERT_FILE, KEY_FILE)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        else:
            print("  falling back to plain HTTP — the token will be visible "
                  "on the network\n", file=sys.stderr)

    shown = socket.gethostbyname(socket.gethostname()) if args.lan else "127.0.0.1"
    print(f"\n  {scheme}://{shown}:{args.port}/?t={token()}\n")
    if args.lan:
        print("  LAN mode. This page carries company names, salaries and real")
        print("  application addresses.")
        if scheme == "https":
            print("  Self-signed certificate: your browser will warn once. That")
            print("  warning is expected — accept it, and the token is encrypted")
            print("  from then on.\n")
        else:
            print("  NO TLS. The token crosses the network in clear text.")
            print("  Do not do this on public wifi.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
