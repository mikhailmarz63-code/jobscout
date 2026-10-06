#!/usr/bin/env python3
"""The browser helper's installer: one command instead of four clicks.

    python3 helper.py install     # stable id, allow-list, board restart, then
                                  # opens brave://extensions with the folder on
                                  # the clipboard and waits for the helper to pair
    python3 helper.py launch      # no clicks at all: a second Brave with its own
                                  # profile and the helper loaded, on the board
    python3 helper.py status      # what is set up, what is not

How the id stops being a guess: an unpacked extension's id is derived from its
path unless the manifest carries a `key`. This writes a public key into
manifest.json, so the id is the same on every machine and every path, and can
be allow-listed before the browser has ever seen the folder. The private half
lives in `.helper-key.pem` (gitignored); it only matters if the extension is
ever packed.

How the token stops being pasted: once the id is allow-listed, the extension
asks the board for the token itself (`/api/pair`), which the board answers
only over loopback and only to that origin.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, now                   # noqa: E402

EXT = HERE / "extension"
MANIFEST = EXT / "manifest.json"
KEY_FILE = HERE / ".helper-key.pem"
ALLOW_FILE = HERE / ".board-extension"
TOKEN_FILE = HERE / ".board-token"
PROFILE = Path.home() / "Library/Application Support/jobscout-browser"
# LifeHub serves the extension now, on its own scoped credential.
BASE = "http://127.0.0.1:8790"

BROWSERS = [
    ("brave", Path("/Applications/Brave Browser.app"), "Brave Browser"),
    ("chrome", Path("/Applications/Google Chrome.app"), "Google Chrome"),
    ("chrome-for-testing", None, None),          # resolved from the Playwright cache
]


# ----------------------------------------------------------------- the id --

def ext_id_from_der(der: bytes) -> str:
    """Chromium's rule: the first 128 bits of SHA-256 over the DER-encoded
    public key, written in the a-p alphabet instead of hex."""
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def ext_id_from_key(key_b64: str) -> str:
    return ext_id_from_der(base64.b64decode(key_b64))


def ensure_key() -> tuple[str, str]:
    """(manifest key, extension id). Generates the pair once."""
    if not KEY_FILE.exists():
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        subprocess.run(["openssl", "genrsa", "-out", str(KEY_FILE), "2048"],
                       check=True, capture_output=True)
    der = subprocess.run(["openssl", "rsa", "-in", str(KEY_FILE), "-pubout", "-outform", "DER"],
                         check=True, capture_output=True).stdout
    key_b64 = base64.b64encode(der).decode()
    manifest = json.loads(MANIFEST.read_text())
    if manifest.get("key") != key_b64:
        manifest["key"] = key_b64
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return key_b64, ext_id_from_der(der)


def current_id() -> str | None:
    key = json.loads(MANIFEST.read_text()).get("key")
    return ext_id_from_key(key) if key else None


def write_allowlist(ext_id: str) -> str:
    origin = f"chrome-extension://{ext_id}"
    lines = []
    if ALLOW_FILE.exists():
        lines = [ln.strip() for ln in ALLOW_FILE.read_text().splitlines() if ln.strip()]
    if origin not in lines:
        lines.append(origin)
        ALLOW_FILE.write_text("\n".join(lines) + "\n")
    return origin


# -------------------------------------------------------------- the board --

def board_up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/api/jobs/freshness", timeout=2)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code in (401, 403)       # answering, just unauthenticated
    except (urllib.error.URLError, OSError):
        return False


def listener_pid(port: int = 8790) -> int | None:
    """Whoever is bound to the board's port -- found by the port, not by the
    process name, because `python3` resolves to a binary called `Python` and
    a name-based pkill quietly misses it."""
    out = subprocess.run(["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    pids = [int(x) for x in out.split() if x.isdigit()]
    return pids[0] if pids else None


def restart_board() -> bool:
    """No longer restarts anything, and no longer needs to.

    The board used to read `.board-extension` once at startup, so adding an
    extension id meant bouncing the server. LifeHub re-reads it on every
    request (lib/jobscout.ext_origins), so a newly allow-listed extension works
    immediately. Kept as a function because the install flow still reports on
    it; it now just says whether LifeHub is reachable."""
    return board_up()


def paired_at() -> int | None:
    conn = connect(readonly=True)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'extension_paired_at'").fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def wait_for_pair(since: int, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = paired_at()
        if t and t >= since:
            return True
        time.sleep(1)
    return False


def board_url() -> str:
    return f"{BASE}/?t={TOKEN_FILE.read_text().strip()}" if TOKEN_FILE.exists() else BASE


# ------------------------------------------------------------ the browser --

def find_browser(prefer: str = "auto") -> tuple[str, list[str]] | None:
    """(name, argv prefix that launches a *new instance*)."""
    order = [b for b in BROWSERS if prefer in ("auto", b[0])]
    for name, app, appname in order:
        if name == "chrome-for-testing":
            cache = Path.home() / "Library/Caches/ms-playwright"
            for d in sorted(cache.glob("chromium-*"), reverse=True):
                binary = next(iter(d.glob("chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")), None)
                if binary:
                    return name, [str(binary)]
        elif app and app.exists():
            return name, ["open", "-na", appname, "--args"]
    return None


def is_running(appname: str) -> bool:
    return subprocess.run(["pgrep", "-x", appname], capture_output=True).returncode == 0


def launch(prefer: str, verify: bool, url: str | None) -> int:
    found = find_browser(prefer)
    if not found:
        print("no Brave, Chrome or Chrome for Testing found", file=sys.stderr)
        return 1
    name, prefix = found
    PROFILE.mkdir(parents=True, exist_ok=True)
    flags = [f"--user-data-dir={PROFILE}", f"--load-extension={EXT}",
             "--no-first-run", "--no-default-browser-check"]
    if verify:
        flags.append("--remote-debugging-port=9333")
    target = url or board_url()
    argv = prefix + flags + [target]
    started = now()
    if name == "chrome-for-testing":
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    else:
        subprocess.run(argv, check=False)
    print(f"launched {name} with its own profile ({PROFILE.name}) and the helper loaded")
    if not verify:
        return 0
    ext_id = current_id() or ""
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=1) as r:
                targets = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if any(ext_id and ext_id in (t.get("url") or "") for t in targets):
            print(f"verified: the browser reports the extension {ext_id}")
            if wait_for_pair(started, 25):
                print("paired ✓ — the helper fetched its token from the board on its own")
            else:
                print("loaded, but no pairing seen within 25s — is the id allow-listed and the board restarted?")
            return 0
    print("could not verify the extension loaded (no chrome-extension:// target appeared)")
    return 1


def open_extensions_page(appname: str) -> None:
    subprocess.run(["open", "-a", appname, "chrome://extensions/"], check=False)


# ----------------------------------------------------------------- verbs --

def install(args) -> int:
    key, ext_id = ensure_key()
    origin = write_allowlist(ext_id)
    print(f"extension id   {ext_id}")
    print(f"allow-listed   {origin}  ({ALLOW_FILE.name})")
    started = now()
    if args.no_restart:
        print("board          left as is (--no-restart); restart it to load the allow-list")
    else:
        print("lifehub        " + ("up — the allow-list is re-read per request, no restart needed"
                          if restart_board() else "not running — start it: python3 lifehub-web/server.py"))
    if paired_at():
        print(f"paired         already, at {time.strftime('%Y-%m-%d %H:%M', time.localtime(paired_at()))}")
    if args.no_open:
        return 0
    found = find_browser("auto")
    appname = found and (found[1][2] if found[1][0] == "open" else None)
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=str(EXT).encode(), check=False)
    if appname:
        open_extensions_page(appname)
        print(f"\n{appname} is showing the extensions page. Two clicks remain, once:\n"
              f"  1. turn on Developer mode (top right) if it is off\n"
              f"  2. Load unpacked → press Cmd+Shift+G, paste (the folder is on your clipboard), Enter\n"
              f"     {EXT}\n"
              f"The helper then pairs with the board by itself — no token to paste.")
    else:
        print(f"open chrome://extensions, Developer mode, Load unpacked → {EXT}")
    if args.no_wait:
        return 0
    print(f"\nwaiting up to {args.wait}s for the helper to pair…", flush=True)
    if wait_for_pair(started, args.wait):
        print("paired ✓ — the helper has the token and the board accepts its origin")
        return 0
    print("not paired yet. Run `python3 helper.py status` after loading it, or `python3 helper.py launch` for the no-click path.")
    return 2


def status(args) -> int:
    ext_id = current_id()
    origins = ALLOW_FILE.read_text().split() if ALLOW_FILE.exists() else []
    print(f"extension id   {ext_id or 'not set — run install'}")
    print(f"allow-list     {', '.join(origins) or 'empty'}"
          + ("" if not ext_id or f"chrome-extension://{ext_id}" in origins else "  ← id not listed"))
    print(f"lifehub        {'up' if board_up() else 'down'} at {BASE}")
    t = paired_at()
    print(f"paired         {time.strftime('%Y-%m-%d %H:%M', time.localtime(t)) if t else 'never'}")
    found = find_browser("auto")
    print(f"browser        {found[0] if found else 'none found'}"
          + (f", running" if found and found[1][0] == "open" and is_running(found[1][2]) else ""))
    print(f"own profile    {'exists' if PROFILE.exists() else 'not created'} ({PROFILE})")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="stable id, allow-list, restart the board, open the extensions page, wait for the pair")
    i.add_argument("--no-open", action="store_true", help="do not open the browser")
    i.add_argument("--no-wait", action="store_true")
    i.add_argument("--no-restart", action="store_true")
    i.add_argument("--wait", type=int, default=180, help="seconds to wait for the pair")
    ln = sub.add_parser("launch", help="a second browser instance, own profile, helper loaded — no clicks")
    ln.add_argument("--browser", default="auto", choices=["auto", "brave", "chrome", "chrome-for-testing"])
    ln.add_argument("--verify", action="store_true", help="open a debugging port and confirm the extension loaded")
    ln.add_argument("--url", help="page to open instead of the board")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "install":
        return install(args)
    if args.cmd == "launch":
        ensure_key()
        write_allowlist(current_id())
        if not board_up():
            restart_board()
        return launch(args.browser, args.verify, args.url)
    return status(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
