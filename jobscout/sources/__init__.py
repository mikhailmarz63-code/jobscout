#!/usr/bin/env python3
"""The source layer. Every source is a module exposing `fetch(cfg) -> list[Posting]`
and nothing else, so adding a board is one file and one registry line.

A source's only job is to return what the board actually said, converted into
the common shape and otherwise untouched. Interpretation -- what the salary
means, whether the candidate can take the job -- happens downstream in `salary.py` and
`eligibility.py`, where it is testable in isolation and where a wrong rule can
be fixed without re-fetching anything.

`raw` carries the entire original record. When a source changes shape, the
evidence of what it used to send is still on disk.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent
ROOT = HERE.parent
SETTINGS = ROOT / "settings.yml"
if not SETTINGS.exists():           # fresh clone: run on the example until you copy it
    SETTINGS = ROOT / "settings.example.yml"

# Politeness is per *host*, not global.
#
# One shared gap meant that hitting Greenhouse, then Ashby, then Lever — three
# unrelated companies' servers — waited a second between each, as though they
# were one overloaded machine. That is not politeness, it is just slow: it made
# a five-provider probe take five seconds when it should take one, and board
# discovery across 1,157 employers unaffordable in a daily run. Each host still
# gets its full gap; they simply no longer queue behind each other.
_last_request_at: dict[str, float] = {}
_rate_lock = threading.Lock()


@dataclass
class Posting:
    """One posting, as one board described it."""
    source: str
    source_id: str
    url: str
    title: str
    company: str
    description: str = ""
    posted_at: int | None = None
    location_raw: str = ""
    employment_type: str | None = None
    seniority: str | None = None
    apply_url: str | None = None
    apply_email: str | None = None
    tags: list[str] = field(default_factory=list)
    timezones: list[float] = field(default_factory=list)
    # Structured salary straight from the source, untouched. salary.py owns the
    # interpretation; a source that guesses here would hide its own uncertainty.
    salary_raw: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def as_row(self, seen: int) -> tuple:
        return (
            self.source, str(self.source_id), self.url, self.title.strip(),
            self.company.strip(), self.description, self.posted_at,
            self.location_raw, self.employment_type, self.seniority,
            self.apply_url, self.apply_email,
            json.dumps(self.tags), json.dumps(self.timezones),
            json.dumps({"salary_raw": self.salary_raw,
                        "record": trim_raw(self.raw)}, default=str),
            seen, seen,
        )


def settings() -> dict:
    return yaml.safe_load(SETTINGS.read_text())


def get(url: str, *, params: dict | None = None, cfg: dict | None = None,
        as_json: bool = True, json_body: dict | None = None,
        binary: bool = False):
    """One polite GET. Every source uses this, so the user agent, the timeout
    and the gap between requests are set in one place and cannot drift.

    `binary=True` returns the raw response bytes (`r.content`) instead of text
    or parsed JSON -- for openjobs.py's centroid file, where `.text` would
    decode the bytes through a guessed charset and corrupt them.

    Returns None on any failure. A source that cannot be reached must not take
    the run down -- the other seven still have jobs in them.
    """
    http = (cfg or settings()).get("http", {})
    gap = float(http.get("politeness_seconds", 1.0))
    from urllib.parse import urlparse as _urlparse
    host = _urlparse(url).netloc or url
    # Claim this host's next slot under the lock, then sleep outside it, so
    # concurrent probes of *different* hosts never queue behind each other and
    # concurrent probes of the *same* host still take their turn.
    with _rate_lock:
        earliest = _last_request_at.get(host, 0.0) + gap
        wait = max(0.0, earliest - time.time())
        _last_request_at[host] = max(earliest, time.time())
    if wait:
        time.sleep(wait)
    headers = {"User-Agent": http.get("user_agent", "jobscout/0.1"),
               "Accept": "application/json, text/xml, */*"}
    try:
        # `json_body` makes it a POST. Ashby publishes its application form
        # only through a GraphQL endpoint, which will not answer a GET. It goes
        # through this function anyway so the politeness gap, the user agent
        # and the timeout stay in one place -- a second HTTP path would drift.
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            r = requests.post(url, params=params, json=json_body,
                              timeout=int(http.get("timeout", 30)),
                              headers=headers)
        else:
            r = requests.get(
                url, params=params, timeout=int(http.get("timeout", 30)),
                headers=headers,
            )
        if r.status_code != 200:
            return None
        if binary:
            return r.content
        return r.json() if as_json else r.text
    except (requests.RequestException, ValueError):
        return None


def cursor_get(conn, source: str) -> tuple[str | None, int, bool]:
    """(cursor, page, exhausted) for a source, or a fresh start."""
    row = conn.execute("SELECT cursor, page, exhausted FROM source_state "
                       "WHERE source = ?", (source,)).fetchone()
    if not row:
        return None, 0, False
    return row["cursor"], row["page"], bool(row["exhausted"])


def cursor_set(conn, source: str, cursor: str | None, page: int,
               exhausted: bool = False) -> None:
    import time as _time
    conn.execute(
        "INSERT INTO source_state (source, cursor, page, exhausted, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source) DO UPDATE SET "
        "cursor = excluded.cursor, page = excluded.page, "
        "exhausted = excluded.exhausted, updated_at = excluded.updated_at",
        (source, cursor, page, 1 if exhausted else 0, int(_time.time())))
    conn.commit()


# What of a source's original record is worth keeping.
#
# `posting.raw` was the entire JSON at ~14 KB apiece, which at 2,187 postings is
# most of the database and at 20,000 would be the whole growth curve. The
# pipeline only ever reads the salary block out of it; the rest was kept on the
# principle that evidence should survive, which is right, but the description
# and every other field it needs already have their own columns.
RAW_KEEP = (
    "min", "max", "currency", "salaryPeriod", "salary", "salary_min",
    "salary_max", "compensation", "seniority", "employmentType", "job_type",
    "jobLevel", "remote", "isRemote", "workplaceType", "locationRestrictions",
    "timezoneRestrictions", "candidate_required_location", "jobGeo", "region",
    "country", "location", "tags", "categories", "departments",
)


def trim_raw(record: dict, depth: int = 0) -> dict:
    """Keep the fields anything downstream might read; drop the prose."""
    if not isinstance(record, dict) or depth > 2:
        return {}
    out = {}
    for key, value in record.items():
        if key in RAW_KEEP:
            out[key] = value
        elif isinstance(value, dict):
            nested = trim_raw(value, depth + 1)
            if nested:
                out[key] = nested
    return out


def epoch(value) -> int | None:
    """Sources date things four different ways. Accept all of them, and return
    None rather than a plausible-looking wrong date."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        # Milliseconds, in the range every one of these boards actually uses.
        return v // 1000 if v > 10_000_000_000 else v
    s = str(value).strip()
    if s.isdigit():
        return epoch(int(s))
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(s.replace("Z", "+0000")
                                    .replace("+00:00", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def sentence_around(text: str, match, window: int = 300) -> str:
    """The sentence a match sits in. Evidence has to be readable by a human
    six weeks later -- a bare regex name proves nothing. One definition, one
    window: eligibility and fit used to carry private copies that had drifted
    to 300 and 240 characters, so the same sentence quoted by two stages came
    out two different lengths."""
    if isinstance(match, str):
        idx = text.lower().find(match.lower())
        if idx < 0:
            return match
        start, end = idx, idx + len(match)
    else:
        start, end = match.span()
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right = min([x for x in (text.find(".", end), text.find("\n", end),
                             start + window) if x > 0] or [len(text)])
    return " ".join(text[left:right].split())[:window].strip()


def strip_html(text: str) -> str:
    """Descriptions arrive as HTML from most sources and as markdown from one.
    The eligibility rules read sentences, so the tags have to go -- but the
    sentence boundaries must survive, or a rule will quote across two of them.
    """
    if not text:
        return ""
    import html
    import re
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# The registry. Import order here is the fetch order.
#
# openjobs is last and disabled by default (settings.yml) -- it is the only
# source here that sends anything off the Mac, so it stays off until a dry run
# is read, and it runs after the seven that need no such review either way.
REGISTRY = [
    "himalayas", "remotive", "remoteok", "jobicy",
    "arbeitnow", "weworkremotely", "hackernews", "ats", "openjobs",
]


def load(name: str):
    import importlib
    return importlib.import_module(f"sources.{name}")
