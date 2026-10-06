#!/usr/bin/env python3
"""The list. Everything above this file exists to make this one honest.

Ordered by `score.py`, which puts eligibility and reach ahead of pay. Both of
those outrank money deliberately: a $12,000/month role he cannot legally take,
or will never be shortlisted for, is worth less than a $2,500 one he can get.
Sorting by pay alone put the whole US-only market at the top of every list, and
then put Directors of Sales at the top of what was left.

Only jobs with `fit.viable = 1` appear here -- meaning he can take it (remote,
unless it is in Sri Lanka or comes with a sponsored relocation) *and* they would
plausibly have him.

Jobs whose salary is unknown are **listed, not hidden**, in their own section.
A third of this market publishes no number and some of the best of it is in
there; a report that silently dropped them would be quietly lying about what is
available.

    python3 report.py                  # the list
    python3 report.py --limit 40
    python3 report.py --state OPEN_WORLDWIDE
    python3 report.py --min-pay 3000
    python3 report.py --unknown-pay    # only the ones with no published number
    python3 report.py --blocked        # what was filtered out, and why
    python3 report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, now              # noqa: E402
from eligibility import SENDABLE         # noqa: E402
from sources import settings             # noqa: E402

# Best case first. This is the order a day should be worked in.
STATE_RANK = {
    "ONSITE_LK": 0,          # he is already in the country
    "OPEN_WORLDWIDE": 1,     # explicitly anywhere
    "OPEN_REGION": 2,        # a region that contains Sri Lanka
    "OPEN_CONTRACTOR": 3,    # hireable through an EOR
    "ONSITE_SPONSORED": 4,   # a move, but a funded one
    "UNKNOWN": 5,            # needs ten seconds of reading
}

# Would they take him? The audit that produced this column found 49 of 69
# verdicts were "well above him", so it goes first, before the money.
REACH = {"likely": "YOUR LEVEL", "plausible": "REACHABLE",
         "stretch": "A STRETCH", "no_chance": "NO CHANCE"}

MODE = {"remote": "remote", "hybrid": "HYBRID", "onsite": "ONSITE", "unknown": "—"}

LABEL = {
    "ONSITE_LK": "SRI LANKA", "OPEN_WORLDWIDE": "WORLDWIDE",
    "OPEN_REGION": "REGION-OK", "OPEN_CONTRACTOR": "CONTRACT",
    "ONSITE_SPONSORED": "SPONSORED", "UNKNOWN": "CHECK IT",
}


def age(posted: int | None, first_seen: int) -> str:
    stamp = posted or first_seen
    days = (now() - stamp) // 86400
    if days <= 0:
        return "today"
    return f"{days}d"


def rows(conn, states: list[str], min_pay: float | None, limit: int,
         unknown_pay: bool | None = None, blocked: bool = False) -> list:
    placeholders = ",".join("?" for _ in states)
    clauses = [f"e.state IN ({placeholders})", "j.status = 'open'"]
    params: list = list(states)

    if min_pay is not None:
        # Applied to KNOWN salaries only. A job with no published number is not
        # a job below the floor -- it is a job we have not priced.
        clauses.append("(s.known = 0 OR s.known IS NULL OR "
                       "COALESCE(s.max_usd_month, s.min_usd_month) >= ?)")
        params.append(min_pay)
    if unknown_pay is True:
        clauses.append("(s.known = 0 OR s.known IS NULL)")
    elif unknown_pay is False:
        clauses.append("s.known = 1")

    params.append(limit)
    return conn.execute(f"""
        SELECT j.id, j.company, j.title, j.canonical_url, j.posted_at,
               j.first_seen, j.source_count, e.state, e.rule, e.evidence_quote,
               e.confidence, s.known pay_known, s.min_usd_month lo,
               s.max_usd_month hi, s.src_currency cur, s.derived_by,
               sc.total score, f.reach, f.work_mode, f.reach_why, f.viable_why
        FROM job j
        JOIN eligibility e ON e.job_id = j.id
        JOIN fit f ON f.job_id = j.id AND f.viable = 1
        LEFT JOIN salary s ON s.job_id = j.id
        LEFT JOIN score  sc ON sc.job_id = j.id
        WHERE {' AND '.join(clauses)}
        ORDER BY
            sc.total DESC NULLS LAST,
            CASE e.state {' '.join(f"WHEN '{k}' THEN {v}" for k, v in STATE_RANK.items())}
                 ELSE 9 END,
            s.known DESC NULLS LAST,
            COALESCE(s.max_usd_month, s.min_usd_month) DESC NULLS LAST,
            j.posted_at DESC NULLS LAST
        LIMIT ?
    """, params).fetchall()


def money(r) -> str:
    if not r["pay_known"]:
        return "not published"
    lo, hi = r["lo"], r["hi"]
    if lo and hi and abs(hi - lo) > 1:
        return f"${lo:,.0f}-{hi:,.0f}/mo"
    return f"${(hi or lo):,.0f}/mo"


def render(conn, rs: list, show_evidence: bool = True) -> None:
    if not rs:
        print("  nothing matched.")
        return
    for r in rs:
        star = "*" if r["state"] in SENDABLE else " "
        print(f"{star} #{r['id']:<5} {REACH.get(r['reach'], '?'):<10} "
              f"{LABEL.get(r['state'], r['state']):<10} {MODE.get(r['work_mode'], ''):<7}"
              f"{money(r):>18}  {age(r['posted_at'], r['first_seen']):>5}  "
              f"{r['company'][:22]:<22} {r['title'][:44]}")
        if show_evidence and r["evidence_quote"]:
            print(f"          why: {r['evidence_quote'][:104]}")
        if r["canonical_url"]:
            print(f"          {r['canonical_url'][:104]}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--state", action="append")
    ap.add_argument("--min-pay", type=float)
    ap.add_argument("--unknown-pay", action="store_true")
    ap.add_argument("--blocked", action="store_true")
    ap.add_argument("--no-evidence", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = settings()
    conn = connect()
    floor = cfg["pay"]["floor_usd_month"]

    if args.blocked:
        print("Filtered out, and the rule that did it:\n")
        for r in conn.execute("""
                SELECT e.rule, COUNT(*) n FROM eligibility e JOIN job j ON j.id = e.job_id
                WHERE e.state IN ('BLOCKED', 'ONSITE_NO_SPONSOR') AND j.status = 'open'
                GROUP BY e.rule ORDER BY n DESC LIMIT 20"""):
            print(f"  {r['n']:>5}  {r['rule']}")
        total = conn.execute(
            "SELECT COUNT(*) c FROM eligibility e JOIN job j ON j.id = e.job_id "
            "WHERE e.state IN ('BLOCKED','ONSITE_NO_SPONSOR') AND j.status='open'"
        ).fetchone()["c"]
        print(f"\n{total} jobs he cannot take. That number is the reason this "
              f"gate exists.")
        return 0

    states = args.state or list(STATE_RANK)
    min_pay = args.min_pay if args.min_pay is not None else floor

    if args.json:
        rs = rows(conn, states, min_pay, args.limit,
                  unknown_pay=True if args.unknown_pay else None)
        print(json.dumps([dict(r) for r in rs], indent=2, default=str))
        return 0

    stamp = datetime.now(timezone.utc).astimezone().strftime("%a %d %b %Y, %H:%M")
    total_open = conn.execute(
        "SELECT COUNT(*) c FROM job WHERE status = 'open'").fetchone()["c"]
    reachable = conn.execute(f"""
        SELECT COUNT(*) c FROM eligibility e JOIN job j ON j.id = e.job_id
        JOIN fit f ON f.job_id = j.id
        WHERE j.status = 'open' AND f.viable = 1
          AND e.state IN ({','.join('?' * len(states))})
    """, states).fetchone()["c"]
    at_level = conn.execute("""
        SELECT COUNT(*) c FROM fit f JOIN job j ON j.id = f.job_id
        WHERE j.status = 'open' AND f.viable = 1 AND f.reach = 'likely'
    """).fetchone()["c"]

    print(f"\njobscout — {stamp}")
    print(f"{total_open:,} open · {reachable:,} you can take and might get · "
          f"{at_level} at your level · floor ${min_pay:,.0f}/mo\n")

    if args.unknown_pay:
        render(conn, rows(conn, states, None, args.limit, unknown_pay=True),
               not args.no_evidence)
        return 0

    priced = rows(conn, states, min_pay, args.limit, unknown_pay=False)
    print(f"── PAYING ABOVE THE FLOOR ─────────────────────────────────────")
    render(conn, priced, not args.no_evidence)

    unpriced = rows(conn, states, None, max(5, args.limit // 3), unknown_pay=True)
    print(f"\n── NO PUBLISHED SALARY ({len(unpriced)} of many) ──────────────")
    print("   Not below the floor — unpriced. A lot of the best roles are here.\n")
    render(conn, unpriced, False)

    review = conn.execute(
        "SELECT COUNT(*) c FROM eligibility e JOIN job j ON j.id = e.job_id "
        "WHERE e.state = 'UNKNOWN' AND j.status = 'open'").fetchone()["c"]
    print(f"\n{review} jobs need ten seconds of human reading: "
          f"python3 eligibility.py --review 20")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
