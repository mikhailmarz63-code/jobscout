#!/usr/bin/env python3
"""We Work Remotely, over RSS.

No salary field at all, but it carries `<region>` -- and "Anywhere in the World"
is the single cleanest eligibility signal any of these eight feeds produces.
That alone earns its place.

Parsed with the standard library. `feedparser` is not installed and this is
forty lines; adding a dependency to save fifteen of them is the wrong trade.
Titles arrive as "Company: Role" and are split back apart here.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from . import Posting, epoch, get, strip_html

FEED = "https://weworkremotely.com/remote-jobs.rss"
NAME = "weworkremotely"


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def fetch(cfg: dict) -> list[Posting]:
    xml = get(FEED, cfg=cfg, as_json=False)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    out = []
    for item in root.findall("channel/item"):
        title = _text(item, "title")
        company, _, role = title.partition(":")
        # No colon means no company prefix -- keep the whole thing as the role
        # rather than silently filing the job under a truncated company name.
        if not role:
            company, role = "", title

        link = _text(item, "link") or _text(item, "guid")
        region = _text(item, "region")
        country = _text(item, "country")

        out.append(Posting(
            source=NAME,
            source_id=link,
            url=link,
            title=role.strip(),
            company=company.strip(),
            description=strip_html(_text(item, "description")),
            posted_at=epoch(_text(item, "pubDate")),
            location_raw="; ".join(x for x in (region, country) if x),
            employment_type=_text(item, "type"),
            apply_url=link,
            tags=[t for t in (_text(item, "category"), _text(item, "skills")) if t],
            salary_raw={},
            raw={"title": title, "region": region, "country": country,
                 "category": _text(item, "category"), "type": _text(item, "type")},
        ))
    return out
