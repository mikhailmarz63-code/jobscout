#!/usr/bin/env python3
"""Stage 1: every source -> the `posting` table. Network in, rows out, nothing
interpreted.

Re-runnable by design. A posting already on disk has its `last_seen` bumped and
its mutable fields refreshed; `first_seen` never moves, because how long a job
has been open is a real signal and rewriting it would destroy it.

A source that fails takes nothing else down with it. Eight feeds, and the run is
worth making if seven answer.

    python3 ingest.py                 # all enabled sources
    python3 ingest.py --only himalayas remoteok
    python3 ingest.py --list
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now          # noqa: E402
from sources import REGISTRY, load, settings    # noqa: E402

UPSERT = """
INSERT INTO posting (source, source_id, url, title, company, description,
                     posted_at, location_raw, employment_type, seniority,
                     apply_url, apply_email, tags, timezones, raw,
                     first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, source_id) DO UPDATE SET
    last_seen       = excluded.last_seen,
    url             = excluded.url,
    title           = excluded.title,
    description     = excluded.description,
    location_raw    = excluded.location_raw,
    apply_url       = excluded.apply_url,
    apply_email     = COALESCE(excluded.apply_email, posting.apply_email),
    tags            = excluded.tags,
    timezones       = excluded.timezones,
    raw             = excluded.raw
"""


def ingest_one(conn, name: str, cfg: dict) -> tuple[int, int, str]:
    """Returns (fetched, new, error). An exception inside a source is caught
    and reported, never raised -- see the module docstring."""
    started = now()
    try:
        module = load(name)
        # Sources that page deeply take the connection so they can remember
        # where they got to; the rest keep the one-argument signature.
        import inspect
        if "conn" in inspect.signature(module.fetch).parameters:
            postings = module.fetch(cfg, conn)
        else:
            postings = module.fetch(cfg)
    except Exception:                       # noqa: BLE001 -- deliberate: see above
        detail = traceback.format_exc(limit=3)
        log_stage(conn, f"ingest:{name}", False, detail, started)
        return 0, 0, detail.strip().splitlines()[-1]

    before = conn.execute("SELECT COUNT(*) c FROM posting WHERE source = ?",
                          (name,)).fetchone()["c"]
    seen = now()
    for p in postings:
        if not p.title or not p.url:
            continue                        # a row with no title is not a job
        conn.execute(UPSERT, p.as_row(seen))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) c FROM posting WHERE source = ?",
                         (name,)).fetchone()["c"]

    log_stage(conn, f"ingest:{name}", True,
              f"fetched={len(postings)} new={after - before}", started)
    return len(postings), after - before, ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="SOURCE")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    cfg = settings()
    if args.list:
        for name in REGISTRY:
            on = cfg.get("sources", {}).get(name, {}).get("enabled", True)
            print(f"  {name:<16} {'enabled' if on else 'disabled'}")
        return 0

    names = args.only or [n for n in REGISTRY
                          if cfg.get("sources", {}).get(n, {}).get("enabled", True)]
    conn = connect()
    total_new = failures = 0

    for name in names:
        fetched, new, err = ingest_one(conn, name, cfg)
        if err:
            failures += 1
            print(f"  {name:<16} FAILED  {err}")
        else:
            print(f"  {name:<16} {fetched:>5} fetched  {new:>4} new")
        total_new += new

    total = conn.execute("SELECT COUNT(*) c FROM posting").fetchone()["c"]
    print(f"\n{total_new} new postings, {total} on disk"
          f"{f', {failures} source(s) failed' if failures else ''}")
    # A failed source is not a failed run. Only every source failing is.
    return 1 if failures == len(names) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
