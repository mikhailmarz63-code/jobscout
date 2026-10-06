#!/usr/bin/env python3
"""Arbeitnow -- the relocation lane.

EU-heavy and mostly on-site, which would make it useless here except for one
thing: it is the only feed that tags visa sponsorship. `ONSITE_SPONSORED` is a
state the candidate explicitly wants surfaced, and this is where those jobs are.

Its `remote` boolean is honest, which is rarer than it sounds.
"""
from __future__ import annotations

from . import Posting, epoch, get, strip_html

API = "https://www.arbeitnow.com/api/job-board-api"
NAME = "arbeitnow"


def fetch(cfg: dict) -> list[Posting]:
    conf = cfg.get("sources", {}).get(NAME, {})
    pages = int(conf.get("pages", 3))
    out: list[Posting] = []

    for page in range(1, pages + 1):
        data = get(API, params={"page": page}, cfg=cfg)
        if not data or not data.get("data"):
            break

        for j in data["data"]:
            tags = list(j.get("tags") or []) + list(j.get("job_types") or [])
            if j.get("remote"):
                tags.append("remote")
            out.append(Posting(
                source=NAME,
                source_id=str(j.get("slug")),
                url=j.get("url", ""),
                title=j.get("title", ""),
                company=j.get("company_name", ""),
                description=strip_html(j.get("description", "")),
                posted_at=epoch(j.get("created_at")),
                location_raw=("Remote" if j.get("remote") else "") or j.get("location", ""),
                apply_url=j.get("url"),
                tags=tags,
                salary_raw={},
                raw=j,
            ))
    return out
