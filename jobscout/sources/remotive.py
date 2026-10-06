#!/usr/bin/env python3
"""Remotive.

`candidate_required_location` is the useful field -- "USA", "Worldwide",
"Europe" -- and it is the board's own answer to the eligibility question rather
than something inferred from the advert body.

Salary is free text ("$14/hour", "70,000 - 90,000 USD"), so it goes to
salary.py's text ladder rather than the structured path.

**This endpoint now ignores every parameter it documents.** Verified
2026-08-24: `limit` at 20/100/1000 and absent, `category` across four different
slugs, `search`, and `company_name` all return the same twenty newest jobs --
the four category calls returned an identical set of ids, and every one of them
contained jobs from nine unrelated categories. The public API has become a
fixed "latest 20" feed whatever you ask it.

So this asks once. A category fan-out was written first and deleted: fourteen
requests for the same twenty rows is not thoroughness, it is a module that looks
busy while contributing nothing extra. Twenty fresh, well-labelled jobs a day is
still worth one call -- `candidate_required_location` is the board's own answer
to the eligibility question -- but the volume has to come from elsewhere.
"""
from __future__ import annotations

from . import Posting, epoch, get, strip_html

API = "https://remotive.com/api/remote-jobs"
NAME = "remotive"


def fetch(cfg: dict) -> list[Posting]:
    data = get(API, cfg=cfg)
    if not data:
        return []

    out = []
    for j in data.get("jobs", []):
        out.append(Posting(
            source=NAME,
            source_id=str(j.get("id")),
            url=j.get("url", ""),
            title=j.get("title", ""),
            company=j.get("company_name", ""),
            description=strip_html(j.get("description", "")),
            posted_at=epoch(j.get("publication_date")),
            location_raw=j.get("candidate_required_location", "") or "",
            employment_type=j.get("job_type"),
            apply_url=j.get("url"),
            tags=j.get("tags") or [],
            salary_raw={"text": j.get("salary") or ""},
            raw=j,
        ))
    return out
