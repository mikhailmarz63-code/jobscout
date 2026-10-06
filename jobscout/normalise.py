#!/usr/bin/env python3
"""Stage 2: postings -> jobs. The same role on six boards becomes one row.

Deduplication matters here more than it looks. Without it the daily list is
mostly the same twenty jobs wearing different hats, and the pay floor gets
applied six times to six copies of one advert with six different salary strings.
With it, `source_count` becomes a signal in its own right: a role syndicated to
six boards is being pushed hard, which usually means it has been open a while.

The key is normalised company + normalised title. Two decisions inside that are
deliberate and easy to get wrong:

  * Legal suffixes are stripped from company names ("Acme Inc" and "Acme" are
    one employer) but nothing else is. "Acme Labs" is not "Acme".
  * From titles, only *remote and gender markers* are stripped -- "(Remote)",
    "(m/f/d)", "- Remote". Everything after a dash is kept, because "Senior
    Engineer - Backend" and "Senior Engineer - Frontend" are two jobs, and
    collapsing them would silently hide one.

Re-runnable. Rebuilding is safe: `job` rows are rebuilt from postings, and the
tables that hang off a job (eligibility, salary, score) survive because the
dedupe key is stable.

    python3 normalise.py
    python3 normalise.py --stale-days 45
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402

LEGAL_SUFFIXES = r"(inc|llc|ltd|limited|corp|corporation|co|gmbh|b\.?v|s\.?a|plc|" \
                 r"pty|pvt|private|ag|ab|oy|as|srl|sas|kk|llp|lp|group|holdings)"

# Only these leave a title. Anything else that looks like noise is somebody's
# real job title somewhere.
TITLE_NOISE = [
    r"\(\s*remote[^)]*\)", r"\[\s*remote[^\]]*\]", r"\bremote\b\s*[-–—]\s*",
    r"[-–—]\s*remote\b", r"\(\s*[mfdwx](\s*/\s*[mfdwx])+\s*\)",
    r"\(\s*all genders?\s*\)", r"\(\s*hybrid\s*\)", r"\(\s*full[- ]time\s*\)",
    r"\(\s*contract\s*\)", r"\bw/m/d\b", r"\bm/w/d\b", r"\bf/m/d\b",
]

# An ATS URL is the employer's own page. Prefer it as the canonical link over an
# aggregator's redirect, which expires and sometimes tracks.
SOURCE_RANK = {"ats": 0, "himalayas": 1, "weworkremotely": 2, "remoteok": 3,
               "remotive": 4, "jobicy": 5, "arbeitnow": 6, "hackernews": 7}


def slug_company(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(rf"\b{LEGAL_SUFFIXES}\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def slug_title(title: str) -> str:
    s = (title or "").lower().strip()
    for pattern in TITLE_NOISE:
        s = re.sub(pattern, " ", s, flags=re.I)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def dedupe_key(company: str, title: str) -> str:
    """A posting with no company (HN comments that ignore the convention) keys
    on its title alone. That is weaker, and it is the honest answer -- guessing
    a company to strengthen the key would merge two unrelated jobs."""
    return f"{slug_company(company)}|{slug_title(title)}"


def build(conn, stale_days: int = 60) -> dict:
    started = now()
    rows = conn.execute(
        "SELECT id, source, source_id, url, title, company, description, "
        "       posted_at, first_seen, last_seen, apply_url "
        "FROM posting ORDER BY id").fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        key = dedupe_key(r["company"], r["title"])
        if key == "|":
            continue                     # neither company nor title survived
        groups.setdefault(key, []).append(r)

    for key, members in groups.items():
        best = min(members, key=lambda m: (SOURCE_RANK.get(m["source"], 9),
                                           -len(m["description"] or "")))
        # The body itself is not copied: the `job_text` view serves the longest
        # posting description per job, and copying it here doubled the file.
        posted = [m["posted_at"] for m in members if m["posted_at"]]

        conn.execute("""
            INSERT INTO job (dedupe_key, title, company, company_slug, canonical_url,
                             description, posted_at, first_seen, last_seen, source_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key) DO UPDATE SET
                title         = excluded.title,
                company       = excluded.company,
                canonical_url = excluded.canonical_url,
                posted_at     = COALESCE(MIN(job.posted_at, excluded.posted_at),
                                         excluded.posted_at, job.posted_at),
                first_seen    = MIN(job.first_seen, excluded.first_seen),
                last_seen     = MAX(job.last_seen, excluded.last_seen),
                source_count  = excluded.source_count
        """, (key, best["title"].strip(), best["company"].strip(),
              slug_company(best["company"]), best["url"],
              None, min(posted) if posted else None,
              min(m["first_seen"] for m in members),
              max(m["last_seen"] for m in members),
              len({m["source"] for m in members})))

        job_id = conn.execute("SELECT id FROM job WHERE dedupe_key = ?",
                              (key,)).fetchone()["id"]
        conn.executemany("UPDATE posting SET job_id = ? WHERE id = ?",
                         [(job_id, m["id"]) for m in members])

    # A job nobody has re-listed in `stale_days` is almost certainly filled.
    # Marked, never deleted -- the application history hangs off it.
    cutoff = now() - stale_days * 86400
    conn.execute("UPDATE job SET status = 'stale' "
                 "WHERE last_seen < ? AND status = 'open'", (cutoff,))
    conn.execute("UPDATE job SET status = 'open' "
                 "WHERE last_seen >= ? AND status = 'stale'", (cutoff,))
    conn.commit()

    stats = {
        "postings": len(rows),
        "jobs": conn.execute("SELECT COUNT(*) c FROM job").fetchone()["c"],
        "open": conn.execute("SELECT COUNT(*) c FROM job WHERE status = 'open'").fetchone()["c"],
        "multi": conn.execute("SELECT COUNT(*) c FROM job WHERE source_count > 1").fetchone()["c"],
    }
    log_stage(conn, "normalise", True, str(stats), started)
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stale-days", type=int, default=60)
    args = ap.parse_args(argv)

    conn = connect()
    s = build(conn, args.stale_days)
    collapsed = s["postings"] - s["jobs"]
    print(f"{s['postings']} postings -> {s['jobs']} jobs "
          f"({collapsed} duplicates collapsed)")
    print(f"{s['open']} open, {s['multi']} listed on more than one board")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
