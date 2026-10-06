#!/usr/bin/env python3
"""Pick which resume variant (and its built PDF) best fits a job posting, by
keyword-frequency scoring -- deterministic and explainable, not ML. The pick
is always shown with its score breakdown so it can be sanity-checked and
overridden, never treated as a black box.

    python3 job-applications/matcher.py posting.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Keyword -> variant. Case-insensitive substring match against the posting
# text; multi-word phrases count as one keyword. Tuned from the actual
# resume content in resume/resume-*.md, not a generic keyword list.
VARIANT_KEYWORDS: dict[str, list[str]] = {
    "tech": [
        "developer", "software engineer", "engineer", "full stack", "full-stack",
        "backend", "back-end", "frontend", "front-end", "programmer", "coding",
        "software development", "api", "codebase", "github", "ci/cd", "devops",
        "cloud", "database", ".net", "asp.net", "node.js", "typescript",
        "javascript", "python developer", "sde", "qa engineer",
        "machine learning engineer", "web development",
    ],
    "business-analyst": [
        "business analyst", "requirements gathering", "requirements", "stakeholder",
        "process improvement", "business process", "workflow", "sql", "excel",
        "reporting", "data analyst", "analytics", "dashboard", "documentation",
        "gap analysis", "user stories", "erp",
    ],
    "management": [
        "manager", "management", "team lead", "leadership", "strategy", "strategic",
        "operations", "director", "supervisor", "p&l", "budget",
        "stakeholder management", "cross-functional", "business development",
        "client relationship",
    ],
    "it-business-analyst": [
        "it business analyst", "systems analyst", "technical business analyst",
        "functional specification", "system requirements", "sdlc", "liaison",
        "technical requirements", "it project", "solutions analyst",
    ],
}

RESUME_PDFS: dict[str, str] = {
    # One CV for every variant since 2026-10-05. Variant keys
    # stay so matching still records the job type.
    "ai-solutions-engineer": "out/Candidate AI Solutions Engineer CV.pdf",
    "tech": "out/Candidate AI Solutions Engineer CV.pdf",
    "business-analyst": "out/Candidate AI Solutions Engineer CV.pdf",
    "management": "out/Candidate AI Solutions Engineer CV.pdf",
    "it-business-analyst": "out/Candidate AI Solutions Engineer CV.pdf",
}

RESUME_SOURCE_MD: dict[str, str] = {
    "ai-solutions-engineer": "resume/resume-ai-solutions-engineer.md",
    "tech": "resume/resume-ai-solutions-engineer.md",
    "business-analyst": "resume/resume-ai-solutions-engineer.md",
    "management": "resume/resume-ai-solutions-engineer.md",
    "it-business-analyst": "resume/resume-ai-solutions-engineer.md",
}

# No keyword signal at all -- default to the most general-purpose variant
# rather than guessing blind.
_FALLBACK_VARIANT = "business-analyst"


def score_posting(posting_text: str) -> dict[str, int]:
    text = posting_text.lower()
    return {
        variant: sum(text.count(kw) for kw in keywords)
        for variant, keywords in VARIANT_KEYWORDS.items()
    }


def pick_variant(posting_text: str) -> tuple[str, dict[str, int]]:
    scores = score_posting(posting_text)
    winner = max(scores, key=lambda v: scores[v])
    if scores[winner] == 0:
        winner = _FALLBACK_VARIANT
    return winner, scores


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: matcher.py <posting.txt>", file=sys.stderr)
        return 2
    text = Path(argv[0]).read_text()
    variant, scores = pick_variant(text)
    print(f"picked: {variant}")
    for v, s in sorted(scores.items(), key=lambda kv: -kv[1]):
        print(f"  {v}: {s}")
    print(f"resume pdf: {RESUME_PDFS[variant]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
