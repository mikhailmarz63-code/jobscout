#!/usr/bin/env python3
"""Hacker News "Ask HN: Who is hiring?" -- the monthly thread.

Worth the parsing pain for one reason: a large share of genuinely
hire-anywhere, well-paid engineering roles are posted here and *nowhere else*.
They never reach a job board, so no aggregator above can see them.

The cost is that a comment is free text. The community convention is a first
line of pipe-separated fields --

    Company | Role | Location | REMOTE (WORLDWIDE) | $120k-160k | apply@co.com

-- which most posters follow and some ignore. This module parses the convention
where it holds and, where it does not, still returns the comment with its whole
body intact. A posting whose company could not be identified is returned with an
empty company rather than a guessed one: `eligibility.py` and `salary.py` read
the body either way, and an invented company name would end up in a cover
letter's salutation.

Structured extraction here is deliberately shallow. The body is the evidence,
and everything downstream reads the body.
"""
from __future__ import annotations

import re

from . import Posting, epoch, get, strip_html

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items"
NAME = "hackernews"

# An email in the body is the whole point of this source: it is the one feed
# where an application address is routinely published.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _latest_thread(cfg: dict) -> dict | None:
    """The most recent 'Who is hiring?' story. The same account also posts
    'Who wants to be hired?' every month, which is the opposite thread and must
    not be ingested as jobs."""
    data = get(SEARCH, cfg=cfg, params={
        "tags": "story,author_whoishiring", "hitsPerPage": 10})
    if not data:
        return None
    for hit in data.get("hits", []):
        if "who is hiring" in (hit.get("title") or "").lower():
            return hit
    return None


def _parse_header(line: str) -> tuple[str, str, str]:
    """(company, role, location) from the pipe convention. Empty strings where
    the convention was not followed -- never a guess."""
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if len(parts) < 2:
        return "", line.strip()[:120], ""
    company = parts[0]
    role = parts[1]
    location = " | ".join(parts[2:4]) if len(parts) > 2 else ""
    return company, role, location


def fetch(cfg: dict) -> list[Posting]:
    thread = _latest_thread(cfg)
    if not thread:
        return []

    story_id = thread.get("objectID")
    item = get(f"{ITEM}/{story_id}", cfg=cfg)
    if not item:
        return []

    out = []
    for child in item.get("children", []):
        # Deleted comments arrive with a null author and no text.
        if not child.get("text") or not child.get("author"):
            continue
        body = strip_html(child["text"])
        if len(body) < 40:          # a reply, not a job post
            continue

        first_line = next((l for l in body.splitlines() if l.strip()), "")
        company, role, location = _parse_header(first_line)
        email = EMAIL_RE.search(body)
        cid = str(child.get("id"))

        out.append(Posting(
            source=NAME,
            source_id=cid,
            url=f"https://news.ycombinator.com/item?id={cid}",
            title=role or first_line[:120],
            company=company,
            description=body,
            posted_at=epoch(child.get("created_at_i") or child.get("created_at")),
            location_raw=location,
            apply_url=f"https://news.ycombinator.com/item?id={cid}",
            apply_email=email.group(0) if email else None,
            tags=["hn", thread.get("title", "")],
            # Deliberately empty. The pay is somewhere in the body, but so is
            # every funding round and ARR figure the poster wanted to boast
            # about -- "$750k" in one of these turned out to be a seed round and
            # ranked as a $62,500/month job. Passing the body as a *description*
            # instead makes salary.py demand a salary cue beside the number.
            salary_raw={},
            raw={"comment_id": cid, "author": child.get("author"),
                 "thread": thread.get("title"), "thread_id": story_id},
        ))
    return out
