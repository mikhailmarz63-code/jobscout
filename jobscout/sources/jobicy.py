#!/usr/bin/env python3
"""Jobicy. Structured salary, and `jobGeo` as the board's own location answer.

Their notice asks for credit with a direct link where listings are shown. Same
position as RemoteOK: localhost-only and personal, so it does not arise yet.
"""
from __future__ import annotations

from . import Posting, epoch, get, strip_html

API = "https://jobicy.com/api/v2/remote-jobs"
NAME = "jobicy"


def fetch(cfg: dict) -> list[Posting]:
    data = get(API, params={"count": 50}, cfg=cfg)
    if not data:
        return []

    out = []
    for j in data.get("jobs", []):
        out.append(Posting(
            source=NAME,
            source_id=str(j.get("id")),
            url=j.get("url", ""),
            title=j.get("jobTitle", ""),
            company=j.get("companyName", ""),
            description=strip_html(j.get("jobDescription") or j.get("jobExcerpt") or ""),
            posted_at=epoch(j.get("pubDate")),
            location_raw=j.get("jobGeo", "") or "",
            employment_type=", ".join(j.get("jobType") or [])
                            if isinstance(j.get("jobType"), list) else j.get("jobType"),
            seniority=j.get("jobLevel"),
            apply_url=j.get("url"),
            tags=j.get("jobIndustry") or [],
            salary_raw={"min": j.get("salaryMin"), "max": j.get("salaryMax"),
                        "currency": j.get("salaryCurrency"),
                        "period": j.get("salaryPeriod")},
            raw=j,
        ))
    return out
