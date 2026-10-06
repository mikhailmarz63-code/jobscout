#!/usr/bin/env python3
"""Himalayas -- the best source of the eight, by a distance.

It is the only board that publishes `locationRestrictions` AND
`timezoneRestrictions` AND a structured salary range as separate fields. Those
three answer, without inference, the three questions the whole system exists to
ask: can he take it, will it wreck his sleep, does it pay. Every other source
needs text parsing to get halfway there.

Cursor pagination, per the API's own note (21/08/2026). It serves **20 jobs per
page whatever `limit` says** -- verified 2026-08-24 -- against a total feed of
~104,000, so the page count in settings.yml is the real volume dial. The feed is
newest-first, which is what makes a bounded page count honest rather than
arbitrary: thirty pages is roughly the last day or two of postings, and the next
run picks up what has appeared since.
"""
from __future__ import annotations

from . import Posting, cursor_get, cursor_set, epoch, get, strip_html

API = "https://himalayas.app/jobs/api"
NAME = "himalayas"


def fetch(cfg: dict, conn=None) -> list[Posting]:
    """Resumes from yesterday's cursor rather than re-walking the newest pages.

    The feed is ~103,000 jobs at 20 a page. Fetching the newest 30 pages every
    morning collected the same 600 jobs forever, and the other 102,400 were
    unreachable — not because the API refused, but because nothing ever asked
    for page 31. The cursor now lives in `source_state`.

    When the cursor runs out the source is marked exhausted and the next run
    starts again from the top, which is also where the genuinely new jobs are.
    """
    conf = cfg.get("sources", {}).get(NAME, {})
    pages = int(conf.get("pages", 5))
    out: list[Posting] = []

    cursor, page_no, exhausted = (None, 0, False)
    if conn is not None:
        cursor, page_no, exhausted = cursor_get(conn, NAME)
        if exhausted:
            cursor, page_no = None, 0     # back to the newest, for today's

    for _ in range(pages):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = get(API, params=params, cfg=cfg)
        if not data or not data.get("jobs"):
            break

        for j in data["jobs"]:
            out.append(Posting(
                source=NAME,
                source_id=str(j.get("guid") or j.get("applicationLink") or j.get("title")),
                url=j.get("applicationLink") or j.get("guid") or "",
                title=j.get("title", ""),
                company=j.get("companyName", ""),
                description=strip_html(j.get("description") or j.get("excerpt") or ""),
                posted_at=epoch(j.get("pubDate")),
                # A list, and the list is the point -- ['United States'] and
                # ['Anywhere'] are opposite answers to the eligibility question.
                location_raw="; ".join(j.get("locationRestrictions") or []),
                employment_type=j.get("employmentType"),
                seniority="; ".join(j.get("seniority") or []),
                apply_url=j.get("applicationLink"),
                tags=(j.get("categories") or []) + (j.get("parentCategories") or []),
                timezones=[float(t) for t in (j.get("timezoneRestrictions") or [])
                           if isinstance(t, (int, float))],
                salary_raw={
                    "min": j.get("minSalary"),
                    "max": j.get("maxSalary"),
                    "currency": j.get("currency"),
                    "period": j.get("salaryPeriod"),
                },
                raw=j,
            ))

        cursor = data.get("nextCursor")
        page_no += 1
        if not cursor:
            if conn is not None:
                cursor_set(conn, NAME, None, 0, exhausted=True)
            break
    else:
        if conn is not None:
            cursor_set(conn, NAME, cursor, page_no)

    return out
