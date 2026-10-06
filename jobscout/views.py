#!/usr/bin/env python3
"""The board's queries and writes, with no HTTP around them.

Split out of `board.py` on 2026-09-08 so LifeHub's Jobs tab and the board can
never disagree about what is in the queue. Everything here was moved verbatim;
`board.py` keeps only its handlers and calls into this module, and
`lifehub-web/lib/jobscout.py` is the second caller.

Nothing in this file opens a connection or commits -- callers pass one in, from
`db.connect()` for reads or `db.session()` for writes, so the 30s busy_timeout
and the WAL discipline stay in one place.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import now                           # noqa: E402
from eligibility import SENDABLE             # noqa: E402
from sources import settings                 # noqa: E402
from world import OUT as WORLD_JSON          # noqa: E402
import ats                                   # noqa: E402


class HttpError(Exception):
    """Raised by save_cv. board.py maps it to a status code; LifeHub maps it to
    an {"error": ...} body. Kept here so the one validator serves both."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------------ urls --

def normalise_url(url: str) -> str:
    """Lower-case host, no www., no query, no fragment, no trailing slash --
    so the URL a tab shows and the URL a feed stored compare equal."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+$", "", u.path or "")
    return f"{host}{path}"


VENDOR_ID_RES = [
    ("greenhouse", re.compile(r"greenhouse\.io/.*?/jobs/(\d+)"), "greenhouse:{}"),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?.*?token=(\d+)"), "greenhouse:{}"),
    ("ashby", re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})"), "ashby:{}"),
    ("lever", re.compile(r"lever\.co/[^/]+/([0-9a-f-]{36})"), "lever:{}"),
    ("workable", re.compile(r"workable\.com/(?:[^/]+/)?j/([A-Z0-9]+)"), "workable:{}"),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/[^/]+/(\d{9,})"), "smartrecruiters:{}"),
]


def resolve_url(conn, url: str) -> dict:
    """Which job is this page? Exact match on a stored URL first, then the
    vendor's own id out of the URL. Workday is not ingested, so it resolves to
    nothing -- honestly, with the vendor named so the panel can say why."""
    raw = url.strip()
    norm = normalise_url(raw)
    if not norm:
        return {"job_id": None, "vendor": None}
    vendor = None
    if "myworkdayjobs.com" in norm:
        vendor = "workday"
    for name, pat, fmt in VENDOR_ID_RES:
        m = pat.search(raw)
        if m:
            vendor = name
            row = conn.execute(
                "SELECT j.id, j.title, j.company FROM posting p JOIN job j ON j.id = p.job_id "
                "WHERE p.source_id = ? LIMIT 1", (fmt.format(m.group(1)),)).fetchone()
            if row:
                return _resolved(conn, row, vendor)
    like = "%" + norm.split("/", 1)[0] + "%"
    for row in conn.execute(
            "SELECT j.id, j.title, j.company, p.url, p.apply_url FROM posting p "
            "JOIN job j ON j.id = p.job_id WHERE p.url LIKE ? OR p.apply_url LIKE ? LIMIT 400",
            (like, like)):
        if norm in (normalise_url(row["url"] or ""), normalise_url(row["apply_url"] or "")):
            return _resolved(conn, row, vendor)
    return {"job_id": None, "vendor": vendor}


def _resolved(conn, row, vendor) -> dict:
    sent = conn.execute("SELECT sent_at FROM application WHERE job_id = ? AND status = 'sent'",
                        (row["id"],)).fetchone()
    return {"job_id": row["id"], "title": row["title"], "company": row["company"],
            "vendor": vendor, "applied": sent["sent_at"] if sent else None}


# --------------------------------------------------------------- queries --

JOB_SELECT = """
    SELECT j.id, j.company, j.title, j.canonical_url, j.posted_at, j.first_seen,
           j.source_count, e.state, e.rule, e.evidence_quote, e.evidence_url,
           e.decided_by, s.known pay_known, s.min_usd_month lo,
           s.max_usd_month hi, s.src_currency cur, s.src_period per,
           s.derived_by pay_from, g.country, g.country_name, g.lat, g.lon,
           g.utc_offset, g.overlap_hours, g.band, sc.total score, sc.breakdown,
           f.reach, f.reach_why, f.work_mode, f.mode_quote, f.viable, f.viable_why,
           fr.verdict verdict, fr.basis_days verdict_days
    FROM job j
    JOIN eligibility e ON e.job_id = j.id
    LEFT JOIN salary s ON s.job_id = j.id
    LEFT JOIN geo    g ON g.job_id = j.id
    LEFT JOIN score sc ON sc.job_id = j.id
    LEFT JOIN fit    f ON f.job_id = j.id
    LEFT JOIN freshness fr ON fr.job_id = j.id
    WHERE j.status = 'open'
