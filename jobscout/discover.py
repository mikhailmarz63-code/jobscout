#!/usr/bin/env python3
"""Stage 1b: find company job boards nobody told us about.

The aggregators are somebody else's index of the market. A company's own ATS
board is the market itself -- a role appears there the day it opens, often days
before it syndicates, and plenty never syndicate at all.

`companies.yml` had seven boards in it. The posting table had **1,157 employers**
that had never been probed, every one of them a company already advertising a
job the candidate's system had seen. That gap was the single largest source of missed
supply, and closing it needed no new data source -- only asking.

Each employer is tried against all five providers, and **both outcomes are
recorded**. Remembering the misses is what makes this affordable to run every
morning: without it, tomorrow re-probes the eleven hundred companies that
answered 404 today, and the day after that, forever.

Bounded per run, so the backlog clears over a few days rather than one run
taking an hour.

    python3 discover.py               # a batch of 250
    python3 discover.py --limit 50
    python3 discover.py --status      # how far through the backlog
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect                          # noqa: E402
from sources import settings                    # noqa: E402
from sources.ats import discover                # noqa: E402


def backlog(conn) -> int:
    return conn.execute("""
        SELECT COUNT(*) c FROM (
          SELECT lower(company) FROM posting
          WHERE company <> ''
            AND lower(company) NOT IN (SELECT lower(company) FROM board)
            AND lower(company) NOT IN (SELECT lower(company) FROM board_miss)
          GROUP BY lower(company))""").fetchone()["c"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--daily", action="store_true",
                    help="a small batch, for the daily run — the backlog is a "
                         "one-off, and once it clears there is nothing to do")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)

    conn = connect()
    if args.daily:
        args.limit = min(args.limit, 120)
    live = conn.execute("SELECT COUNT(*) c FROM board WHERE live = 1").fetchone()["c"]
    missed = conn.execute("SELECT COUNT(*) c FROM board_miss").fetchone()["c"]
    left = backlog(conn)

    if args.status:
        print(f"\n  live boards   {live:>6}")
        print(f"  known misses  {missed:>6}")
        print(f"  still to try  {left:>6}")
        if live:
            print("\n  biggest boards found")
            for r in conn.execute(
                    "SELECT company, ats, slug, jobs_seen FROM board "
                    "WHERE live = 1 ORDER BY jobs_seen DESC LIMIT 12"):
                print(f"    {r['company'][:30]:<30} {r['ats']:<16} "
                      f"{r['jobs_seen']:>5} jobs")
        return 0

    if not left:
        print(f"  every known employer has been probed — {live} live board(s)")
        return 0

    print(f"  {left} employers unprobed; trying {min(args.limit, left)}")
    stats = discover(conn, settings(), args.limit)
    print(f"  found {stats['found']}, missed {stats['missed']} — "
          f"{live + stats['found']} live board(s) known, "
          f"{left - stats['probed']} left to try")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
