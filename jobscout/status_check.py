#!/usr/bin/env python3
"""Stage: is a shortlisted job still open, per open-jobs' own crawler.

`POST /status` on the same public index `sources/openjobs.py` reads from
(github.com/elliottdehn/open-jobs, CC0) answers, per key, whether their crawler
still sees the posting, last saw it, or watched it get pulled. That is a
second, independent signal on top of this project's own `last_seen` ageing --
useful specifically for the ATS boards this project polls directly, where a
company deleting a posting produces no signal here at all until `last_seen`
drifts stale weeks later.

Deliberately conservative about which postings it asks about at all: a wrong
guess at the key can only ever come back "unknown" (harmless) rather than
match someone else's real posting, so only vendors this project can build an
exact slug regex for are included. See `status_key()`.

Off by default (`sources.openjobs.status_sweep.enabled` in settings.yml) --
same host as openjobs.py, same "read a dry run first" rule. When enabled, a
network failure is loud: this stage logs it to run_log and exits non-zero, it
never swallows it the way a single ingest source is allowed to.

    python3 status_check.py             # the configured top_n, if enabled
    python3 status_check.py --force     # run even if the setting says off
    python3 status_check.py --dry-run   # show what would close, change nothing
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402
from sources import get, settings           # noqa: E402

BASE = "https://backend.dehnbostele.workers.dev"

# Only vendors this project can build an exact slug regex for. A wrong key
# here would not fail closed -- it would fail by matching someone else's real
# posting -- so workable and smartrecruiters are left out until their URL
# shapes are as well-established here as these three (forms.py trusts the
# same two greenhouse/ashby patterns for the same reason).
SLUG_RE = {
    "greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"ashbyhq\.com/([a-z0-9_.-]+)", re.I),
    "lever": re.compile(r"lever\.co/([a-z0-9_-]+)/", re.I),
}


def status_key(source: str, source_id: str, url: str) -> str | None:
    """`<ats>/<slug>#<id>` -- open-jobs' own key shape -- or None when this
    posting cannot be mapped to it with confidence."""
    if source == "openjobs":
        return source_id            # already in that shape; see sources/openjobs.py
    if ":" not in (source_id or ""):
        return None
    vendor, ident = source_id.split(":", 1)
    pattern = SLUG_RE.get(vendor)
    if not pattern or not ident:
        return None
    m = pattern.search(url or "")
    if not m:
        return None
    return f"{vendor}/{m.group(1)}#{ident}"


def shortlisted_keys(conn, top_n: int, max_keys: int, max_boards: int
                      ) -> dict[str, list[int]]:
    """key -> [job_id, ...] for the top_n open jobs by score, capped to the
    endpoint's own per-call limits (1000 keys / 150 distinct boards). Ordered
    by score, so a cap truncates the least-ranked jobs first, never the
    others -- the whole point of "shortlisted"."""
    job_ids = [r["id"] for r in conn.execute("""
        SELECT j.id FROM job j JOIN score sc ON sc.job_id = j.id
        WHERE j.status = 'open' ORDER BY sc.total DESC LIMIT ?
    """, (top_n,))]
    if not job_ids:
        return {}
    placeholders = ",".join("?" * len(job_ids))
    postings = conn.execute(f"""
        SELECT job_id, source, source_id, url FROM posting
        WHERE job_id IN ({placeholders})
    """, job_ids).fetchall()

    out: dict[str, list[int]] = {}
    boards: set[str] = set()
    for p in postings:
        key = status_key(p["source"], p["source_id"], p["url"])
        if not key:
            continue
        board = key.rsplit("#", 1)[0]
        if board not in boards and len(boards) >= max_boards:
            continue
        boards.add(board)
        out.setdefault(key, []).append(p["job_id"])
        if len(out) >= max_keys:
            break
    return out


def sweep(conn, cfg: dict, dry_run: bool = False) -> dict:
    conf = cfg.get("sources", {}).get("openjobs", {}).get("status_sweep", {})
    top_n = int(conf.get("top_n", 150))
    max_keys = int(conf.get("max_keys", 1000))
    max_boards = int(conf.get("max_boards", 150))

    keyed = shortlisted_keys(conn, top_n, max_keys, max_boards)
    if not keyed:
        return {"checked": 0, "closed": 0}

    payload = get(f"{BASE}/status", cfg=cfg, json_body={"keys": list(keyed)})
    if not payload or "statuses" not in payload:
        raise RuntimeError("open-jobs /status did not answer -- network or format failure")

    closed = []
    for key, info in payload["statuses"].items():
        if (info or {}).get("status") != "removed":
            continue
        closed.extend(keyed.get(key, []))
    closed = sorted(set(closed))

    if closed and not dry_run:
        placeholders = ",".join("?" * len(closed))
        conn.execute(
            f"UPDATE job SET status = 'closed' WHERE id IN ({placeholders}) "
            f"AND status = 'open'", closed)
        conn.commit()
    return {"checked": len(keyed), "closed": len(closed), "closed_ids": closed}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="run even if sources.openjobs.status_sweep.enabled is false")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = settings()
    conf = cfg.get("sources", {}).get("openjobs", {}).get("status_sweep", {})
    if not conf.get("enabled", False) and not args.force:
        print("status_check: disabled (sources.openjobs.status_sweep.enabled "
              "is false) -- nothing to do")
        return 0

    conn = connect()
    started = now()
    try:
        result = sweep(conn, cfg, dry_run=args.dry_run)
    except Exception as exc:                # noqa: BLE001 -- see module docstring: loud, not swallowed
        detail = f"{type(exc).__name__}: {exc}"
        log_stage(conn, "status_check", False, detail, started)
        print(f"status_check: FAILED -- {detail}", file=sys.stderr)
        return 1

    detail = f"checked={result['checked']} closed={result['closed']}"
    log_stage(conn, "status_check", True, detail, started)
    print(f"status_check: {detail}"
          f"{' (dry run -- nothing written)' if args.dry_run else ''}")
    if result["closed"]:
        print(f"  closed job ids: {result.get('closed_ids', [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
