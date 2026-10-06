#!/usr/bin/env python3
"""RemoteOK.

Two traps in this feed, both handled here rather than downstream:

  * The first element of the array is not a job. It is a legal notice carrying
    the API's terms. Anything that iterates the array naively ingests a posting
    called None at company None.
  * `salary_min` and `salary_max` are `0` when the salary is not stated. Zero is
    not a salary. It is passed through as a raw value and salary.py is what
    decides 0 means unknown -- but the trap is documented at both ends, because
    reading it literally would rank every unpaid-looking job last and quietly
    bury a third of the feed.

Their terms ask for attribution and a followed link back when listings are
shown publicly. This board is localhost-only and personal, so that does not
arise; if it is ever published, the link is owed.
"""
from __future__ import annotations

from . import Posting, epoch, get, strip_html

API = "https://remoteok.com/api"
NAME = "remoteok"


def fetch(cfg: dict) -> list[Posting]:
    data = get(API, cfg=cfg)
    if not isinstance(data, list):
        return []

    out = []
    for j in data:
        # The legal notice, and anything else malformed.
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue
        out.append(Posting(
            source=NAME,
            source_id=str(j.get("id")),
            url=j.get("url", ""),
            title=j.get("position", ""),
            company=j.get("company", ""),
            description=strip_html(j.get("description", "")),
            posted_at=epoch(j.get("epoch") or j.get("date")),
            location_raw=j.get("location", "") or "",
            apply_url=j.get("apply_url") or j.get("url"),
            tags=j.get("tags") or [],
            salary_raw={"min": j.get("salary_min"), "max": j.get("salary_max"),
                        "currency": "USD", "period": "year"},
            raw=j,
        ))
    return out