"""


SORTS = {
    "score":   "sc.total DESC NULLS LAST",
    "pay":     "COALESCE(s.max_usd_month, s.min_usd_month) DESC NULLS LAST",
    "age":     "COALESCE(j.posted_at, j.first_seen) DESC",
    "overlap": "g.overlap_hours DESC NULLS LAST",
}


def jobs(conn, state: str = "", band: str = "", country: str = "",
         reach: str = "", q: str = "", sort: str = "score",
         limit: int = 400, offset: int = 0, show_ghost: bool = False
         ) -> tuple[list[dict], int]:
    """The ranked list, and how many rows the filters match in total -- so a
    page can say "60 of 312" instead of pretending the list ends where the
    limit does."""
    clauses, params = [], []
    # Everything except the review queue is filtered to viable jobs: he can
    # take it, and they would plausibly have him. Review deliberately is not --
    # its whole purpose is the jobs no rule could place.
    if state != "review":
        clauses.append("f.viable = 1")
    # A ghost -- open long past what its kind usually takes -- is hidden by
    # default; the board's toggle passes show_ghost=True to see them anyway. A
    # job with no freshness row yet (the stage has not run) is never hidden by
    # this, since NULL <> 'ghost' is already true.
    if not show_ghost:
        clauses.append("(fr.verdict IS NULL OR fr.verdict <> 'ghost')")
    # A job he has already applied to is finished work. Leaving it at the top
    # of tomorrow's list is how a queue stops being a queue.
    if state != "applied":
        clauses.append("j.id NOT IN (SELECT job_id FROM application "
                       "WHERE status = 'sent')")
    if reach:
        clauses.append("f.reach = ?")
        params.append(reach)
    if state == "sendable":
        clauses.append(f"e.state IN ({','.join('?' * len(SENDABLE))})")
        params += list(SENDABLE)
    elif state == "review":
        clauses.append("e.state = 'UNKNOWN'")
    elif state:
        clauses.append("e.state = ?")
        params.append(state)
    else:
        clauses.append("e.state NOT IN ('BLOCKED', 'ONSITE_NO_SPONSOR')")
    if band:
        clauses.append("g.band = ?")
        params.append(band)
    if country:
        clauses.append("g.country = ?")
        params.append(country)
    if q:
        # Free text over title and company. LIKE with its wildcards escaped,
        # so a search for "100%" means the string, not "anything".
        needle = "%" + re.sub(r"([\\%_])", r"\\\1", q.strip()) + "%"
        clauses.append(r"(j.title LIKE ? ESCAPE '\' OR j.company LIKE ? ESCAPE '\')")
        params += [needle, needle]

    where = f"{JOB_SELECT} AND {' AND '.join(clauses)}"
    total = conn.execute(f"SELECT COUNT(*) FROM ({where})", params).fetchone()[0]
    order = SORTS.get(sort, SORTS["score"])
    rows = conn.execute(
        f"{where} ORDER BY {order}, sc.total DESC NULLS LAST, j.id LIMIT ? OFFSET ?",
        [*params, limit, offset])
    return [dict(r) for r in rows], total


def settle(conn, job_id: int, state: str, note: str = "") -> bool:
    """A human settling a job the rules could not place. `decided_by='human'`
    is what stops the next run overwriting the answer. False if no such job."""
    url = conn.execute("SELECT canonical_url FROM job WHERE id = ?",
                       (job_id,)).fetchone()
    if not url:
        return False
    quote = note.strip() or "settled by hand on the board"
    conn.execute(
        "INSERT INTO eligibility (job_id, state, evidence_quote, "
        "evidence_url, rule, decided_by, confidence, decided_at) "
        "VALUES (?,?,?,?,'human','human',1.0,?) "
        "ON CONFLICT (job_id) DO UPDATE SET state=excluded.state, "
        "evidence_quote=excluded.evidence_quote, "
        "evidence_url=excluded.evidence_url, rule='human', "
        "decided_by='human', confidence=1.0, decided_at=excluded.decided_at",
        (job_id, state, quote, url["canonical_url"] or "board", now()))
    return True


def map_layers(conn) -> dict:
    """Everything the four views need, computed in one pass so switching views
    is instant and needs no round trip."""
    cfg = settings()
    mine = float(cfg["candidate"]["utc_offset"])
    world = json.loads(WORLD_JSON.read_text())["countries"]

    # 1. Timezone fit -- every country, whether or not it has a job in it. This
    # view is about the clock, not the market.
    from geo import band as band_of, overlap_hours
    green = float(cfg["hours"]["green_min_overlap"])
    amber = float(cfg["hours"]["amber_min_overlap"])
    day_start = float(cfg["hours"]["workday_start"])
    day_end = float(cfg["hours"]["workday_end"])

    # Antarctica has a real timezone and no labour market. Banding it puts a
    # continent-sized amber block along the bottom of a map about where you can
    # get work, which draws the eye and means nothing.
    timezone_layer = {}
    for iso, c in world.items():
        if c["utc_offset"] is None or iso in ("AQ", "ATA", "TF", "BV", "HM", "GS"):
            continue
        hours = overlap_hours(c["utc_offset"], mine, day_start, day_end)
        timezone_layer[iso] = {"hours": hours, "band": band_of(hours, green, amber),
                               "offset": c["utc_offset"], "multi": c["multi_zone"]}

    # 2 + 3. Pins and the hires-from-here choropleth, over jobs he can take.
    hires, pins = {}, []
    for r in conn.execute(f"""
            {JOB_SELECT} AND f.viable = 1 AND g.country IS NOT NULL"""):
        iso = r["country"]
        hires[iso] = hires.get(iso, 0) + 1
    for r in conn.execute(f"""
            {JOB_SELECT} AND f.viable = 1
              AND g.lat IS NOT NULL ORDER BY sc.total DESC NULLS LAST LIMIT 400"""):
        pins.append({"id": r["id"], "lat": r["lat"], "lon": r["lon"],
                     "company": r["company"], "title": r["title"],
                     "score": r["score"], "band": r["band"],
                     "country": r["country"]})

    # 4. Salary heat -- median, not mean. One radiologist at $83,000/month
    # would otherwise repaint a whole country.
    salary_layer = {}
    per_country: dict[str, list[float]] = {}
    for r in conn.execute("""
            SELECT g.country, COALESCE(s.max_usd_month, s.min_usd_month) pay
            FROM geo g JOIN salary s ON s.job_id = g.job_id
            JOIN job j ON j.id = g.job_id
            WHERE s.known = 1 AND g.country IS NOT NULL AND j.status = 'open'"""):
        per_country.setdefault(r["country"], []).append(r["pay"])
    for iso, values in per_country.items():
        values.sort()
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        salary_layer[iso] = {"median": round(median), "n": len(values)}

    # The blocked count per country is its own story: it is where the market
    # is, and where the door is shut.
    blocked = {}
    for r in conn.execute("""
            SELECT g.country, COUNT(*) n FROM geo g
            JOIN eligibility e ON e.job_id = g.job_id
            JOIN job j ON j.id = g.job_id
            WHERE e.state IN ('BLOCKED','ONSITE_NO_SPONSOR') AND j.status = 'open'
              AND g.country IS NOT NULL GROUP BY g.country"""):
        blocked[r["country"]] = r["n"]

    return {"timezone": timezone_layer, "hires": hires, "pins": pins,
            "salary": salary_layer, "blocked": blocked,
            "me": {"offset": mine, "country": cfg["candidate"]["country"],
                   "city": cfg["candidate"]["city"],
                   "workday": [day_start, day_end]},
            "floor": cfg["pay"]["floor_usd_month"]}


def stats(conn) -> dict:
    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    by_state = {r["state"]: r["n"] for r in conn.execute(
        "SELECT e.state, COUNT(*) n FROM eligibility e JOIN job j ON j.id = e.job_id "
        "WHERE j.status = 'open' GROUP BY e.state")}
    by_band = {r["band"]: r["n"] for r in conn.execute(
        "SELECT g.band, COUNT(*) n FROM geo g JOIN fit f ON f.job_id = g.job_id "
        "JOIN job j ON j.id = g.job_id WHERE j.status = 'open' "
        "AND f.viable = 1 AND g.band IS NOT NULL GROUP BY g.band")}
    by_reach = {r["reach"]: r["n"] for r in conn.execute(
        "SELECT f.reach, COUNT(*) n FROM fit f JOIN job j ON j.id = f.job_id "
        "WHERE j.status = 'open' AND f.viable = 1 GROUP BY f.reach")}
    by_mode = {r["work_mode"]: r["n"] for r in conn.execute(
        "SELECT f.work_mode, COUNT(*) n FROM fit f JOIN job j ON j.id = f.job_id "
        "WHERE j.status = 'open' GROUP BY f.work_mode")}
    dropped = {r["viable_why"]: r["n"] for r in conn.execute(
        "SELECT f.viable_why, COUNT(*) n FROM fit f JOIN job j ON j.id = f.job_id "
        "WHERE j.status = 'open' AND f.viable = 0 GROUP BY f.viable_why "
        "ORDER BY n DESC")}
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM posting GROUP BY source ORDER BY n DESC")}
    day_ago = now() - 86400
    # On a database whose oldest row is itself younger than a day, "new since
    # yesterday" is every row, and reporting 79 of 79 as new is a lie dressed
    # as a metric. Say it is the first run instead.
    oldest = conn.execute("SELECT MIN(first_seen) t FROM job").fetchone()["t"]
    first_run = not oldest or oldest >= day_ago
    fresh = conn.execute(
        "SELECT COUNT(*) c FROM fit f JOIN job j ON j.id = f.job_id "
        "WHERE f.viable = 1 AND j.status = 'open' AND j.first_seen >= ?",
        (day_ago,)).fetchone()["c"]
    top, _ = jobs(conn, limit=5)

    return {
        "jobs": one("SELECT COUNT(*) FROM job WHERE status = 'open'"),
        "fresh": 0 if first_run else fresh,
        "first_run": first_run,
        "top": top,
        "postings": one("SELECT COUNT(*) FROM posting"),
        "priced": one("SELECT COUNT(*) FROM salary WHERE known = 1"),
        "reachable": conn.execute(
            "SELECT COUNT(*) c FROM fit f JOIN job j ON j.id = f.job_id "
            "WHERE j.status = 'open' AND f.viable = 1 AND j.id NOT IN "
            "(SELECT job_id FROM application WHERE status = 'sent')").fetchone()["c"],
        "applied": conn.execute(
            "SELECT COUNT(*) c FROM application WHERE status = 'sent'").fetchone()["c"],
        "followups": conn.execute(
            "SELECT COUNT(*) c FROM application WHERE status = 'sent' "
            "AND outcome IS NULL AND sent_at <= ?",
            (now() - 7 * 86400,)).fetchone()["c"],
        "at_level": by_reach.get("likely", 0),
        "review": by_state.get("UNKNOWN", 0),
        "by_state": by_state, "by_band": by_band, "by_source": by_source,
        "by_reach": by_reach, "by_mode": by_mode, "dropped": dropped,
        "floor": settings()["pay"]["floor_usd_month"],
    }


# -------------------------------------------------------------------- cv --

RESUME_DIR = ats.RESUME
BACKUPS = RESUME_DIR / ".backups"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$", re.I)
CV_MAX_CHARS = 40_000
KEEP_BACKUPS = 30


def cv_path(slug: str) -> Path | None:
    """resume/resume-<slug>.md, confined to resume/ and existing."""
    if not SLUG_RE.match(slug or ""):
        return None
    p = ats.variant_path(slug)
    if p.resolve().parent != RESUME_DIR.resolve() or not p.is_file():
        return None
    return p


def save_cv(slug: str, markdown: str) -> dict:
    """Validate, back up, write atomically. The board is the only thing that
    writes into resume/, and it never writes something that does not parse as
    a CV -- a blank editor saved by accident would otherwise erase the file."""
    p = cv_path(slug)
    if p is None:
        raise HttpError(404, "no such variant")
    if len(markdown) > CV_MAX_CHARS:
        raise HttpError(413, f"more than {CV_MAX_CHARS} characters")
    cv = ats.parse_cv(markdown, str(p))
    if not cv.name or len(cv.sections) < 3 or not cv.experience():
        raise HttpError(400, "does not parse as a CV (name, three sections, one role)")
    BACKUPS.mkdir(exist_ok=True)
    stamp = now()
    backup = BACKUPS / f"resume-{slug}.{stamp}.md"
    backup.write_text(p.read_text())
    old = sorted(BACKUPS.glob(f"resume-{slug}.*.md"))
    for stale in old[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(markdown if markdown.endswith("\n") else markdown + "\n")
    os.replace(tmp, p)
    return {"ok": True, "backup": str(backup), "model": cv.to_dict(),
            "lint": ats.lint(cv)}


# ------------------------------------------------------------- kit + forms --
# The application pack, and the answers the browser helper fills a form with.
# Extracted from board.py's handlers on 2026-09-08 so LifeHub's extension routes
# and the board return byte-for-byte the same shapes. Returns None where the
# handlers returned 404, so each caller maps that to its own status code.


def kit_pack(conn, job_id: int) -> dict | None:
    import kit
    pack = kit.build(conn, job_id)
    if not pack:
        return None
    return {
        # The disqualifiers first. kit.build() has always returned them;
        # the board used to drop them on the floor.
        "blockers": list(pack.get("blockers") or []),
        "flags": list(pack.get("flags") or []),
        "sections": [{"heading": h, "body": b} for h, b in pack["sections"] if b],
        "stories": [{
            "title": st["title"],
            "body": (f"Situation: {st['s']}\n\nTask: {st['t']}\n\n"
                     f"Action: {st['a']}\n\nResult: {st['r']}"
                     + (f"\n\nThe point to land: {st['point']}" if st["point"] else "")),
        } for st in pack["stories"]],
        "defences": [{"heading": q, "body": a} for q, a in pack["defences"]],
        "markdown": kit.render(pack),
    }


def answers(conn, job_id: int | None, fields: list) -> dict:
    """Not found, or no job_id at all: identity, CTC, story and project matching
    still apply even to a job jobscout has never seen. Only a genuinely
    unreachable board should leave a field blank -- not "the board is fine but
    this posting isn't tracked"."""
    import kit
    fields = [f for f in fields if isinstance(f, dict)]
    out = kit.answers_for_labels(conn, job_id, fields) if job_id else None
    return out or kit.answers_for_labels_generic(fields)


