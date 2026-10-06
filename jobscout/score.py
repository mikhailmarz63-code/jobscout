#!/usr/bin/env python3
"""Stage 6: one number per job, 0-100. The daily list is this, sorted.

Until this file existed the list was sorted by pay, and the top of it was
Directors of Sales and Heads of Marketing -- real jobs, open to him, that he has
no chance of getting and no interest in. Pay is a component here, not the
ordering.

Seven components, and the three that matter most are the ones a naive ranking
has no way to see:

  * **timezone fit.** A US-East job is a 0.0-hour overlap with a Colombo working
    day. It is a night shift with a day job's title, and no salary column will
    ever say so.
  * **reach.** His own CV critique (2026-08-17) named overreach as the blocking
    problem: "capable graduate, oversold". An independent audit of the top forty
    found 49 of 69 verdicts were "well above him". The classification lives in
    `fit.py`; this file only turns it into points.
  * **role.** Whether it is his profession at all, read from the title alone --
    because an advert's body describes the company as much as the job, and the
    company is usually a technology company whichever job it is advertising.

Jobs that `fit.py` marked non-viable never reach this file.

Every score stores its full breakdown as JSON, because a rank nobody can
interrogate is a rank nobody should trust.

    python3 score.py             # score everything, print the top 25
    python3 score.py --top 50
    python3 score.py --explain 412
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "job-applications"))

from db import connect, log_stage, now      # noqa: E402
from sources import settings                # noqa: E402

# Reused rather than rewritten: job-applications/matcher.py already scores a
# posting against each CV variant, keyword by keyword, tuned from the actual
# resume text. It is the same question this needs answering.
try:
    from matcher import VARIANT_KEYWORDS, pick_variant
except ImportError:                         # pragma: no cover
    VARIANT_KEYWORDS, pick_variant = {}, None
    print("score.py: ../job-applications/matcher.py not importable -- the "
          "profile component is running keyword-only", file=sys.stderr)

WEIGHTS = {
    "eligibility": 30,     # a job he cannot take is worth nothing, whatever it pays
    "timezone": 20,        # the hours he would actually work
    "role": 20,            # is this even his profession
    "profile": 15,         # does his CV have anything to say about the detail
    "seniority": 15,       # is it a level he can credibly reach
    "pay": 20,             # above the floor, how far above
    "freshness": 10,       # a three-week-old posting has a shortlist already
}

# Everything scoring positive, if a job maxed every component. Scores are
# normalised against this so 100 is genuinely the top.
MAX_POINTS = sum(WEIGHTS.values())

ELIGIBILITY_POINTS = {
    "ONSITE_LK": 1.0,          # in-country: no visa, no timezone compromise
    "OPEN_WORLDWIDE": 1.0,
    "OPEN_REGION": 0.9,
    "OPEN_CONTRACTOR": 0.8,    # real, but self-employment tax and no benefits
    "ONSITE_SPONSORED": 0.6,   # a life move, not a job change
    "UNKNOWN": 0.35,           # might be anything; worth a look, not a rush
}

BAND_POINTS = {"green": 1.0, "amber": 0.45, "red": 0.0}

# Seniority vocabulary used to live here (TOO_SENIOR / ENTRY / MID) and was
# removed on 2026-08-24: fit.py owns the classification now, so keeping a
# second copy here could only ever drift out of agreement with it.

# The role vocabulary and role_fit() moved to fit.py on 2026-08-24.
#
# An adversarial review of this file made the point that settled it: role_fit
# and the reach classifier answer the same question -- is this job right for
# him -- while eligibility, timezone, pay and freshness make up 80 of the 130
# points and say nothing about whether he is qualified at all. A job in the
# wrong profession was losing 20 points and keeping its place. Nineteen of the
# 98 viable jobs were somebody else's career.
#
# So profession is a gate in fit.py now, next to reach, and this file only
# converts what fit decided into points.
from fit import reach, role_fit          # noqa: E402
# freshness.basis() is the fix for the bug this file used to carry: see
# freshness() below.
from freshness import basis as freshness_basis   # noqa: E402

SKILLS = [
    "python", "typescript", "javascript", "sql", "sqlite", "postgres",
    "postgresql", "node", "react", "html", "css", "asp.net", ".net", "docker",
    "api", "rest", "automation", "n8n", "workflow", "llm", "ai", "claude",
    "openai", "gpt", "prompt", "rag", "ollama", "machine learning",
    "business analyst", "requirements", "stakeholder", "process improvement",
    "documentation", "excel", "reporting", "dashboard", "data analysis",
    "erp", "mis", "pdf", "logistics", "freight", "supply chain", "operations",
]


def profile_fit(title: str, description: str) -> tuple[float, str]:
    """0.0 to 1.0. How much of his CV is relevant at all."""
    text = f"{title} {description}".lower()
    hits = [s for s in SKILLS if s in text]
    # Ten distinct skills named is as strong a signal as this needs; beyond
    # that it is a long advert, not a better match.
    skill_score = min(len(hits) / 10.0, 1.0)

    variant = ""
    if pick_variant and description:
        try:
            variant, scores = pick_variant(f"{title}\n{description}")
            top = max(scores.values()) if scores else 0
            skill_score = max(skill_score, min(top / 25.0, 1.0))
        except Exception:                   # noqa: BLE001 -- a helper, not a gate
            variant = ""
    return skill_score, f"{len(hits)} skills{f', cv:{variant}' if variant else ''}"


# Reach -> seniority points. `fit.reach()` owns the classification; this only
# converts it. The vocabulary that used to live here was removed on 2026-08-24,
# because a second copy of it could only ever drift out of agreement with the
# one that decides viability.
REACH_POINTS = {
    "likely": 1.0,        # his level, his field
    "plausible": 0.35,    # mid, but reachable
    "stretch": -0.5,      # worth a shot, never a plan
    "no_chance": -1.0,    # and these are dropped before scoring anyway
}


def seniority_fit(title: str, description: str) -> tuple[float, str]:
    """-1.0 to +1.0. Delegates the judgment to fit.reach() and only converts."""
    level, why = reach(title, description)
    return REACH_POINTS.get(level, 0.0), f"{level} — {why}"


def pay_fit(known: int | None, lo: float | None, hi: float | None,
            floor: float) -> tuple[float, str]:
    """0.0 to 1.0. Unknown pay scores at the midpoint, not at zero.

    Scoring an unpublished salary as zero would push every unpriced job to the
    bottom, and a third of this market -- including much of the best of it --
    publishes no number. Neutral is the honest position: we do not know.
    """
    if not known:
        return 0.5, "not published"
    top = hi or lo or 0
    if top < floor:
        return 0.0, f"${top:,.0f} is below the floor"
    # Full marks at 4x the floor. Beyond that the extra money is not what
    # decides whether to apply.
    ratio = min((top - floor) / (floor * 3.0), 1.0)
    return round(0.4 + 0.6 * ratio, 3), f"${top:,.0f}/mo"


def freshness(posted: int | None, first_seen: int) -> tuple[float, str]:
    """Age from the *earlier* of the two dates, not `posted or first_seen`.

    That used to be a straight `posted or first_seen`, which trusted a source's
    own date whenever it had one -- so a listing re-dated to look new again,
    without the job ever actually closing, scored as "posted in the last 2
    days" however long it had really been open. `first_seen` is ours and a
    source cannot write to it, so the older of the two is the safe bound. See
    `freshness.py` for the verdict (fresh/stale/restamped/ghost) built on the
    same basis and shown on the board.
    """
    days = (now() - freshness_basis(posted, first_seen)) / 86400
    if days <= 2:
        return 1.0, "posted in the last 2 days"
    if days <= 7:
        return 0.7, f"{days:.0f} days old"
    if days <= 21:
        return 0.35, f"{days:.0f} days old"
    return 0.0, f"{days:.0f} days old — a shortlist already exists"


def score_one(row, floor: float) -> tuple[float, dict]:
    parts = {}

    value = ELIGIBILITY_POINTS.get(row["state"], 0.0)
    parts["eligibility"] = {"points": round(value * WEIGHTS["eligibility"], 1),
                            "why": row["state"]}

    if row["band"]:
        value = BAND_POINTS.get(row["band"], 0.0)
        why = f"{row['band']} — {row['overlap_hours']}h overlap"
    else:
        # No timezone known. Neutral, like unknown pay: an unplaced job is not
        # a bad job, it is an unmeasured one.
        value, why = 0.5, "timezone unknown"
    parts["timezone"] = {"points": round(value * WEIGHTS["timezone"], 1), "why": why}

    value, why = role_fit(row["title"] or "")
    parts["role"] = {"points": round(value * WEIGHTS["role"], 1), "why": why}

    value, why = profile_fit(row["title"] or "", row["description"] or "")
    parts["profile"] = {"points": round(value * WEIGHTS["profile"], 1), "why": why}

    value, why = seniority_fit(row["title"] or "", row["description"] or "")
    # Maps -1..+1 onto -weight..+weight, so a Director role is actively pushed
    # down rather than merely failing to be pushed up.
    parts["seniority"] = {"points": round(value * WEIGHTS["seniority"], 1), "why": why}

    value, why = pay_fit(row["pay_known"], row["lo"], row["hi"], floor)
    parts["pay"] = {"points": round(value * WEIGHTS["pay"], 1), "why": why}

    value, why = freshness(row["posted_at"], row["first_seen"])
    parts["freshness"] = {"points": round(value * WEIGHTS["freshness"], 1), "why": why}

    # Normalised so 100 is the ceiling and the number means something on its
    # own -- `dispatch.min_score` in settings.yml is a threshold a human has to
    # be able to reason about. Raw points top out at MAX_POINTS because the
    # seniority component can subtract as well as add.
    raw = sum(p["points"] for p in parts.values())
    total = round(max(0.0, raw / MAX_POINTS * 100), 1)
    parts["_raw"] = {"points": round(raw, 1), "why": f"of {MAX_POINTS} possible"}
    return total, parts


def apply(conn) -> int:
    started = now()
    floor = float(settings()["pay"]["floor_usd_month"])
    # `fit.viable = 1` is the gate now: it already encodes "he can take it" and
    # "they would have him", including the remote requirement. Scoring a job
    # that fails either one only puts it back on a list it was removed from.
    rows = conn.execute("""
        SELECT j.id, j.title, jt.description, j.posted_at, j.first_seen,
               j.source_count, e.state, g.band, g.overlap_hours,
               s.known pay_known, s.min_usd_month lo, s.max_usd_month hi
        FROM job j
        LEFT JOIN job_text jt ON jt.job_id = j.id
        JOIN eligibility e ON e.job_id = j.id
        JOIN fit f ON f.job_id = j.id
        LEFT JOIN geo g ON g.job_id = j.id
        LEFT JOIN salary s ON s.job_id = j.id
        WHERE j.status = 'open' AND f.viable = 1
          AND e.state NOT IN ('BLOCKED', 'ONSITE_NO_SPONSOR')
    """).fetchall()

    for r in rows:
        total, breakdown = score_one(r, floor)
        conn.execute(
            "INSERT INTO score (job_id, total, breakdown, scored_at) VALUES (?,?,?,?) "
            "ON CONFLICT (job_id) DO UPDATE SET total = excluded.total, "
            "breakdown = excluded.breakdown, scored_at = excluded.scored_at",
            (r["id"], total, json.dumps(breakdown), now()))

    # A job that has become blocked, stale or non-viable keeps no score --
    # otherwise it holds its place in a sorted list long after it stopped
    # qualifying, which is the quietest way for a filter to stop working.
    conn.execute("""
        DELETE FROM score WHERE job_id IN (
            SELECT s.job_id FROM score s
            JOIN eligibility e ON e.job_id = s.job_id
            JOIN job j ON j.id = s.job_id
            LEFT JOIN fit f ON f.job_id = s.job_id
            WHERE e.state IN ('BLOCKED', 'ONSITE_NO_SPONSOR')
               OR j.status <> 'open'
               OR f.viable IS NULL OR f.viable = 0)
    """)
    conn.commit()
    log_stage(conn, "score", True, f"scored={len(rows)}", started)
    return len(rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--explain", type=int, metavar="JOB_ID")
    args = ap.parse_args(argv)

    conn = connect()

    if args.explain:
        r = conn.execute(
            "SELECT j.company, j.title, j.canonical_url, sc.total, sc.breakdown "
            "FROM job j JOIN score sc ON sc.job_id = j.id WHERE j.id = ?",
            (args.explain,)).fetchone()
        if not r:
            print(f"no score for job {args.explain}")
            return 1
        print(f"{r['company']} — {r['title']}\n{r['canonical_url']}\n")
        print(f"  TOTAL  {r['total']}\n")
        for name, part in json.loads(r["breakdown"]).items():
            print(f"  {name:<12} {part['points']:>6}   {part['why']}")
        return 0

    n = apply(conn)
    print(f"scored {n} jobs\n")
    for r in conn.execute("""
            SELECT j.id, j.company, j.title, sc.total, e.state, g.band,
                   s.known pay_known, s.max_usd_month hi
            FROM score sc JOIN job j ON j.id = sc.job_id
            JOIN eligibility e ON e.job_id = j.id
            LEFT JOIN geo g ON g.job_id = j.id
            LEFT JOIN salary s ON s.job_id = j.id
            ORDER BY sc.total DESC LIMIT ?""", (args.top,)):
        pay = f"${r['hi']:,.0f}/mo" if r["pay_known"] and r["hi"] else "—"
        print(f"  {r['total']:>5}  {(r['band'] or '?'):<6} {pay:>12}  "
              f"{r['company'][:20]:<20} {r['title'][:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
