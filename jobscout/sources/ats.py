#!/usr/bin/env python3
"""Company job boards, straight from the ATS -- the register layer.

The seven aggregators above are somebody else's index of the market. This is
the market itself: Greenhouse, Ashby, Lever, Workable and SmartRecruiters all
publish an unauthenticated JSON endpoint per company, with no key, no quota and
no rate limit worth the name. A role appears here the moment it opens, which is
often days before it is syndicated anywhere, and some never syndicate at all.

`companies.yml` is the universe. It starts small and grows -- `--discover`
probes every company already seen in the posting table against all five ATSs and
reports the ones that answer, so the list is extended from evidence rather than
from a guess about who uses what.

    python3 -m sources.ats --check          # every slug in companies.yml
    python3 -m sources.ats --discover 40    # find slugs for companies already seen
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

import yaml

from . import Posting, epoch, get, strip_html

NAME = "ats"
HERE = Path(__file__).parent
COMPANIES = HERE.parent / "companies.yml"

sys.path.insert(0, str(HERE.parent))
from db import log_stage, now                    # noqa: E402

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
}

# Extra query args that make each board return the description and the pay.
# Without them Greenhouse returns titles only and Ashby omits compensation.
# Discovery only needs to know whether a board exists and has anything on it.
# The full-content parameters below turn a 6 KB answer into a 3 MB one, which
# is the difference between sweeping 1,157 companies in minutes and in hours.
PROBE_PARAMS = {
    "greenhouse": {},
    "ashby": {},
    "lever": {"mode": "json", "limit": "1"},
    "workable": {},
    "smartrecruiters": {"limit": "1"},
}

PARAMS = {
    "greenhouse": {"content": "true"},
    "ashby": {"includeCompensation": "true"},
    "lever": {"mode": "json"},
    "workable": {"details": "true"},
    "smartrecruiters": {"limit": "100"},
}


def _entries() -> list[dict]:
    if not COMPANIES.exists():
        return []
    data = yaml.safe_load(COMPANIES.read_text()) or {}
    return data.get("companies", []) or []


def _url(ats: str, slug: str) -> str:
    return ENDPOINTS[ats].format(slug=slug)


def _jobs(ats: str, payload) -> list:
    """Each ATS buries the array somewhere different."""
    if payload is None:
        return []
    if ats == "lever":
        return payload if isinstance(payload, list) else []
    if ats == "smartrecruiters":
        return payload.get("content", []) if isinstance(payload, dict) else []
    return payload.get("jobs", []) if isinstance(payload, dict) else []


# --------------------------------------------------------------- adapters --
# One per ATS. Each returns a Posting or None; none of them interpret.

def _greenhouse(j: dict, company: str) -> Posting | None:
    if not j.get("id"):
        return None
    loc = (j.get("location") or {}).get("name", "")
    offices = "; ".join(o.get("name", "") for o in (j.get("offices") or []) if o.get("name"))
    return Posting(
        source=NAME, source_id=f"greenhouse:{j['id']}",
        url=j.get("absolute_url", ""), title=j.get("title", ""),
        company=j.get("company_name") or company,
        # Greenhouse double-encodes: the content field is HTML inside an
        # HTML-escaped string, so it needs unescaping before tag stripping.
        description=strip_html(html.unescape(j.get("content") or "")),
        posted_at=epoch(j.get("first_published") or j.get("updated_at")),
        location_raw="; ".join(x for x in (loc, offices) if x),
        apply_url=j.get("absolute_url"),
        tags=[d.get("name", "") for d in (j.get("departments") or [])],
        salary_raw={}, raw={k: v for k, v in j.items() if k != "content"},
    )


def _ashby(j: dict, company: str) -> Posting | None:
    if not j.get("id") or j.get("isListed") is False:
        return None
    # secondaryLocations is where "Remote (Canada), Remote (US)" lives, and it
    # is the difference between a job that is open to him and one that is not.
    secondary = "; ".join(
        s.get("location", "") for s in (j.get("secondaryLocations") or [])
        if isinstance(s, dict) and s.get("location"))
    comp = j.get("compensation") or {}
    return Posting(
        source=NAME, source_id=f"ashby:{j['id']}",
        url=j.get("jobUrl", ""), title=(j.get("title") or "").strip(),
        company=company,
        description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""),
        posted_at=epoch(j.get("publishedAt")),
        location_raw="; ".join(x for x in (
            j.get("location", ""), secondary,
            "Remote" if j.get("isRemote") else "", j.get("workplaceType", "")) if x),
        employment_type=j.get("employmentType"),
        apply_url=j.get("applyUrl") or j.get("jobUrl"),
        tags=[x for x in (j.get("department"), j.get("team")) if x],
        salary_raw={"text": comp.get("scrapeableCompensationSalarySummary")
                            or comp.get("compensationTierSummary") or ""},
        raw={k: v for k, v in j.items()
             if k not in ("descriptionHtml", "descriptionPlain")},
    )


def _lever(j: dict, company: str) -> Posting | None:
    if not j.get("id"):
        return None
    cat = j.get("categories") or {}
    return Posting(
        source=NAME, source_id=f"lever:{j['id']}",
        url=j.get("hostedUrl", ""), title=j.get("text", ""), company=company,
        description=j.get("descriptionPlain") or strip_html(j.get("description") or ""),
        posted_at=epoch(j.get("createdAt")),
        location_raw="; ".join(x for x in (cat.get("location"),
                                           cat.get("allLocations") and
                                           "; ".join(cat["allLocations"])) if x),
        employment_type=cat.get("commitment"),
        apply_url=j.get("applyUrl") or j.get("hostedUrl"),
        tags=[x for x in (cat.get("team"), cat.get("department")) if x],
        salary_raw={"text": (j.get("salaryRange") or {}).get("min") and
                            json.dumps(j.get("salaryRange")) or ""},
        raw={k: v for k, v in j.items() if k not in ("description", "descriptionPlain")},
    )


def _workable(j: dict, company: str) -> Posting | None:
    if not j.get("shortcode"):
        return None
    return Posting(
        source=NAME, source_id=f"workable:{j['shortcode']}",
        url=j.get("url", ""), title=j.get("title", ""), company=company,
        description=strip_html(j.get("description") or ""),
        posted_at=epoch(j.get("published_on") or j.get("created_at")),
        location_raw="; ".join(x for x in (
            j.get("location", {}).get("city", "") if isinstance(j.get("location"), dict) else "",
            j.get("location", {}).get("country", "") if isinstance(j.get("location"), dict) else "",
            "Remote" if j.get("telecommuting") else "") if x),
        employment_type=j.get("employment_type"),
        apply_url=j.get("application_url") or j.get("url"),
        tags=[j.get("department")] if j.get("department") else [],
        salary_raw={}, raw=j,
    )


def _smartrecruiters(j: dict, company: str) -> Posting | None:
    if not j.get("id"):
        return None
    loc = j.get("location") or {}
    return Posting(
        source=NAME, source_id=f"smartrecruiters:{j['id']}",
        url=(j.get("ref") or ""), title=j.get("name", ""), company=company,
        description="",   # the list endpoint omits it; the detail call is per-job
        posted_at=epoch(j.get("releasedDate")),
        location_raw="; ".join(str(x) for x in (loc.get("city"), loc.get("country"),
                                                "Remote" if loc.get("remote") else "") if x),
        employment_type=(j.get("typeOfEmployment") or {}).get("label"),
        apply_url=j.get("applyUrl") or j.get("ref"),
        tags=[], salary_raw={}, raw=j,
    )


ADAPTERS = {
    "greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever,
    "workable": _workable, "smartrecruiters": _smartrecruiters,
}


def _rotate(conn, boards: list[dict], per_run: int) -> list[dict]:
    """The `per_run` boards least recently fetched, and stamp them as fetched.

    companies.yml entries have no row in `board` and so sort oldest-first --
    which is right: the hand-curated list is the one somebody chose, and it
    should be checked every time.
    """
    last = {(r["ats"], r["slug"]): r["checked_at"] for r in
            conn.execute("SELECT ats, slug, checked_at FROM board")}
    boards.sort(key=lambda e: last.get((e.get("ats"), e.get("slug")), 0))
    picked = boards[:per_run]
    stamp = now()
    conn.executemany(
        "UPDATE board SET checked_at = ? WHERE ats = ? AND slug = ?",
        [(stamp, e.get("ats"), e.get("slug")) for e in picked])
    conn.commit()
    return picked


def all_boards(conn=None) -> list[dict]:
    """companies.yml first, then whatever discovery found.

    The file stays hand-curated -- it is the list somebody chose. The table is
    the list a machine found, and mixing the two would make it impossible to
    tell which was which when one of them turns out to be wrong.
    """
    entries = list(_entries())
    if conn is None:
        return entries
    seen = {(e.get("ats"), e.get("slug")) for e in entries}
    for r in conn.execute(
            "SELECT company, slug, ats FROM board WHERE live = 1 "
            "ORDER BY jobs_seen DESC"):
        if (r["ats"], r["slug"]) not in seen:
            entries.append({"name": r["company"], "ats": r["ats"],
                            "slug": r["slug"], "discovered": True})
    return entries


def fetch(cfg: dict, conn=None) -> list[Posting]:
    """A rotating slice of the known boards, not all of them every day.

    Fetching all 248 boards with full job content took 456 seconds and 25,000
    postings, and that number grows with every board discovery finds -- at the
    full 1,157-employer sweep it would be the whole run. But a company's board
    does not change much between Tuesday and Wednesday, so re-reading every one
    daily buys almost nothing.

    So boards rotate: a bounded batch per run, oldest-checked first, cycling
    through the lot every few days. New jobs at a company are at most a couple
    of days late, which for a board nobody else is watching is still far ahead
    of the aggregators.
    """
    conf = cfg.get("sources", {}).get(NAME, {})
    per_run = int(conf.get("boards_per_run", 80))

    boards = all_boards(conn)
    if conn is not None and len(boards) > per_run:
        boards = _rotate(conn, boards, per_run)

    out: list[Posting] = []
    for entry in boards:
        ats, slug = entry.get("ats"), entry.get("slug")
        name = entry.get("name") or slug
        if not ats or not slug or ats not in ENDPOINTS or entry.get("enabled") is False:
            continue
        payload = get(_url(ats, slug), params=PARAMS.get(ats), cfg=cfg)
        for j in _jobs(ats, payload):
            if not isinstance(j, dict):
                continue
            try:
                p = ADAPTERS[ats](j, name)
            except (KeyError, TypeError, AttributeError):
                p = None      # one malformed record must not lose the other 200
            if p and p.title:
                out.append(p)
    return out


# ------------------------------------------------------------------- CLI --

def _slugify(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9]+", "", name.lower())
    hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return list(dict.fromkeys([base, hyphen]))


def _probe(cfg: dict, slug: str) -> tuple[str, int] | None:
    """Try all five providers at once.

    Sequentially this cost about four seconds per company -- five providers,
    each waiting a full politeness gap behind the last, even though they are
    five unrelated companies' servers. Run concurrently it is about one second,
    because the rate limiter is per host: each provider still gets its own gap,
    they simply stop queueing behind each other. 1,157 employers goes from
    roughly 80 minutes to 20.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(ats: str):
        jobs = _jobs(ats, get(_url(ats, slug), params=PROBE_PARAMS.get(ats), cfg=cfg))
        return (ats, len(jobs)) if jobs else None

    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        results = list(pool.map(one, ENDPOINTS))
    # Deterministic winner: ENDPOINTS order, not whichever answered first.
    for hit in results:
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- discovery --