def repeated_questions(conn, limit: int = 200) -> list[dict]:
    """J4: every form question on file, grouped by normalised text -- how many
    times it has been seen, whether kit.py has an answer for it, and whether
    it is one of the guarded kinds (self-identification, work authorisation or
    sponsorship, a salary figure) that must always be his to answer.
    Unanswered first: that is the list worth clearing once."""
    import kit
    rows = conn.execute("SELECT label, required FROM form_question").fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        key = re.sub(r"\s+", " ", (r["label"] or "").strip().lower())[:200]
        if not key:
            continue
        g = groups.setdefault(key, {"label": r["label"], "count": 0, "required": 0})
        g["count"] += 1
        g["required"] += 1 if r["required"] else 0
    if not groups:
        return []

    # One call over every distinct label, reusing the exact routing and
    # guard rules the extension itself is answered with -- a second copy of
    # that logic here could only ever drift out of agreement with it.
    answered = kit.answers_for_labels_generic([{"label": g["label"]} for g in groups.values()])
    by_label = {a["label"]: a for a in answered.get("answers", [])}

    out = []
    for g in groups.values():
        a = by_label.get(g["label"], {})
        out.append({
            "label": g["label"], "count": g["count"], "required": g["required"],
            "kind": a.get("kind"), "answered": bool(a) and not a.get("needs_you"),
            "guarded": a.get("kind") in kit.GUARD_KINDS,
            "answer": a.get("answer") or "",
        })
    out.sort(key=lambda x: (x["answered"], -x["count"]))
    return out[:limit]


