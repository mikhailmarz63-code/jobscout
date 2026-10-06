#!/usr/bin/env python3
"""Who jobscout is running for.

With no person set, everything behaves exactly as before: `settings.yml`,
`jobscout.db` and `../resume/` beside the code. Set one and every path moves
into that person's folder instead:

    people/<slug>/
        profile.yml        target titles, seniority, skills (from people.py add)
        settings.yml       country, timezone, salary floor, contact details
        resume/resume-cv.md
        jobscout.db        their own database: their scores, their shortlist
        kits/

The switch is one environment variable so it reaches every stage, including
the ones run.py starts as child processes:

    JOBSCOUT_PERSON=ajey python3 run.py
    python3 run.py --person ajey          # the same thing

Modules ask this file for a path rather than building their own. That is the
whole design: one place decides whose files these are, so a run for one person
can never read another person's CV or write into another person's database.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

HERE = Path(__file__).parent
ROOT = HERE.parent
PEOPLE = ROOT / "people"
ENV = "JOBSCOUT_PERSON"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CV_SLUG = "cv"


def name() -> str:
    return os.environ.get(ENV, "").strip()


def home() -> Path | None:
    """The person's folder, or None when running for the owner."""
    slug = name()
    if not slug:
        return None
    if not SLUG_RE.match(slug):
        raise SystemExit(f"{ENV}={slug!r} is not a valid name: lowercase "
                         f"letters, digits and dashes only")
    folder = PEOPLE / slug
    if not folder.is_dir():
        raise SystemExit(f"no person '{slug}' yet. Add one with:\n"
                         f"    python3 people.py add <cv file> --name {slug}")
    return folder


def path(default: Path, filename: str) -> Path:
    folder = home()
    return folder / filename if folder else default


def resume_dir(default: Path) -> Path:
    folder = home()
    return folder / "resume" if folder else default


def profile() -> dict | None:
    folder = home()
    if not folder:
        return None
    return yaml.safe_load((folder / "profile.yml").read_text()) or {}


def title_regex(phrases: list[str] | None) -> re.Pattern | None:
    """A whole-word, case-insensitive pattern for a list of job-title phrases.

    Longest first, so "data engineer" wins over "engineer" and the match that
    gets quoted back is the specific one."""
    words = sorted({p.strip().lower() for p in phrases or [] if p and p.strip()},
                   key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


NEVER = re.compile(r"(?!x)x")   # a pattern that matches nothing