def candidates(conn, limit: int) -> list[str]:
    """Employers seen in the postings that have never been probed.

    Ordered by how many postings they have, because a company that advertises
    ten roles is more likely to have a board worth watching than one that
    advertised once and vanished.
    """
    return [r["company"] for r in conn.execute("""
        SELECT p.company, COUNT(*) n FROM posting p
        WHERE p.company <> ''
          AND lower(p.company) NOT IN (SELECT lower(company) FROM board)
          AND lower(p.company) NOT IN (SELECT lower(company) FROM board_miss)
        GROUP BY lower(p.company)
        ORDER BY n DESC, p.company
        LIMIT ?""", (limit,))]


def discover(conn, cfg: dict, limit: int = 200) -> dict:
    """Probe a bounded batch and record both outcomes.

    Recording the *misses* is what makes this affordable to run daily: without
    it, tomorrow's run re-probes the same eleven hundred companies that
    answered 404 today, forever.
    """
    import sqlite3 as _sqlite3

    started = now()
    found = missed = 0
    for name in candidates(conn, limit):
      try:
        hit = None
        for slug in _slugify(name):
            hit = _probe(cfg, slug)
            if hit:
                ats, count = hit
                conn.execute(
                    "INSERT INTO board (company, slug, ats, jobs_seen, live, "
                    "found_at, checked_at) VALUES (?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT (ats, slug) DO UPDATE SET "
                    "jobs_seen = excluded.jobs_seen, checked_at = excluded.checked_at",
                    (name, slug, ats, count, now(), now()))
                found += 1
                break
        if not hit:
            conn.execute("INSERT OR REPLACE INTO board_miss VALUES (?, ?)",
                         (name, now()))
            missed += 1
        conn.commit()
      except _sqlite3.OperationalError:
        # A sweep that dies on one contended write throws away every probe it
        # already paid for. Skip the employer; the next run finds it again,
        # because a company with no row in either table is still a candidate.
        continue

    stats = {"found": found, "missed": missed, "probed": found + missed}
    log_stage(conn, "ats:discover", True, json.dumps(stats), started)
    return stats


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(HERE.parent))
    from . import settings
    cfg = settings()

    if "--check" in argv:
        for e in _entries():
            payload = get(_url(e["ats"], e["slug"]), params=PARAMS.get(e["ats"]), cfg=cfg)
            n = len(_jobs(e["ats"], payload))
            print(f"{e['name']:<24} {e['ats']:<16} {e['slug']:<20} {n:>4} jobs"
                  f"{'   <-- EMPTY' if n == 0 else ''}")
        return 0

    if "--discover" in argv:
        idx = argv.index("--discover")
        limit = int(argv[idx + 1]) if len(argv) > idx + 1 and argv[idx + 1].isdigit() else 200
        from db import connect
        conn = connect()
        remaining = conn.execute("""
            SELECT COUNT(*) c FROM (SELECT lower(company) FROM posting
              WHERE company <> ''
                AND lower(company) NOT IN (SELECT lower(company) FROM board)
                AND lower(company) NOT IN (SELECT lower(company) FROM board_miss)
              GROUP BY lower(company))""").fetchone()["c"]
        print(f"  {remaining} employers still unprobed; doing {min(limit, remaining)}")
        stats = discover(conn, cfg, limit)
        live = conn.execute("SELECT COUNT(*) c FROM board WHERE live = 1").fetchone()["c"]
        print(f"  found {stats['found']}, missed {stats['missed']} "
              f"— {live} live board(s) known")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