def judge_result(conn, job_id: int) -> dict | None:
    """J3's cached ratings for one job, whatever CV hash produced them --
    the board shows the most recent read, not a re-run of judge.py's own
    per-CV-hash caching rule (that rule lives in judge.py; this is a read)."""
    row = conn.execute(
        "SELECT reasoning, choice, called_at FROM agent_call WHERE job_id = ? "
        "AND purpose LIKE 'fit_judge:%' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if not row or not row["reasoning"]:
        return None
    try:
        result = json.loads(row["reasoning"])
    except json.JSONDecodeError:
        return None
    if "criteria" not in result:
        return None
    result["overall"] = row["choice"]
    result["called_at"] = row["called_at"]
    return result


def bundle(conn, job_id: int) -> dict | None:
    """Everything the helper needs offline for one job: identity, the generic
    pack and his prepared lines."""
    import forms
    import kit
    pack = kit.build(conn, job_id)
    if not pack:
        return None
    who = settings().get("candidate", {})
    cached = kit.answers_for_labels(conn, job_id, forms.cached(conn, job_id))
    return {"job_id": job_id, "company": pack["job"]["company"], "title": pack["job"]["title"],
            "identity": {k: who.get(k, "") for k in ("name", "email", "phone", "linkedin", "city")},
            "generic": [{"heading": hd, "body": b} for hd, b in pack["sections"] if b],
            "lines": kit.load_lines(), "answers": cached.get("answers", []),
            "blockers": pack.get("blockers", []), "flags": pack.get("flags", []),
            "fetched_at": now()}
