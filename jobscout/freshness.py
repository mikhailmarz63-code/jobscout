#!/usr/bin/env python3
"""Stage: ghost and re-stamp grading.

Steal from open-jobs (CC0), which grades every posting it crawls the same
way and catches the re-stamp "because first_seen can't be forged". Ours is the
same idea, sized to this database.

The bug this replaces: `score.py` used to compute freshness from
`posted_at or first_seen`, so a source that bumps a listing's date without the
job ever having closed made a month-old posting score as brand new. The fix is
`basis()` below -- the *older* of the two, because `first_seen` is ours and
cannot be gamed by whoever writes the advert.

Four verdicts. Ghost outranks the others: a job open long past what its kind
usually takes is worth hiding by default whatever else is true of it.

    python3 freshness.py                # grade everything undecided
    python3 freshness.py --all          # re-grade
    python3 freshness.py --breakdown    # counts by verdict
    python3 freshness.py --explain 412  # one job, the maths behind its verdict
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402

RESTAMP_DAYS = 2        # posted_at newer than first_seen by more than this
GHOST_MULTIPLE = 2.0    # more than this many times the par
GHOST_ABSOLUTE_DAYS = 120
GLOBAL_DEFAULT_PAR = 30.0   # used only when there is no closed-job history at all
FAMILY_MIN_N = 20


def basis(posted_at: int | None, first_seen: int) -> int:
    """The timestamp freshness is measured from -- the earlier of the two when
    both exist, because `first_seen` is the one side an advert cannot write.

    This is the whole fix: the old code took `posted_at or first_seen`, which
    trusted the source's own date whenever it had one. A source that re-dates a
    listing to look new again -- without the job ever actually closing -- made
    that bug read as "posted in the last 2 days" for a job that had in fact been
    open for a month.
    """
    if posted_at is None:
        return first_seen
    return min(posted_at, first_seen)


# Same family logic normalise.py already uses for titles, reduced further:
# strip seniority markers, then keep the first two significant tokens -- so
# "Senior Backend Engineer II" and "Backend Engineer" land in the same bucket
# while "Backend Engineer" and "Product Manager" do not. Coarser than an
# exact-title match, which almost never reaches the n>=20 the par calculation
# asks for; still narrow enough that a software-engineering par and a sales
# par never mix.
_NOISE = re.compile(r"[^a-z0-9\s]")
_LEVEL_WORDS = {"senior", "sr", "jr", "junior", "lead", "staff", "principal",
               "associate", "i", "ii", "iii", "iv", "v", "1", "2", "3"}


def title_family(title: str) -> str:
    s = _NOISE.sub(" ", (title or "").lower())
    tokens = [t for t in s.split() if t and t not in _LEVEL_WORDS]
    return " ".join(tokens[:2])


def _percentile(values: list[float], p: float) -> float:
    """The p-th percentile, no numpy required. `statistics.quantiles` needs at
    least two points; below that the single value (or the default) stands in
    for "the par", which is the honest answer with nothing to measure yet."""
    if not values:
        return GLOBAL_DEFAULT_PAR
    if len(values) == 1:
        return values[0]
    # n=100 cuts give the 75th percentile directly; quantiles() already
    # interpolates, which the raw n>=20 sample here is small enough to need.
    qs = statistics.quantiles(sorted(values), n=100, method="inclusive")
    idx = min(max(int(round(p * 100)) - 1, 0), len(qs) - 1)
    return qs[idx]


def _closed_open_days(conn) -> list[tuple[str, float]]:
    """(title_family, open_days) for every closed job with enough history to
    compute open_days from. `last_seen` is the closest thing to a close date
    this schema has until something marks `closed_at` directly -- `status.py`'s
    /status sweep (added alongside this) starts producing real closures; until
    there are twenty of them per family the global fallback below carries it."""
    rows = conn.execute(
        "SELECT title, first_seen, last_seen, posted_at FROM job "
        "WHERE status = 'closed'").fetchall()
    out = []
    for r in rows:
        start = basis(r["posted_at"], r["first_seen"])
        days = (r["last_seen"] - start) / 86400
        if days > 0:
            out.append((title_family(r["title"]), days))
    return out


def par_for(conn, title: str, cache: dict | None = None) -> tuple[float, str]:
    """(par_days, source). Cached per call to `apply()` -- computing it fresh
    per job would re-scan every closed job once per open one."""
    if cache is None:
        cache = {}
    if "_by_family" not in cache:
        by_family: dict[str, list[float]] = {}
        all_days: list[float] = []
        for fam, days in _closed_open_days(conn):
            by_family.setdefault(fam, []).append(days)
            all_days.append(days)
        cache["_by_family"] = by_family
        cache["_global"] = _percentile(all_days, 0.75)
    fam = title_family(title)
    bucket = cache["_by_family"].get(fam, [])
    if len(bucket) >= FAMILY_MIN_N:
        return _percentile(bucket, 0.75), "family"
    return cache["_global"], "global"


def classify(basis_days: float, par_days: float, restamp_days: float | None
             ) -> str:
    if basis_days > GHOST_ABSOLUTE_DAYS or basis_days > GHOST_MULTIPLE * par_days:
        return "ghost"
    if restamp_days and restamp_days > RESTAMP_DAYS:
        return "restamped"
    if basis_days > par_days:
        return "stale"
    return "fresh"


def apply(conn, redo: bool = False) -> dict:
    started = now()
    where = "" if redo else " AND fr.job_id IS NULL"
    rows = conn.execute(f"""
        SELECT j.id, j.title, j.posted_at, j.first_seen
        FROM job j
        LEFT JOIN freshness fr ON fr.job_id = j.id
        WHERE j.status = 'open' {where}
    """).fetchall()

    cache: dict = {}
    stats = {"fresh": 0, "stale": 0, "restamped": 0, "ghost": 0}
    stamp = now()
    for r in rows:
        start = basis(r["posted_at"], r["first_seen"])
        basis_days = (stamp - start) / 86400
        restamp_days = ((r["posted_at"] - r["first_seen"]) / 86400
                        if r["posted_at"] and r["posted_at"] > r["first_seen"] else None)
        par_days, par_source = par_for(conn, r["title"] or "", cache)
        verdict = classify(basis_days, par_days, restamp_days)
        stats[verdict] += 1

        conn.execute("""
            INSERT INTO freshness (job_id, verdict, basis_days, par_days,
                                   par_source, restamp_days, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                verdict = excluded.verdict, basis_days = excluded.basis_days,
                par_days = excluded.par_days, par_source = excluded.par_source,
                restamp_days = excluded.restamp_days, decided_at = excluded.decided_at
        """, (r["id"], verdict, round(basis_days, 2), round(par_days, 2),
              par_source, round(restamp_days, 2) if restamp_days else None, stamp))

    conn.commit()
    log_stage(conn, "freshness", True,
              f"fresh={stats['fresh']} stale={stats['stale']} "
              f"restamped={stats['restamped']} ghost={stats['ghost']}", started)
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--breakdown", action="store_true")
    ap.add_argument("--explain", type=int, metavar="JOB_ID")
    args = ap.parse_args(argv)

    conn = connect()

    if args.explain:
        r = conn.execute(
            "SELECT j.title, j.company, fr.* FROM freshness fr "
            "JOIN job j ON j.id = fr.job_id WHERE fr.job_id = ?",
            (args.explain,)).fetchone()
        if not r:
            print(f"no freshness row for job {args.explain} -- run freshness.py first")
            return 1
        print(f"{r['company']} — {r['title']}\n")
        print(f"  verdict       {r['verdict']}")
        print(f"  basis_days    {r['basis_days']}")
        print(f"  par_days      {r['par_days']}  ({r['par_source']})")
        print(f"  restamp_days  {r['restamp_days'] or '—'}")
        return 0

    if args.breakdown:
        for r in conn.execute(
                "SELECT verdict, COUNT(*) n FROM freshness fr "
                "JOIN job j ON j.id = fr.job_id WHERE j.status = 'open' "
                "GROUP BY verdict ORDER BY n DESC"):
            print(f"  {r['verdict']:<12} {r['n']:>6,}")
        return 0

    stats = apply(conn, redo=args.all)
    print(f"fresh {stats['fresh']}   stale {stats['stale']}   "
          f"restamped {stats['restamped']}   ghost {stats['ghost']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
