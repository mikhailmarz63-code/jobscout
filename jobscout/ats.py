#!/usr/bin/env python3
"""What an applicant-tracking system sees, and what to do about it.

Reads a CV from `resume/resume-<slug>.md`, projects it the way a parser would
(plain text; or the text layer of the rendered PDF when one exists), scores it
against six real ATS platforms with rules ported from an open-source screener,
and proposes edits -- every one of them backed by a line that already exists in
`resume/`. Nothing here invents a claim. What it cannot source it marks
`[NEEDS YOU]`.

    python3 ats.py --self                     # the canonical CV alone, no job
    python3 ats.py 753                        # against job 753's advert
    python3 ats.py 753 --variant tech --pdf "out/Candidate Tech CV.pdf"
    python3 ats.py --jd advert.txt --json     # an advert that is not in the DB

The six numbers are heuristics. Greenhouse and Lever do not score candidates at
all -- a recruiter searches and reads -- so their cards estimate how findable
the CV is, not whether it "passes". Workday, Taleo, iCIMS and SuccessFactors do
parse into fields and support knockout filters; their pass marks here are the
screener's thresholds, not the vendors'.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
RESUME = ROOT / "resume"
OUT = ROOT / "out"
CANONICAL = "ai-solutions-engineer"

# Files a fact may be taken from. COWORK-PROMPT.md lists the same set.
INJECTABLE_FILES = ["STAR-STORIES.md", "INTERVIEW-LINES.md", "INTERVIEW-DEFENCE.md"]

PDF_NAMES = {
    "ai-solutions-engineer": "Candidate AI Solutions Engineer CV.pdf",
    "tech": "Candidate Tech CV.pdf",
    "business-analyst-JK": "Candidate Business Analyst CV.pdf",
    "management": "Candidate Management CV.pdf",
    "it-business-analyst": "Candidate IT Business Analyst CV.pdf",
}


# =============================================================== the model ==

@dataclass
class Entry:
    title: str
    org: str = ""
    dates: str = ""
    note: str = ""
    bullets: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)      # non-bullet lines under the head
    line: int = 0
    bullet_lines: list[int] = field(default_factory=list)
    body_lines: list[int] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.title}|{self.org}".lower()


@dataclass
class Section:
    type: str
    header: str
    line: int
    entries: list[Entry] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    paragraph_lines: list[int] = field(default_factory=list)
    skills: dict[str, list[str]] = field(default_factory=dict)
    skill_lines: dict[str, int] = field(default_factory=dict)
    has_table: bool = False


@dataclass
class CV:
    name: str
    contact: str
    sections: list[Section]
    path: str = ""
    raw: str = ""

    def section(self, type_: str) -> Section | None:
        return next((s for s in self.sections if s.type == type_), None)

    def skills(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for s in self.sections:
            for cat, terms in s.skills.items():
                out.setdefault(cat, []).extend(terms)
        return out

    def experience(self) -> list[Entry]:
        return [e for s in self.sections if s.type == "experience" for e in s.entries]

    def bullets(self) -> list[tuple[Entry, str]]:
        return [(e, b) for e in self.experience() for b in e.bullets]

    def to_dict(self) -> dict:
        return {"name": self.name, "contact": self.contact, "path": self.path,
                "sections": [asdict(s) for s in self.sections]}


# ------------------------------------------------------------- the parser --

H1_RE = re.compile(r"^#\s+(.+?)\s*$")
H2_RE = re.compile(r"^##\s+(.+?)\s*$")
H3_RE = re.compile(r"^###\s+(.+?)\s*$")
HR_RE = re.compile(r"^-{3,}\s*$")
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
SKILL_RE = re.compile(r"^(?:-\s*)?\*\*([^*]+?):\*\*\s*(.+?)\s*$")
BOLD_HEAD_RE = re.compile(r"^\*\*(.+?)\*\*(.*)$")
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_RE = re.compile(
    rf"(?:(?:{MONTH})\s+)?(?:19|20)\d{{2}}\s*(?:[–—-]|to)\s*"
    rf"(?:(?:(?:{MONTH})\s+)?(?:19|20)\d{{2}}|Present|Current|Now)", re.I)
DATE_LINE_RE = re.compile(
    rf"^\**\s*((?:{DATE_RE.pattern})|(?:19|20)\d{{2}})\s*\**(?:\s*,\s*[^*]{{0,60}})?\s*$", re.I)
NOTE_RE = re.compile(r"^\*[^*].*[^*]\*$")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}")
TRAIL_NOTE_RE = re.compile(r"\s*(\*\((?:[^()]|\([^()]*\))*\)\*|\((?:[^()]|\([^()]*\))*\))\s*$")

HEADER_TYPES = [                       # loose, first match wins
    ("experience", r"experience|employment|work history|career history"),
    ("skills", r"skill|competenc|technolog|tools|proficienc|expertise"),
    ("projects", r"project|deliverable|portfolio"),
    ("education", r"education|academic|qualification"),
    ("certifications", r"certif|licen|accredit|training|courses"),
    ("summary", r"summary|profile|objective|about"),
    ("languages", r"language"),
    ("references", r"referee|reference"),
    ("availability", r"availab|notice"),
    ("awards", r"award|honou?r|achievement"),
    ("publications", r"publication|paper|research"),
]

# The anchored patterns an exact parser is documented to recognise. Ported
# from ats-screener's section-detector; deliberately stricter than the loose
# table above, so "Technical Skills & Tools" is recognised by us and flagged
# for Workday and Taleo.
STRICT_HEADERS = {
    "contact": r"^(contact\s*(info(rmation)?)?|personal\s*(info(rmation)?|details))$",
    "summary": r"^(summary|profile|about(\s*me)?|objective|professional\s*summary|career\s*summary|executive\s*summary|personal\s*statement)$",
    "experience": r"^(experience|work\s*experience|professional\s*experience|employment(\s*history)?|work\s*history|relevant\s*experience|career\s*history)$",
    "education": r"^(education|academic(\s*background)?|educational\s*background|qualifications|academic\s*qualifications)$",
    "skills": r"^(skills|technical\s*skills|core\s*competencies|competencies|areas?\s*of\s*expertise|proficiencies|technologies|tools?\s*(&|and)\s*technologies)$",
    "projects": r"^(projects|personal\s*projects|academic\s*projects|notable\s*projects|selected\s*projects|key\s*projects|side\s*projects)$",
    "certifications": r"^(certifications?|licenses?(\s*(&|and)\s*certifications?)?|professional\s*certifications?|accreditations?)$",
    "awards": r"^(awards?|honors?(\s*(&|and)\s*awards?)?|achievements?|recognition|scholarships?)$",
    "publications": r"^(publications?|research|papers?|presentations?)$",
    "volunteer": r"^(volunteer(ing)?(\s*experience)?|community\s*(service|involvement)|extracurricular(\s*activities)?)$",
    "languages": r"^(languages?|language\s*proficiency)$",
    "interests": r"^(interests?|hobbies(\s*(&|and)\s*interests?)?)$",
}
# Header renames that change the label, never the facts.
STANDARD_HEADER_FOR = {
    "technical skills & tools": "Technical Skills", "skills & tools": "Skills",
    "technical skills and tools": "Technical Skills", "core competencies": "Skills",
    "languages & technologies": "Technical Skills", "selected projects": "Projects",
    "key deliverables": "Projects", "academic projects": "Projects",
    "work experience": "Experience",
}


def strip_md(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = s.replace("**", "")
    s = re.sub(r"(?<!\w)\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)", r"\1", s)
    return s.replace("&amp;", "&").strip()


def classify_header(header: str) -> str:
    h = strip_md(header).lower()
    for type_, pat in HEADER_TYPES:
        if re.search(pat, h):
            return type_
    return "unknown"


def strict_header_type(header: str) -> str | None:
    h = re.sub(r"\*\(.*?\)\*", "", strip_md(header)).strip().lower()
    for type_, pat in STRICT_HEADERS.items():
        if re.match(pat, h, re.I):
            return type_
    return None


def split_terms(text: str) -> list[str]:
    """Split a skills line on `,` and ` · ` outside parentheses, so
    "Excel (dashboards, pivot tables)" stays one term."""
    out, depth, cur = [], 0, []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (ch == "," or text.startswith(" · ", i) or text.startswith(" | ", i)):
            out.append("".join(cur).strip())
            cur = []
            i += 3 if ch == " " else 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur).strip())
    return [strip_md(t) for t in out if t.strip()]


def _split_head(text: str) -> tuple[str, str, str]:
    """`**Title**, Org *(note)*` / `**Title** — Org` / `### Title · Org *(note)*`
    -> (title, org, note)."""
    note = ""
    m = TRAIL_NOTE_RE.search(text)
    if m:
        note = strip_md(m.group(1)).strip("*")
        text = text[:m.start()].rstrip()
    bold = BOLD_HEAD_RE.match(text)
    if bold:
        title, rest = bold.group(1).strip(), bold.group(2).strip()
        org = re.sub(r"^[,—–·:-]\s*", "", rest).strip()
        return strip_md(title), strip_md(org), note
    if " · " in text:
        title, org = text.split(" · ", 1)
        return strip_md(title), strip_md(org), note
    return strip_md(text), "", note


def parse_cv(text: str, path: str = "") -> CV:
    lines = text.splitlines()
    name, contact_parts = "", []
    sections: list[Section] = []
    section: Section | None = None
    entry: Entry | None = None
    in_table_header = False

    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if HR_RE.match(line):
            continue
        m = H1_RE.match(line)
        if m and not name:
            name = strip_md(m.group(1))
            continue
        m = H2_RE.match(line)
        if m:
            section = Section(classify_header(m.group(1)), strip_md(m.group(1)), n)
            sections.append(section)
            entry = None
            continue
        if section is None:
            contact_parts.append(strip_md(line))
            continue

        m = H3_RE.match(line)
        if m:
            title, org, note = _split_head(m.group(1))
            entry = Entry(title, org, "", note, line=n)
            section.entries.append(entry)
            continue

        if TABLE_RE.match(line):
            section.has_table = True
            if TABLE_SEP_RE.match(line):
                continue
            cells = [strip_md(c) for c in line.strip().strip("|").split("|")]
            section.paragraphs.append(" · ".join(c for c in cells if c))
            section.paragraph_lines.append(n)
            continue

        skill = SKILL_RE.match(line)
        if skill and section.type in ("skills", "languages"):
            cat = strip_md(skill.group(1))
            section.skills[cat] = split_terms(skill.group(2))
            section.skill_lines[cat] = n
            continue

        bullet = BULLET_RE.match(line)
        if bullet:
            body = bullet.group(1)
            head = BOLD_HEAD_RE.match(body)
            if head and section.type in ("projects", "education", "certifications", "awards") \
                    and not entry_is_open_for_bullets(section, entry):
                # `- **Name** (capstone): text` -- a project written as a bullet
                title, org, note = _split_head(body)
                rest = ""
                # anything after the bold head and separator is the description
                after = head.group(2)
                after = TRAIL_NOTE_RE.sub("", after) if not org else ""
                rest = re.sub(r"^[:—–-]\s*", "", after).strip() if after else org
                e = Entry(title, "" if rest == org else org, "", note, line=n)
                if rest:
                    e.body.append(strip_md(rest))
                    e.body_lines.append(n)
                section.entries.append(e)
                entry = e
                continue
            if entry is not None and section.type in ("experience", "projects", "education"):
                entry.bullets.append(strip_md(body))
                entry.bullet_lines.append(n)
            else:
                section.paragraphs.append(strip_md(body))
                section.paragraph_lines.append(n)
            continue

        dm = DATE_LINE_RE.match(line.strip())
        if dm and entry is not None and not entry.dates:
            entry.dates = strip_md(dm.group(1)).strip()
            continue

        head = BOLD_HEAD_RE.match(line.strip())
        if head and section.type not in ("summary", "availability"):
            title, org, note = _split_head(line.strip())
            entry = Entry(title, org, "", note, line=n)
            # a date hiding in the note or after the dash: *(2022)*,
            # *(2023 to August 2026)*, `**IGCSE** — 2021`
            dm2 = re.search(rf"(?:{DATE_RE.pattern})|(?:{MONTH}\s+)?(?:19|20)\d{{2}}", note or "", re.I)
            if dm2 and section.type == "education":
                entry.dates = dm2.group(0)
            if section.type == "education" and org and re.fullmatch(
                    rf"(?:{DATE_RE.pattern})|(?:{MONTH}\s+)?(?:19|20)\d{{2}}", org, re.I):
                entry.dates, entry.org = org, ""
            section.entries.append(entry)
            continue

        if NOTE_RE.match(line.strip()) and entry is None:
            section.paragraphs.append(strip_md(line))
            section.paragraph_lines.append(n)
            continue

        if entry is not None and not entry.bullets:
            body = strip_md(line)
            entry.body.append(body)
            entry.body_lines.append(n)
            if section.type == "education" and not entry.dates:
                dm3 = re.search(rf"(?:{DATE_RE.pattern})|(?:{MONTH}\s+)?(?:19|20)\d{{2}}", body, re.I)
                if dm3:
                    entry.dates = dm3.group(0)
            if section.type == "education" and not entry.org and " · " in body:
                entry.org = body.split(" · ", 1)[0]
            continue

        para = strip_md(line)
        # A competencies paragraph: "A · B · C" under a skills header. Captured
        # into section.skills, not section.paragraphs -- else every consumer
        # (PDF, plain_text projection) prints the line twice.
        if section.type == "skills" and " · " in para:
            section.skills[section.header] = split_terms(para)
            section.skill_lines[section.header] = n
            continue
        section.paragraphs.append(para)
        section.paragraph_lines.append(n)

    contact = " · ".join(p for p in contact_parts if p)
    return CV(name=name, contact=contact, sections=sections, path=path, raw=text)


def entry_is_open_for_bullets(section: Section, entry: Entry | None) -> bool:
    """A bold bullet inside an entry that already has bullets is a bullet with
    emphasis, not a new entry."""
    return entry is not None and bool(entry.bullets) and section.type == "experience"


def variant_path(slug: str) -> Path:
    return RESUME / f"resume-{slug}.md"


def slug_of(path: Path) -> str:
    return path.stem.removeprefix("resume-")


def pdf_path_for(slug: str) -> Path:
    """Where the rendered PDF of a variant lives (whether or not it exists)."""
    name = PDF_NAMES.get(slug)
    if not name:
        parts = [("AI" if p == "ai" else "IT" if p == "it" else p.capitalize())
                 for p in slug.split("-")]
        name = f"Candidate {' '.join(parts)} CV.pdf"
    return OUT / name


def default_pdf(slug: str) -> Path | None:
    p = pdf_path_for(slug)
    return p if p.exists() else None


def list_variants() -> list[dict]:
    out = []
    for p in sorted(RESUME.glob("resume-*.md")):
        slug = slug_of(p)
        pdf = default_pdf(slug)
        out.append({"slug": slug, "path": str(p), "canonical": slug == CANONICAL,
                    "pdf": str(pdf) if pdf else None,
                    "mtime": int(p.stat().st_mtime)})
    return out


def load_cv(path: Path | str) -> CV:
    p = Path(path)
    return parse_cv(p.read_text(), str(p))


# ============================================================ projections ==

def entry_head(e: Entry) -> str:
    head = e.title if not e.org else f"{e.title}, {e.org}"
    return f"{head} ({e.note})" if e.note else head


def plain_text(cv: CV) -> str:
    """What a parser sees: no markup, one fact per line, dates on their own
    line, one skills line per category. The PDF builder honours the same
    contract, so the two projections agree."""
    out = [cv.name, cv.contact, ""]
    for s in cv.sections:
        out.append(s.header)
        for p in s.paragraphs:
            out.append(p)
        for cat, terms in s.skills.items():
            out.append(f"{cat}: {', '.join(terms)}")
        for e in s.entries:
            out.append(entry_head(e))
            if e.dates:
                out.append(e.dates)
            out.extend(e.body)
            out.extend(f"- {b}" for b in e.bullets)
        out.append("")
    return "\n".join(out).strip() + "\n"


@dataclass
class PdfText:
    text: str
    pages: int
    has_images: bool
    extractor: str
    warnings: list[str] = field(default_factory=list)


def pdf_text(path: Path | str) -> PdfText | None:
    """The text layer of a PDF, via pypdf (pdfminer as a fallback). None when
    neither is installed -- the report then says "not analysed" rather than
    pretending."""
    p = Path(path)
    if not p.exists():
        return None
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        has_images = False
        for pg in reader.pages:
            try:
                if pg.images:
                    has_images = True
                    break
            except Exception:                       # noqa: BLE001 -- odd XObjects
                res = pg.get("/Resources") or {}
                xo = res.get("/XObject") if hasattr(res, "get") else None
                if xo and any("/Image" in str(getattr(v, "get_object", lambda: v)())
                              for v in xo.values()):
                    has_images = True
                    break
        text = "\n".join(pages)
        extractor = "pypdf"
    except ImportError:
        try:
            from pdfminer.high_level import extract_text
            text = extract_text(str(p))
            pages = text.split("\f")
            has_images = False
            extractor = "pdfminer"
        except ImportError:
            return None
    if "\x7f" in text:
        warnings.append("bullet glyph lands as a control character (\\x7f) in the text layer")
    for i, pg in enumerate(pages, 1):
        if not pg.strip():
            warnings.append(f"page {i} has no extractable text -- image-only or outlined fonts")
    return PdfText(text=text, pages=len(pages), has_images=has_images,
                   extractor=extractor, warnings=warnings)


# ================================================================ signals ==

DATE_FORMATS = {
    "Month YYYY": re.compile(rf"\b{MONTH}\s+(?:19|20)\d{{2}}\b", re.I),
    "MM/YYYY": re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}\b"),
    "DD/MM/YYYY": re.compile(r"\b\d{1,2}/\d{1,2}/(?:19|20)\d{2}\b"),
    "YYYY": re.compile(r"(?<![/\d])(?:19|20)\d{2}(?![/\d])"),
}
PERSONAL_DATA_RE = re.compile(r"\bage\s*:?\s*\d{2}\b|\bdate of birth\b|\bd\.?o\.?b\b|\bnationality\b"
                              r"|\bmarital status\b|\breligion\b|\bgender\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


@dataclass
class Signals:
    source: str
    text: str
    lines: list[str]
    words: int
    pages: int
    has_images: bool
    multi_column: bool
    tables: bool
    special_char_ratio: float
    caps_lines: int
    bullet_styles: int
    date_formats: dict[str, int]
    entry_date_formats: list[str]
    sections_present: list[str]
    strict_unknown_headers: list[str]
    bullets: list[str]
    skills: list[str]
    education_text: str
    contact: dict[str, bool]
    column_evidence: dict = field(default_factory=dict)


def date_format_of(dates: str) -> str:
    if not dates:
        return ""
    for name in ("Month YYYY", "MM/YYYY", "DD/MM/YYYY"):
        if DATE_FORMATS[name].search(dates):
            return name
    if DATE_FORMATS["YYYY"].search(dates):
        return "YYYY"
    return "other"


def _text_signals(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    lengths = [len(ln) for ln in lines] or [0]
    mean = statistics.fmean(lengths)
    cv = (statistics.pstdev(lengths) / mean) if mean else 0.0
    gap_lines = sum(1 for ln in lines if re.search(r"\S {3,}\S", ln))
    two_gap_lines = sum(1 for ln in lines if len(re.findall(r"\S {3,}\S", ln)) >= 2 or "\t" in ln)
    median = statistics.median(lengths)
    date_mid = sum(1 for ln in lines if (m := DATE_RE.search(ln)) and len(ln) - m.end() >= 15)
    n = max(1, len(lines))
    multi_column = cv > 0.6 and gap_lines / n > 0.25 and median < 45
    tables = two_gap_lines / n > 0.10
    special = len(re.findall(r"[^\w\s.,;:!?@#$%&*()\-+=/\\'\"·–—•]", text)) / max(1, len(text))
    caps = sum(1 for ln in lines if len(ln.strip()) > 30 and ln == ln.upper() and re.search(r"[A-Z]", ln))
    styles = {m.group(1) for ln in lines if (m := re.match(r"^\s*([-•*·▪►➤○●])\s", ln))}
    return {"lines": lines, "words": len(re.findall(r"\b\w+\b", text)),
            "multi_column": multi_column, "tables": tables,
            "special_char_ratio": special, "caps_lines": caps,
            "bullet_styles": len(styles),
            "date_formats": {k: len(p.findall(text)) for k, p in DATE_FORMATS.items()
                             if p.findall(text)},
            "column_evidence": {"line_length_cv": round(cv, 2), "gap_line_ratio": round(gap_lines / n, 2),
                                "two_gap_ratio": round(two_gap_lines / n, 2),
                                "median_len": median, "date_mid_line": date_mid}}


def _model_signals(cv: CV) -> dict:
    exp = cv.experience()
    edu = cv.section("education")
    edu_text = ""
    if edu:
        edu_text = " ".join([*edu.paragraphs, *(entry_head(e) + " " + e.dates + " " + " ".join(e.body)
                                                for e in edu.entries)])
    present = sorted({s.type for s in cv.sections if s.type != "unknown"})
    if EMAIL_RE.search(cv.contact) or PHONE_RE.search(cv.contact):
        present.append("contact")
    return {"entry_date_formats": [date_format_of(e.dates) for e in exp],
            "sections_present": sorted(set(present)),
            "strict_unknown_headers": [s.header for s in cv.sections
                                       if strict_header_type(s.header) is None],
            "bullets": [b for _, b in cv.bullets()],
            "skills": [t for terms in cv.skills().values() for t in terms],
            "education_text": edu_text,
            "contact": {"email": bool(EMAIL_RE.search(cv.contact)),
                        "phone": bool(PHONE_RE.search(cv.contact)),
                        "linkedin": "linkedin" in cv.contact.lower()}}


def signals_from_cv(cv: CV) -> Signals:
    text = plain_text(cv)
    t = _text_signals(text)
    # markdown cannot have columns or tables in the text-layer sense; the
    # rendered PDF is where that is measured. A markdown table is still noted.
    t.update(multi_column=False, tables=any(s.has_table for s in cv.sections))
    words = t["words"]
    return Signals(source="markdown", text=text, pages=max(1, math.ceil(words / 600)),
                   has_images=False, **t, **_model_signals(cv))


def signals_from_pdf(cv: CV, pdf: PdfText) -> Signals:
    t = _text_signals(pdf.text)
    return Signals(source="pdf", text=pdf.text, pages=pdf.pages, has_images=pdf.has_images,
                   **t, **_model_signals(cv))


# =============================================================== keywords ==

SYNONYMS: dict[str, set[str]] = {
    "postgresql": {"postgres", "psql"}, "javascript": {"js"}, "typescript": {"ts"},
    "llm": {"large language model", "large language models", "llms"},
    "machine learning": {"ml"}, "uat": {"user acceptance testing", "acceptance testing"},
    "sdlc": {"software development lifecycle", "software development life cycle"},
    "requirements gathering": {"requirements elicitation", "requirement gathering",
                               "requirements analysis", "gathering requirements"},
    "rest": {"restful", "rest api", "rest apis"}, "node.js": {"node", "nodejs"},
    "asp.net core": {"asp.net", ".net core", ".net"},
    "rag": {"retrieval-augmented generation", "retrieval augmented generation"},
    "generative ai": {"genai", "gen ai", "generative artificial intelligence"},
    "openai": {"chatgpt"}, "business analyst": {"business analysis"},
    "stakeholder": {"stakeholders", "stakeholder management"},
    "ci/cd": {"cicd", "continuous integration", "continuous delivery", "continuous deployment"},
    "sql": {"structured query language"}, "excel": {"microsoft excel", "ms excel"},
    "dashboard": {"dashboards", "dashboarding"}, "automation": {"automate", "automated", "automating"},
    "specification": {"specifications", "functional specification", "functional specifications", "spec"},
    "prompt engineering": {"prompting", "prompt design"},
    "python": {"python3"}, "api": {"apis"}, "html/css": {"html", "css"},
    "kubernetes": {"k8s"}, "aws": {"amazon web services"}, "gcp": {"google cloud platform"},
    "docker": {"dockerized", "dockerised"},
}
_SYN_INDEX: dict[str, str] = {}
for _canon, _vs in SYNONYMS.items():
    _SYN_INDEX.setdefault(_canon, _canon)
    for _v in _vs:
        _SYN_INDEX.setdefault(_v, _canon)


def canonical(term: str) -> str:
    return _SYN_INDEX.get(term.lower(), term.lower())


JD_STOP = frozenset("""
a about above across after again against all also although always am an and any
are around as at be because been before being below between both but by can
could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just let me
more most my no nor not of off on once only or other our ours out over own re
same she should so some such than that the their theirs them then there these
they this those through to too under until up very was we were what when where
which while who whom why will with within without would you your yours yourself
role team work working ability strong plus experience experienced years year
join us our company candidate candidates ideal looking looking-for want wants
need needs needed required requirement requirements preferred qualifications
responsibilities responsibility include including including: etc e.g i.e
build building develop developing design designing support supporting help
helping ensure ensuring drive driving deliver delivering great good excellent
skills skill knowledge understanding familiarity proficiency proficient
environment environments opportunity opportunities benefits salary equal
employer employment apply application applicants position positions job jobs
day daily week weekly new using use used based across within global remote
world time full part fully high highly well one two three
""".split())

GENERIC = frozenset("""
wide range outstanding results result track record way ways meet meeting annual
colleagues colleague code coding software engineer engineers engineering graduate
graduates degree university level levels senior junior lead leads world class
world-class exceptional excellent strong good great best better ability abilities
passion passionate love enjoy interest interested keen motivated driven commitment
committed culture values value mission vision impact impactful growth grow scale
fast-paced dynamic exciting innovative modern leading leader industry industries
customers customer users user product products platform platforms solutions solution
business businesses technical technology technologies team teams people person
member members individual individuals organisation organization company companies
project projects task tasks work works working workplace office hybrid onsite on-site
location located relocate relocation travel benefits compensation package bonus equity
pension holiday holidays vacation health dental insurance perks culture diversity
inclusive inclusion equal opportunity gender race religion disability veteran status
please note contact email click link apply now today start date deadline interview
process stage stages round rounds offer offers join joining hire hiring recruit
recruiting recruitment talent career careers professional professionals development
opportunity opportunities learn learning mentorship mentor mentoring training
communication communicate communicating written verbal english language fluent
attention detail detail-oriented oriented problem problems solving solve solver
mindset approach approaches perspective perspectives background backgrounds
year years month months day days hour hours week weeks time times full part time-off
another home interesting personal contribute contributions contribution others thing
things everything something anything everyone someone anyone ideas idea change changes
help helps area areas range variety various different difference make makes making
""".split())

# Terms an ATS keyword list commonly carries that none of his files name yet.
# Recognising them as skills keeps "linux distribution" from becoming a
# phrase of its own, and lets a real gap read as "[NEEDS YOU] linux".
BUILTIN_SKILLS = """
linux ubuntu debian rust c++ c# java golang git github gitlab bitbucket kubernetes
terraform ansible aws azure gcp cloud saas b2b b2c crm sap salesforce tableau
power bi looker pandas numpy spark airflow kafka redis mongodb mysql graphql grpc
microservices oop tdd jira confluence figma agile scrum kanban devops sre oauth
jwt etl elt data modelling data modeling data warehouse analytics statistics
a/b testing product management project management prince2 pmp itil iso 27001
gdpr soc 2 open source open-source distributed systems networking tcp/ip bash
shell scripting powershell fastapi django flask react vue angular next.js
""".split("\n")

ACTION_VERBS = frozenset("""
achieved accelerated administered advanced analyzed analysed architected automated
built centralized centralised championed collaborated conceptualized consolidated
contributed converted coordinated created decreased defined delivered designed
developed directed drove eliminated enabled engineered established exceeded executed
expanded facilitated founded generated grew headed identified implemented improved
increased influenced initiated innovated integrated introduced launched led
leveraged managed maximized mentored migrated modernized negotiated operated
optimized optimised orchestrated organized organised outperformed overhauled oversaw
pioneered planned presented prioritized prioritised produced programmed proposed
published raised recommended redesigned reduced refactored reformed re-engineered
reorganized replaced researched resolved restructured revamped revolutionized scaled
secured simplified spearheaded standardized streamlined strengthened supervised
surpassed synchronized trained transformed translated unified upgraded
took ran wrote specified rebuilt re-architected shipped validated caught gathered
served extended handled edited tracked founded packaged productized productised
owned maintained authored documented configured deployed tested reviewed supported
assessed audited cut removed migrated automated modelled modeled mapped scoped
""".split())

QUANT_RES = [re.compile(p, re.I) for p in (
    r"\d+(?:\.\d+)?\s*%", r"[$£€₹]\s?[\d,]+", r"\b\d+\s*(?:x|times)\b",
    r"\b\d+\+?\s*(?:users?|customers?|clients?|employees?|members?|team|staff|people)\b",
    r"\b\d+\+?\s*(?:projects?|products?|applications?|systems?|services?|sites?|videos?|posts?|pieces)\b",
    r"\b(?:top|first|#)\s*\d+\b", r"\b\d+\s*(?:hours?|days?|weeks?|months?|years?)\b",
    r"\b\d{1,3}(?:,\d{3})+\+?\b", r"\b\d+\s*(?:million|billion|thousand|k|m|b)\b",
    r"\b\d+\+?\s*(?:records?|files?|sheets?|charts?|documents?|vlans?|messages?|source)\b",
    r"\b(?:negative|minus)\s+\d+", r"\b\d+(?:st|nd|rd|th)\s+percentile\b")]

BANNED_WORDS = frozenset("""
delve tapestry multifaceted pivotal realm synergy paradigm holistic nuanced foster
embark leverage leveraged leveraging utilize utilized utilizing harness harnessed
spearhead spearheaded cornerstone cutting-edge groundbreaking robust comprehensive
meticulous meticulously notably subsequently remarkably seamlessly thereby
facilitate facilitated showcase showcased underscore underscored bolster bolstered
innovative
""".split())
BANNED_PHRASES = ("proven track record", "passionate about", "demonstrated ability to",
                  "strong foundation in", "well-versed in", "adept at", "results-driven",
                  "detail-oriented", "team player", "go-getter", "think outside the box",
                  "i am excited to apply", "uniquely positioned", "it is worth noting")

BUDGET = {"one_line": 111, "two_line_target": [189, 205], "two_line_max": 218,
          "orphan_min": 78, "awkward_zone": [112, 188]}


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    return [t.strip(".,;:!?()[]{}\"'") for t in
            re.findall(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9+#]|[a-z0-9]", text)]


def stem(w: str) -> str:
    if len(w) <= 4:
        return w
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    return w


def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![\w+#.]){re.escape(term.lower())}(?![\w+#])", re.I)


def has_term(text: str, term: str) -> bool:
    return bool(_word_re(term).search(text))


def count_term(text: str, term: str) -> int:
    return len(_word_re(term).findall(text))


@dataclass
class Taxonomy:
    terms: list[str]
    sources: dict[str, str]


_TAX: Taxonomy | None = None


def _builtin_terms() -> list[str]:
    """BUILTIN_SKILLS is written as words; the two-word entries are listed
    here explicitly so the split does not tear them apart."""
    multi = {"power bi", "data modelling", "data modeling", "data warehouse",
             "a/b testing", "product management", "project management", "iso 27001",
             "soc 2", "open source", "distributed systems", "shell scripting"}
    text = " ".join(BUILTIN_SKILLS)
    for m in multi:
        text = text.replace(m, "")
    return sorted(multi | set(text.split()))


def taxonomy(extra: list[str] = ()) -> Taxonomy:
    """Every phrase this system already treats as a skill: score.SKILLS,
    matcher.VARIANT_KEYWORDS, every skills-line term of every variant, and the
    synonym map. Cached; degrades to the CV terms if a module is missing."""
    global _TAX
    if _TAX is not None and not extra:
        return _TAX
    terms: dict[str, str] = {}
    try:
        sys.path.insert(0, str(HERE))
        from score import SKILLS
        for t in SKILLS:
            terms.setdefault(t.lower(), "score.SKILLS")
    except Exception:                                 # noqa: BLE001
        pass
    try:
        sys.path.insert(0, str(ROOT / "job-applications"))
        from matcher import VARIANT_KEYWORDS
        for vs in VARIANT_KEYWORDS.values():
            for t in vs:
                terms.setdefault(t.lower(), "matcher")
    except Exception:                                 # noqa: BLE001
        pass
    for p in RESUME.glob("resume-*.md"):
        try:
            cv = load_cv(p)
        except Exception:                             # noqa: BLE001
            continue
        for cat_terms in cv.skills().values():
            for t in cat_terms:
                base = re.sub(r"\s*\(.*?\)\s*", " ", t).strip().lower()
                for piece in (base, *re.findall(r"\(([^)]*)\)", t)):
                    for sub in split_terms(piece):
                        sub = sub.lower().strip()
                        if 2 <= len(sub) <= 40:
                            terms.setdefault(sub, p.name)
    for t in _builtin_terms():
        terms.setdefault(t, "builtin")
    for k, vs in SYNONYMS.items():
        terms.setdefault(k, "synonyms")
        for v in vs:
            terms.setdefault(v, "synonyms")
    for t in extra:
        terms.setdefault(t.lower(), "extra")
    for junk in ("ai", "mis", "api", "ba", "py", "ts", "js", "spec", "node"):
        pass  # kept: short, but real skills in his field
    tax = Taxonomy(sorted(terms, key=lambda s: (-len(s), s)), terms)
    if not extra:
        _TAX = tax
    return tax


@dataclass
class Keyword:
    term: str
    count: int
    kind: str
    weight: float
    required: bool


REQ_HEAD = re.compile(r"^(?:\W*)(requirements?|qualifications?|what you.?ll need|must[- ]haves?|"
                      r"you have|about you|who you are|what we.?re looking for|"
                      r"minimum qualifications|basic qualifications|you should have|"
                      r"what you bring|skills (?:and|&) experience|nice to have|preferred)\b", re.I)


def required_block(text: str) -> str:
    """The advert's requirements section(s): from a header that names them to
    the next short header-looking line, at most 40 lines each."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        if REQ_HEAD.match(lines[i].strip()) and len(lines[i].split()) <= 8:
            j = i + 1
            while j < len(lines) and j - i < 40:
                ln = lines[j].strip()
                if ln and len(ln.split()) <= 6 and not ln.endswith((".", ",", ";")) \
                        and not re.match(r"^[-•*]", ln) and j > i + 1 and ln[0].isupper():
                    break
                out.append(ln)
                j += 1
            i = j
        else:
            i += 1
    return "\n".join(out)


def jd_keywords(text: str, tax: Taxonomy | None = None, limit: int = 40,
                exclude: list[str] = ()) -> list[Keyword]:
    """The advert's vocabulary, weighted. Skills from the taxonomy first (3),
    then repeated bigrams (2), then repeated words (1); ×count up to 3, ×1.5
    inside the requirements block. Filler and the company's own name are
    never keywords -- an ATS does not screen you for saying "wide range"."""
    tax = tax or taxonomy()
    text_l = text.lower()
    req = required_block(text).lower()
    skip = JD_STOP | GENERIC | {w.lower() for x in exclude for w in tokenize(x)}
    found: dict[str, Keyword] = {}

    for term in tax.terms:
        if len(term) < 2 or term in skip:
            continue
        c = count_term(text_l, term)
        if c:
            found[term] = Keyword(term, c, "skill", 3.0 * min(c, 3), has_term(req, term))

    toks = tokenize(text_l)
    ok = lambda t: t not in skip and len(t) >= 3 and not t.isdigit()   # noqa: E731
    skill_words = {w for t in found for w in t.split()}
    bigrams = Counter(f"{a} {b}" for a, b in zip(toks, toks[1:]) if ok(a) and ok(b))
    for bg, c in bigrams.items():
        a, b = bg.split()
        if c >= 2 and bg not in found and a not in skill_words and b not in skill_words:
            found[bg] = Keyword(bg, c, "phrase", 2.0 * min(c, 3), has_term(req, bg))
    for w, c in Counter(t for t in toks if ok(t)).items():
        if c >= 3 and w not in found and w not in skill_words:
            found[w] = Keyword(w, c, "word", 1.0 * min(c, 3), has_term(req, w))

    kws = list(found.values())
    for k in kws:
        if k.required:
            k.weight *= 1.5
    # a unigram already inside a kept phrase is noise
    phrases = [k.term for k in kws if " " in k.term]
    kws = [k for k in kws if " " in k.term or not any(has_term(p, k.term) for p in phrases)]
    kws.sort(key=lambda k: (-k.weight, -k.count, k.term))
    return kws[:limit]


@dataclass
class KeywordMatch:
    score: int
    matched: list[str]
    synonym: list[str]
    missing: list[str]
    per_term: dict[str, str]


def match_keywords(cv_text: str, keywords: list[Keyword], strategy: str) -> KeywordMatch:
    """Port of the screener's matcher, weighted by the keyword's own weight.
    exact: whole-word only. fuzzy: + synonyms and stems. semantic: + containment."""
    text_l = cv_text.lower()
    toks = set(tokenize(text_l))
    stems = {stem(t) for t in toks}
    canon_present = {canonical(t) for t in toks}
    matched, synonym, missing, per = [], [], [], {}
    w_all = w_exact = w_syn = 0.0
    for k in keywords:
        w_all += k.weight
        t = k.term
        if has_term(text_l, t):
            matched.append(t); per[t] = "exact"; w_exact += k.weight
            continue
        if strategy == "exact":
            missing.append(t); per[t] = "missing"
            continue
        c = canonical(t)
        hit = c in canon_present or any(has_term(text_l, v) for v in SYNONYMS.get(c, ()))
        hit = hit or (c != t and has_term(text_l, c))
        if not hit and " " not in t:
            hit = stem(t) in stems
        if not hit and " " in t:
            parts = [p for p in t.split() if p not in JD_STOP]
            hit = bool(parts) and all(stem(p) in stems for p in parts) and strategy == "semantic"
        if hit:
            synonym.append(t); per[t] = "synonym"; w_syn += k.weight
            continue
        if strategy == "semantic" and len(t) >= 4:
            if any((t in tok or tok in t) and min(len(t), len(tok)) >= 4 for tok in toks):
                synonym.append(t); per[t] = "partial"; w_syn += k.weight
                continue
        missing.append(t); per[t] = "missing"
    score = round(100 * (w_exact + 0.8 * w_syn) / w_all) if w_all else 0
    return KeywordMatch(min(100, score), matched, synonym, missing, per)


# =============================================================== profiles ==

@dataclass
class Quirk:
    id: str
    delta: int
    message: str


@dataclass
class Profile:
    name: str
    vendor: str
    strictness: float
    strategy: str
    weights: dict[str, float]
    required_sections: list[str]
    date_formats: list[str]
    passing: int
    auto_scores: bool
    quirks: list
    note: str = ""


def _q_workday_headers(sig, kw):
    if len(sig.strict_unknown_headers) > 2:
        return Quirk("workday-header-format", -5,
                     f"{len(sig.strict_unknown_headers)} section headers are not standard names "
                     f"({', '.join(sig.strict_unknown_headers[:4])}). Workday expects Experience, "
                     "Education, Skills.")


def _q_workday_pages(sig, kw):
    if sig.pages > 2:
        return Quirk("workday-page-limit", -8, f"{sig.pages} pages. Workday may truncate beyond page 2.")


def _q_taleo_density(sig, kw):
    if kw is not None and len(sig.skills) < 5:
        return Quirk("taleo-keyword-density", -10,
                     "fewer than five skills listed explicitly. Taleo relies on keyword matching.")


def _q_taleo_sections(sig, kw):
    missing = [h for h in ("contact", "experience", "education", "skills") if h not in sig.sections_present]
    if len(missing) > 1:
        return Quirk("taleo-section-headers", -8, f"missing standard sections: {', '.join(missing)}.")


def _q_icims_taxonomy(sig, kw):
    if len(sig.skills) >= 10:
        return Quirk("icims-skills-taxonomy", +5, "a detailed skills list suits iCIMS's taxonomy matching.")


def _q_greenhouse_quant(sig, kw):
    if sig.bullets and sum(quantified(b) for b in sig.bullets) / len(sig.bullets) >= 0.4:
        return Quirk("greenhouse-quantification", +8, "strong quantification; scorecards reward measurable impact.")


def _q_greenhouse_projects(sig, kw):
    if "projects" in sig.sections_present:
        return Quirk("greenhouse-projects", +3, "a projects section; hiring managers read it.")


def _q_lever_narrative(sig, kw):
    if sig.bullets:
        avg = sum(map(len, sig.bullets)) / len(sig.bullets)
        if 60 <= avg <= 150:
            return Quirk("lever-narrative", +5, "well-detailed bullets suit Lever's contextual matching.")


def _q_lever_summary(sig, kw):
    if "summary" in sig.sections_present:
        return Quirk("lever-summary", +3, "a professional summary; Lever's CRM uses it for context.")


def _q_sf_dates(sig, kw):
    if not re.search(r"\b(19|20)\d{2}\b", sig.text):
        return Quirk("sf-structured-data", -10, "no dates detected. SuccessFactors needs a date per position.")
    if not sig.bullets:
        return Quirk("sf-structured-data", -8, "no experience entries detected.")


def _q_sf_sections(sig, kw):
    missing = [h for h in ("contact", "experience", "education", "skills") if h not in sig.sections_present]
    if missing:
        return Quirk("sf-section-structure", -5 * len(missing), f"missing sections: {', '.join(missing)}.")


def _q_dates_for(profile_formats: list[str], strictness: float):
    def check(sig, kw):
        odd = [f for f in sig.entry_date_formats if f and f not in profile_formats]
        if odd:
            pen = -round(3 * strictness * len(odd))
            return Quirk("date-format", pen,
                         f"{len(odd)} experience date(s) not in a preferred format "
                         f"({', '.join(sorted(set(odd)))}); this parser prefers {', '.join(profile_formats)}.")
    return check


NO_AUTO_SCORE = ("does not score or auto-reject applications. A recruiter searches and "
                 "filters by keyword and reads the PDF; this number estimates how findable "
                 "and readable the CV is in that search, not whether it \"passes\".")

PROFILES: list[Profile] = [
    Profile("Workday", "Workday, Inc.", 0.9, "exact",
            {"formatting": .25, "keywords": .30, "sections": .15, "experience": .15,
             "education": .10, "quantification": .05},
            ["contact", "experience", "education", "skills"], ["MM/YYYY", "Month YYYY"], 70, True,
            [_q_workday_headers, _q_workday_pages, _q_dates_for(["MM/YYYY", "Month YYYY"], 0.9)],
            "strict parser, exact keyword matching, HiredScore ranking; skips headers and footers."),
    Profile("Taleo", "Oracle Corporation", 0.85, "exact",
            {"formatting": .20, "keywords": .35, "sections": .15, "experience": .15,
             "education": .10, "quantification": .05},
            ["contact", "experience", "education", "skills"], ["MM/YYYY", "Month YYYY"], 65, True,
            [_q_taleo_density, _q_taleo_sections, _q_dates_for(["MM/YYYY", "Month YYYY"], 0.85)],
            "boolean keyword filtering, knockout questions, rigid parsing."),
    Profile("iCIMS", "iCIMS, Inc.", 0.6, "fuzzy",
            {"formatting": .15, "keywords": .30, "sections": .15, "experience": .20,
             "education": .10, "quantification": .10},
            ["contact", "experience", "education"], ["Month YYYY", "MM/YYYY", "YYYY"], 60, True,
            [_q_icims_taxonomy, _q_dates_for(["Month YYYY", "MM/YYYY", "YYYY"], 0.6)],
            "grammar-based NLP parser with a skills taxonomy; the most forgiving of the strict four."),
    Profile("SuccessFactors", "SAP SE", 0.85, "exact",
            {"formatting": .25, "keywords": .25, "sections": .20, "experience": .15,
             "education": .10, "quantification": .05},
            ["contact", "experience", "education", "skills"], ["MM/YYYY", "DD/MM/YYYY"], 65, True,
            [_q_sf_dates, _q_sf_sections, _q_dates_for(["MM/YYYY", "DD/MM/YYYY"], 0.85)],
            "Textkernel parser into structured SAP fields; date-sensitive."),
    Profile("Greenhouse", "Greenhouse Software", 0.4, "semantic",
            {"formatting": .10, "keywords": .25, "sections": .10, "experience": .25,
             "education": .10, "quantification": .20},
            ["experience", "education"], ["Month YYYY", "MM/YYYY", "YYYY"], 55, False,
            [_q_greenhouse_quant, _q_greenhouse_projects, _q_dates_for(["Month YYYY", "MM/YYYY", "YYYY"], 0.4)],
            "Greenhouse " + NO_AUTO_SCORE),
    Profile("Lever", "Lever (Employ Inc.)", 0.35, "semantic",
            {"formatting": .08, "keywords": .22, "sections": .10, "experience": .30,
             "education": .10, "quantification": .20},
            ["experience"], ["Month YYYY", "YYYY"], 50, False,
            [_q_lever_narrative, _q_lever_summary, _q_dates_for(["Month YYYY", "YYYY"], 0.35)],
            "Lever " + NO_AUTO_SCORE),
]
VENDOR_TO_PROFILE = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Greenhouse",
                     "workable": "iCIMS", "smartrecruiters": "iCIMS", "workday": "Workday",
                     "taleo": "Taleo", "icims": "iCIMS", "successfactors": "SuccessFactors"}


# ================================================================ scorers ==

def quantified(bullet: str) -> bool:
    return any(p.search(bullet) for p in QUANT_RES)


def starts_with_verb(bullet: str) -> bool:
    first = re.sub(r"[^a-z-]", "", bullet.strip().split()[0].lower()) if bullet.strip() else ""
    return first in ACTION_VERBS


def score_formatting(sig: Signals, strictness: float) -> dict:
    issues, details, ded = [], [], 0.0

    def hit(pen, issue, detail):
        nonlocal ded
        p = pen * strictness
        ded += p
        issues.append(issue)
        details.append(f"{detail} (-{round(p)})")

    if sig.multi_column:
        hit(15, "multi-column layout detected", "multi-column layouts read out of order")
    if sig.tables:
        hit(12, "tables detected", "content inside tables may be skipped")
    if sig.has_images:
        hit(8, "images or graphics detected", "text in images is invisible to a parser")
    if sig.pages > 2:
        hit(5, f"{sig.pages} pages", "longer than two pages may be truncated")
    if sig.words < 150:
        hit(10, "very short", f"only {sig.words} words -- parsing issue or thin content")
    elif sig.words > 1500:
        hit(3, "long", f"{sig.words} words is above average")
    if sig.special_char_ratio > 0.05:
        hit(8, "unusual characters", "high density of special characters suggests encoding problems")
    if sig.caps_lines > 3:
        hit(3, "excessive all-caps", f"{sig.caps_lines} lines fully uppercase")
    if sig.bullet_styles > 2:
        hit(2, "inconsistent bullets", f"{sig.bullet_styles} bullet styles")
    if not (sig.multi_column or sig.tables or sig.has_images):
        details.append("clean single-column layout (good)")
    if sig.pages <= 2:
        details.append("appropriate length (good)")
    if 300 <= sig.words <= 800:
        details.append("word count in the ideal range (good)")
    return {"score": max(0, min(100, round(100 - ded))), "issues": issues, "details": details}


def score_sections(sig: Signals, required: list[str]) -> dict:
    present = [r for r in required if r in sig.sections_present]
    missing = [r for r in required if r not in sig.sections_present]
    return {"score": round(100 * len(present) / max(1, len(required))),
            "present": present, "missing": missing}


def score_experience(bullets: list[str]) -> dict:
    if not bullets:
        return {"score": 0, "quantified": 0, "total": 0, "verbs": 0,
                "highlights": ["no experience bullets found"]}
    q = sum(quantified(b) for b in bullets)
    v = sum(starts_with_verb(b) for b in bullets)
    n = len(bullets)
    quant = min(1.0, (q / n) / 0.4) * 40
    verbs = min(1.0, (v / n) / 0.7) * 30
    count = 30 if n >= 8 else 25 if n >= 5 else 20 if n >= 3 else 10
    hl = []
    hl.append(f"{round(100 * q / n)}% of bullets are quantified"
              + (" (excellent)" if q / n >= .4 else " (good, aim for 40%+)" if q / n >= .2 else " -- add numbers"))
    hl.append("strong use of action verbs" if v / n >= .7
              else f"{round(100 * v / n)}% of bullets start with an action verb (aim for 70%+)")
    if n < 5:
        hl.append(f"only {n} bullets")
    return {"score": round(min(100, quant + verbs + count)), "quantified": q, "total": n,
            "verbs": v, "highlights": hl}


def score_education(text: str) -> dict:
    t = text.lower()
    notes, pts = [], 0
    if re.search(r"\b(bachelor|master|diploma|b\.?sc|m\.?sc|ph\.?d|mba|degree|igcse|a-level|bs|ba)\b", t):
        pts += 34
    else:
        notes.append("no degree word found")
    if re.search(r"\b(university|college|school|institute|academy)\b", t):
        pts += 33
    else:
        notes.append("no institution found")
    if re.search(r"\b(19|20)\d{2}\b", t):
        pts += 33
    else:
        notes.append("no graduation year found")
    return {"score": min(100, pts), "notes": notes}


def score_platform(sig: Signals, kw: KeywordMatch | None, profile: Profile) -> dict:
    fmt = score_formatting(sig, profile.strictness)
    sec = score_sections(sig, profile.required_sections)
    exp = score_experience(sig.bullets)
    edu = score_education(sig.education_text)
    quant = round(100 * exp["quantified"] / exp["total"]) if exp["total"] else 0
    parts = {"formatting": fmt["score"], "sections": sec["score"], "experience": exp["score"],
             "education": edu["score"], "quantification": quant}
    weights = dict(profile.weights)
    if kw is not None:
        parts["keywords"] = kw.score
    else:
        # no advert: drop the keyword weight and rescale the rest to 1
        w = weights.pop("keywords")
        weights = {k: v / (1 - w) for k, v in weights.items()}
    weighted = sum(parts[k] * weights[k] for k in weights)
    quirks = [q for q in (f(sig, kw) for f in profile.quirks) if q]
    total = max(0, min(100, round(weighted + sum(q.delta for q in quirks))))
    suggestions = []
    if fmt["score"] < 70:
        suggestions += [f"fix formatting: {i}" for i in fmt["issues"][:3]]
    if kw is not None and kw.score < 60 and kw.missing:
        suggestions.append(f"missing from the advert: {', '.join(kw.missing[:5])}")
        if profile.strategy == "exact":
            suggestions.append(f"{profile.name} matches exact terms -- use the advert's own words, not synonyms")
    if sec["missing"]:
        suggestions.append(f"add sections {profile.name} looks for: {', '.join(sec['missing'])}")
    if exp["total"]:
        if exp["quantified"] / exp["total"] < .3:
            suggestions.append("quantify more bullets (numbers, %, amounts)")
        if exp["verbs"] / exp["total"] < .5:
            suggestions.append("start more bullets with an action verb")
    if edu["score"] < 50:
        suggestions.append("education needs degree, institution and year")
    return {"name": profile.name, "vendor": profile.vendor, "score": total,
            "passing": profile.passing, "passes": total >= profile.passing,
            "auto_scores": profile.auto_scores, "strategy": profile.strategy,
            "breakdown": {"formatting": fmt, "keywords": ({"score": kw.score, "strategy": profile.strategy}
                                                          if kw is not None else None),
                          "sections": sec, "experience": exp, "education": edu,
                          "quantification": quant},
            "quirks": [asdict(q) for q in quirks], "suggestions": suggestions,
            "note": profile.note if not profile.auto_scores else ""}


# ============================================================ injectables ==

@dataclass
class Provenance:
    file: str
    line: int
    text: str


@dataclass
class Injectable:
    terms: dict[str, list[Provenance]]
    sentences: list[Provenance]
    skills_by_file: dict[str, dict[str, list[str]]]

    def has_sentence(self, s: str) -> bool:
        key = norm_sentence(s)
        return any(norm_sentence(p.text) == key for p in self.sentences)


def norm_sentence(s: str) -> str:
    return " ".join(strip_md(s).split()).lower().rstrip(".")


def injectable_files() -> list[Path]:
    files = sorted(RESUME.glob("resume-*.md"))
    files += [RESUME / f for f in INJECTABLE_FILES if (RESUME / f).exists()]
    return files


def injectable_set(files: list[Path] | None = None, tax: Taxonomy | None = None) -> Injectable:
    """Every fact already written down about him, with where it lives. An edit
    may only add a term that appears here, or a sentence that appears here."""
    tax = tax or taxonomy()
    files = files if files is not None else injectable_files()
    terms: dict[str, list[Provenance]] = {}
    sentences: list[Provenance] = []
    skills_by_file: dict[str, dict[str, list[str]]] = {}
    for p in files:
        text = p.read_text()
        if p.name.startswith("resume-"):
            try:
                skills_by_file[p.name] = load_cv(p).skills()
            except Exception:                         # noqa: BLE001
                pass
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or HR_RE.match(line):
                continue
            plain = strip_md(re.sub(r"^\s*(?:[-*•]\s+|>\s*)", "", line))
            if len(plain) >= 12:
                sentences.append(Provenance(p.name, n, plain))
            low = plain.lower()
            for t in tax.terms:
                if has_term(low, t):
                    terms.setdefault(t, []).append(Provenance(p.name, n, plain[:160]))
    return Injectable(terms, sentences, skills_by_file)


# =============================================================== improver ==

@dataclass
class Edit:
    id: str
    kind: str
    impact: str
    platforms: list[str]
    summary: str
    why: str
    evidence: list[Provenance]
    patch: dict | None
    needs_you: bool
    delta: dict[str, int]


TERM_TO_CATEGORY = [
    (r"python|typescript|javascript|sql|html|css|c\+\+|java\b|go\b|rust|bash|shell", "Languages"),
    (r"claude|llm|gpt|openai|rag|prompt|ollama|mlx|whisper|llama|generative|machine learning|ai\b", "AI"),
    (r"postgres|sqlite|mysql|excel|docker|kubernetes|aws|gcp|azure|linux|pdf|data|platform", "Data"),
    (r"asp\.net|\.net|node|react|n8n|serilog|dbup|framework|library|tool", "Frameworks"),
    (r"requirement|specification|stakeholder|uat|sdlc|agile|scrum|process|gap analysis|workshop|delivery"
     r"|documentation|strategy|reporting|analysis|planning|liaison|management|consult", "Solution"),
]


def pick_category(term: str, cv: CV, injectable: Injectable, source_file: str) -> str | None:
    cats = list(cv.skills().keys())
    if not cats:
        return None
    # the category the source variant filed it under, if this CV has a like-named one
    for cat, terms in injectable.skills_by_file.get(source_file, {}).items():
        if any(has_term(t.lower(), term) for t in terms):
            for mine in cats:
                if mine.lower().split()[0] == cat.lower().split()[0]:
                    return mine
    for pat, hint in TERM_TO_CATEGORY:
        if re.search(pat, term, re.I):
            for mine in cats:
                if hint.lower() in mine.lower():
                    return mine
    spoken = re.compile(r"spoken|^languages$", re.I)
    technical = [c for c in cats if not spoken.search(c)] or cats
    return max(technical, key=lambda c: len(cv.skills()[c]))


def improve(cv: CV, keywords: list[Keyword], injectable: Injectable, report: dict,
            profile_name: str | None = None) -> list[Edit]:
    """Ranked, evidence-backed edits. Every patch is a pure text operation on
    the markdown; every added term or sentence is one that already exists in
    resume/. What cannot be sourced comes back as [NEEDS YOU]."""
    edits: list[Edit] = []
    md = cv.raw
    text_l = plain_text(cv).lower()
    my_file = Path(cv.path).name if cv.path else ""
    base = {p["name"]: p["score"] for p in report["platforms"]}
    kw_by_term = {k.term: k for k in keywords}
    lines = md.splitlines()

    def add(kind, summary, why, evidence, patch, needs_you=False, platforms=()):
        edits.append(Edit(f"{kind}:{len(edits)}", kind, "low", list(platforms), summary, why,
                          evidence, patch, needs_you, {}))

    # 1. add_skill / needs_you
    for k in keywords:
        if has_term(text_l, k.term):
            continue
        prov = [p for p in injectable.terms.get(k.term, []) if p.file != my_file]
        if prov and k.kind == "skill":
            if count_term(text_l, k.term) >= 6:
                continue
            cat = pick_category(k.term, cv, injectable, prov[0].file)
            if cat is None:
                continue
            add("add_skill", f"Add \"{k.term}\" to the {cat} line",
                f"the advert asks for it {k.count}×{' (in the requirements)' if k.required else ''} "
                f"and you already claim it in {prov[0].file}",
                prov[:3], {"op": "append_skill", "category": cat, "term": k.term})
        elif k.weight >= 4 and not prov and not _any_form_present(text_l, k.term):
            add("needs_you", f"[NEEDS YOU] \"{k.term}\" -- asked {k.count}×"
                f"{', required' if k.required else ''}; nothing in resume/ mentions it",
                "only add it if it is true; an invented skill costs the interview", [], None,
                needs_you=True)

    # 2. exact_form: the CV has a synonym, the advert wants the exact word
    for k in keywords:
        if has_term(text_l, k.term) or k.kind != "skill":
            continue
        c = canonical(k.term)
        variants = {c, *SYNONYMS.get(c, set())} - {k.term}
        for v in variants:
            if not has_term(text_l, v):
                continue
            for i, ln in enumerate(lines):
                if len(_word_re(v).findall(ln)) == 1 and not ln.startswith("#"):
                    m = _word_re(v).search(ln)
                    old = m.group(0)
                    new = k.term if old.islower() else (k.term.upper() if old.isupper()
                                                       else k.term[:1].upper() + k.term[1:])
                    add("exact_form", f"Write \"{new}\" where line {i + 1} says \"{old}\"",
                        "exact-match parsers do not know they are the same thing",
                        [Provenance(my_file, i + 1, ln.strip()[:160])],
                        {"op": "replace_token", "line": i + 1, "old": old, "new": new},
                        platforms=["Workday", "Taleo", "SuccessFactors"])
                    break
            break

    # 3. lead_bullet: a required term buried below the first bullet
    req_terms = [k.term for k in keywords if k.required]
    for e in cv.experience():
        if len(e.bullets) < 2:
            continue
        first_has = any(has_term(e.bullets[0].lower(), t) for t in req_terms)
        if first_has:
            continue
        for idx, b in enumerate(e.bullets[1:], 1):
            hits = [t for t in req_terms if has_term(b.lower(), t)]
            if hits:
                add("lead_bullet", f"Lead {e.title} with the bullet that mentions {', '.join(hits[:2])}",
                    "a screener reads the first bullet of each role; the required term is in bullet "
                    f"{idx + 1}", [Provenance(my_file, e.bullet_lines[idx], b[:160])],
                    {"op": "move_bullet", "entry": e.key(), "from": idx, "to": 0})
                break

    # 4. reword_bullet: a sibling variant already says it with a verb
    sib = [p for p in injectable.sentences if p.file.startswith("resume-") and p.file != my_file]
    for e in cv.experience():
        for idx, b in enumerate(e.bullets):
            if starts_with_verb(b):
                continue
            mine = set(tokenize(b)) - JD_STOP
            best, best_j = None, 0.0
            for p in sib:
                if not starts_with_verb(p.text) or any(w in BANNED_WORDS for w in tokenize(p.text)):
                    continue
                theirs = set(tokenize(p.text)) - JD_STOP
                j = len(mine & theirs) / max(1, len(mine | theirs))
                if j > best_j:
                    best, best_j = p, j
            if best and best_j >= 0.6:
                add("reword_bullet", f"Reword bullet {idx + 1} of {e.title} to start with a verb",
                    f"your {best.file} already says the same thing as \"{best.text.split()[0]} …\"",
                    [best], {"op": "replace_bullet", "entry": e.key(), "index": idx,
                             "old": b, "new": best.text})

    # 5. header_rename: label only, never facts
    for s in cv.sections:
        if strict_header_type(s.header) is None:
            std = STANDARD_HEADER_FOR.get(s.header.lower())
            if std:
                add("header_rename", f"Rename \"{s.header}\" to \"{std}\"",
                    "exact parsers recognise a short list of header names; this one is not on it",
                    [Provenance(my_file, s.line, lines[s.line - 1].strip())],
                    {"op": "replace_line", "line": s.line, "old": lines[s.line - 1],
                     "new": f"## {std}"}, platforms=["Workday", "Taleo", "SuccessFactors"])

    # simulate each patchable edit and rank
    for ed in edits:
        if not ed.patch:
            continue
        new_md, applied, _ = apply_patches(md, [ed.patch])
        if not applied:
            ed.patch = None
            ed.needs_you = True
            ed.summary += " (patch no longer applies -- re-score)"
            continue
        sim = _platform_scores(parse_cv(new_md, cv.path), keywords)
        ed.delta = {name: sim[name] - base.get(name, 0) for name in sim}
        gain = max(ed.delta.values(), default=0)
        flips = any(sim[p["name"]] >= p["passing"] > p["score"] for p in report["platforms"]
                    if p["auto_scores"])
        own = ed.delta.get(profile_name, 0) if profile_name else 0
        ed.impact = "high" if flips or own >= 5 else "medium" if gain >= 2 else "low"
    rank = {"high": 0, "medium": 1, "low": 2}
    edits.sort(key=lambda e: (e.needs_you, rank[e.impact],
                              -kw_by_term.get(_edit_term(e), Keyword("", 0, "", 0, False)).weight))
    assert_no_fabrication(edits, injectable)
    needs = [e for e in edits if e.needs_you]
    return [e for e in edits if not e.needs_you] + needs[:6]


def _any_form_present(text_l: str, term: str) -> bool:
    c = canonical(term)
    forms = {term, c, *SYNONYMS.get(c, set())}
    return any(has_term(text_l, f) for f in forms)


def _edit_term(e: Edit) -> str:
    if e.patch and e.patch.get("op") == "append_skill":
        return e.patch["term"]
    m = re.search(r'"([^"]+)"', e.summary)
    return m.group(1) if m else ""


def _platform_scores(cv: CV, keywords: list[Keyword]) -> dict[str, int]:
    sig = signals_from_cv(cv)
    out = {}
    for p in PROFILES:
        kw = match_keywords(sig.text, keywords, p.strategy) if keywords else None
        out[p.name] = score_platform(sig, kw, p)["score"]
    return out


def assert_no_fabrication(edits: list[Edit], injectable: Injectable) -> None:
    for e in edits:
        if not e.patch:
            continue
        op = e.patch["op"]
        if op == "append_skill":
            if e.patch["term"].lower() not in injectable.terms:
                raise ValueError(f"fabrication: {e.patch['term']!r} is not in resume/")
        elif op == "replace_bullet":
            if not injectable.has_sentence(e.patch["new"]):
                raise ValueError("fabrication: replacement bullet is not a sentence from resume/")
        elif op == "replace_token":
            if canonical(e.patch["old"]) != canonical(e.patch["new"]):
                raise ValueError("fabrication: token swap is not a synonym pair")
        elif op == "replace_line":
            if not e.patch["new"].startswith("## "):
                raise ValueError("fabrication: replace_line may only rename a header")
        elif op != "move_bullet":
            raise ValueError(f"unknown patch op {op}")


# ---------------------------------------------------------------- patches --

def apply_patches(markdown: str, patches: list[dict]) -> tuple[str, list[str], list[dict]]:
    """Apply text patches, each verified by re-parsing. A patch that does not
    match exactly once, or that changes the CV's shape, is rejected with a
    reason and the previous text is kept. No fuzzy matching, no LLM."""
    applied, rejected = [], []
    text = markdown
    for patch in patches:
        pid = f"{patch.get('op')}:{patch.get('term') or patch.get('line') or patch.get('entry')}"
        try:
            new = _apply_one(text, patch)
        except ValueError as exc:
            rejected.append({"id": pid, "reason": str(exc)})
            continue
        text = new
        applied.append(pid)
    return text, applied, rejected


def _apply_one(text: str, patch: dict) -> str:
    op = patch.get("op")
    lines = text.splitlines()
    before = parse_cv(text)
    if op == "append_skill":
        cat, term = patch["category"], patch["term"]
        cand = [i for i, ln in enumerate(lines)
                if (m := SKILL_RE.match(ln)) and strip_md(m.group(1)).lower() == cat.lower()]
        if len(cand) != 1:
            raise ValueError(f"no single skills line for category {cat!r}")
        if has_term(lines[cand[0]].lower(), term):
            raise ValueError(f"{term!r} is already on that line")
        lines[cand[0]] = lines[cand[0]].rstrip() + f", {term}"
        out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        after = parse_cv(out)
        if not any(has_term(t.lower(), term) for t in after.skills().get(cat, [])):
            raise ValueError("term did not land in the parsed skills line")
    elif op == "replace_token":
        i = int(patch["line"]) - 1
        if not 0 <= i < len(lines) or lines[i].count(patch["old"]) != 1:
            raise ValueError(f"line {patch['line']} changed since this edit was proposed -- re-score")
        lines[i] = lines[i].replace(patch["old"], patch["new"])
        out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        after = parse_cv(out)
    elif op == "replace_line":
        i = int(patch["line"]) - 1
        if not 0 <= i < len(lines) or lines[i] != patch["old"]:
            raise ValueError(f"line {patch['line']} changed since this edit was proposed -- re-score")
        lines[i] = patch["new"]
        out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        after = parse_cv(out)
    elif op in ("move_bullet", "replace_bullet"):
        entry = next((e for e in before.experience() if e.key() == patch["entry"].lower()), None)
        if entry is None:
            raise ValueError(f"no experience entry {patch['entry']!r}")
        if op == "move_bullet":
            src, dst = int(patch["from"]), int(patch["to"])
            if not (0 <= src < len(entry.bullet_lines) and 0 <= dst < len(entry.bullet_lines)):
                raise ValueError("bullet index out of range")
            idxs = [n - 1 for n in entry.bullet_lines]
            moved = lines[idxs[src]]
            order = [lines[i] for i in idxs]
            order.pop(src)
            order.insert(dst, moved)
            for i, ln in zip(idxs, order):
                lines[i] = ln
        else:
            idx = int(patch["index"])
            if not 0 <= idx < len(entry.bullets) or entry.bullets[idx] != patch["old"]:
                raise ValueError("that bullet changed since this edit was proposed -- re-score")
            n = entry.bullet_lines[idx] - 1
            m = re.match(r"^(\s*[-*•]\s+)", lines[n])
            lines[n] = (m.group(1) if m else "- ") + patch["new"]
        out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        after = parse_cv(out)
        if len(after.bullets()) != len(before.bullets()):
            raise ValueError("bullet count changed")
    else:
        raise ValueError(f"unknown op {op!r}")
    if len(after.experience()) != len(before.experience()) or after.name != before.name:
        raise ValueError("the edit changed the CV's shape; refused")
    return out


# =================================================================== lint ==

def lint(cv: CV, jd_terms: list[str] = (), pages: int | None = None) -> list[dict]:
    out = []
    text = cv.raw
    lines = text.splitlines()

    def finding(rule, severity, line, snippet, fix):
        out.append({"rule": rule, "severity": severity, "line": line,
                    "text": snippet[:140], "fix": fix})

    # recruiter hygiene: things a human screener holds against the CV even
    # when every parser reads it cleanly
    for n, ln in enumerate(lines, 1):
        low = strip_md(ln).lower()
        if PERSONAL_DATA_RE.search(low):
            finding("personal-data", "medium", n, ln.strip(),
                    "remove age, date of birth, nationality, gender, religion and marital status")
        if "![" in ln:
            finding("photo", "medium", n, ln.strip(), "drop the photo; it invites bias and breaks some parsers")
        if re.search(r"\bspoken languages?\b", low):
            finding("spoken-languages", "low", n, ln.strip(),
                    "cut spoken languages for English-first employers unless the advert asks")
    for s in cv.sections:
        if re.match(r"references?\b", s.header.strip(), re.I):
            finding("references", "low", s.line, s.header,
                    "cut the section; employers ask for referees after the interview")
    edu = cv.section("education")
    grad_years = [int(y) for y in re.findall(r"\b(20\d{2})\b", " ".join(
        [*edu.paragraphs, *(entry_head(e) + " " + e.dates + " " + " ".join(e.body) for e in edu.entries)]))] if edu else []
    if pages and pages > 1 and grad_years and max(grad_years) >= date.today().year - 2:
        finding("new-grad-length", "medium", 0, f"{pages} pages, graduated {max(grad_years)}",
                "keep a new-grad CV to one page")

    for n, ln in enumerate(lines, 1):
        low = strip_md(ln).lower()
        for w in BANNED_WORDS:
            if has_term(low, w):
                if w == "landscape" and re.search(r"(threat|energy|network) landscape", low):
                    continue
                finding("banned-word", "medium", n, ln.strip(), f"replace \"{w}\" with a plain word")
        for ph in BANNED_PHRASES:
            if ph in low:
                finding("banned-phrase", "medium", n, ln.strip(), f"cut \"{ph}\" and state what you did")
    dashes = sum(ln.count("—") for ln in lines if not ln.startswith("#") and not BOLD_HEAD_RE.match(ln.strip()))
    if dashes > 2:
        finding("em-dash-count", "low", 0, f"{dashes} em-dashes in body text", "keep at most two; use commas or full stops")
    triplets = sum(1 for ln in lines if re.search(r"\b\w+, \w+,? and \w+\b", ln))
    if triplets > 2:
        finding("triplets", "low", 0, f"{triplets} \"X, Y and Z\" constructions", "vary the rhythm: pairs, singles, lists of four")
    bullets = cv.bullets()
    passive = sum(1 for _, b in bullets if re.search(r"\b(was|were|been|being)\s+\w+ed\b", b))
    if bullets and passive / len(bullets) > 0.2:
        finding("passive-voice", "medium", 0, f"{passive} of {len(bullets)} bullets are passive", "lead with what you did")
    for e, b in bullets:
        n = e.bullet_lines[e.bullets.index(b)] if b in e.bullets else 0
        last = b.rstrip(".").split()[-1] if b.split() else ""
        if last.lower().endswith("ing") and not re.search(r"\d", " ".join(b.split()[-4:])):
            finding("ing-ending", "medium", n, b, "end on a result, metric or object, not an -ing phrase")
        if len(b) > BUDGET["two_line_max"]:
            finding("bullet-too-long", "medium", n, b, f"{len(b)} chars; hard max is {BUDGET['two_line_max']}")
        elif BUDGET["awkward_zone"][0] <= len(b) <= BUDGET["awkward_zone"][1]:
            finding("bullet-orphan-zone", "info", n, b, f"{len(b)} chars spills to a short second line; aim for ≤{BUDGET['one_line']} or {BUDGET['two_line_target'][0]}–{BUDGET['two_line_target'][1]}")
    plain = plain_text(cv).lower()
    for t in {*(x.lower() for x in jd_terms), *(s.lower() for s in taxonomy().terms if len(s) > 3)}:
        c = count_term(plain, t)
        if c > 8:
            finding("keyword-stuffing", "medium", 0, f"\"{t}\" appears {c}×", "over eight repeats reads as stuffing")
        elif 6 <= c <= 8:
            finding("keyword-borderline", "info", 0, f"\"{t}\" appears {c}×", "borderline; do not add more")
    for s in cv.sections:
        if strict_header_type(s.header) is None:
            finding("non-standard-header", "info", s.line, s.header,
                    f"exact parsers may not recognise this header"
                    + (f"; \"{STANDARD_HEADER_FOR[s.header.lower()]}\" is the standard name"
                       if s.header.lower() in STANDARD_HEADER_FOR else ""))
    return out


# ================================================================= report ==

def score_all(cv: CV, jd: str | None, pdf: PdfText | None, exclude: list[str] = ()) -> dict:
    sig = signals_from_pdf(cv, pdf) if pdf else signals_from_cv(cv)
    keywords = jd_keywords(jd, exclude=exclude) if jd else []
    by_strategy = {}
    platforms = []
    for p in PROFILES:
        kw = match_keywords(sig.text, keywords, p.strategy) if keywords else None
        if kw is not None:
            by_strategy[p.strategy] = kw.score
        platforms.append(score_platform(sig, kw, p))
    kw_sem = match_keywords(sig.text, keywords, "semantic") if keywords else None
    stuffing = []
    plain = sig.text.lower()
    for k in keywords:
        c = count_term(plain, k.term)
        if c > 8:
            stuffing.append({"term": k.term, "count": c})
    return {
        "signals": {"source": sig.source, "words": sig.words, "pages": sig.pages,
                    "has_images": sig.has_images, "multi_column": sig.multi_column,
                    "tables": sig.tables, "date_formats": sig.date_formats,
                    "entry_date_formats": sig.entry_date_formats,
                    "sections_present": sig.sections_present,
                    "strict_unknown_headers": sig.strict_unknown_headers,
                    "bullets": len(sig.bullets), "quantified": sum(quantified(b) for b in sig.bullets),
                    "action_verbs": sum(starts_with_verb(b) for b in sig.bullets),
                    "skills_count": len(sig.skills), "contact": sig.contact,
                    "column_evidence": sig.column_evidence},
        "keywords": None if not keywords else {
            "total": len(keywords), "score_by_strategy": by_strategy,
            "matched": kw_sem.matched, "synonym": kw_sem.synonym,
            "missing": [{"term": k.term, "count": k.count, "weight": round(k.weight, 1),
                         "required": k.required, "kind": k.kind}
                        for k in keywords if kw_sem.per_term.get(k.term) == "missing"],
            "terms": [asdict(k) for k in keywords], "stuffing": stuffing},
        "platforms": platforms,
        "_keywords": keywords,
    }


def report_for_text(variant: str, jd_text: str | None, job: dict | None,
                    pdf: Path | str | None = None) -> dict:
    path = variant_path(variant)
    if not path.exists():
        raise FileNotFoundError(f"no such variant: {path}")
    cv = load_cv(path)
    pdf_obj = pdf_text(pdf) if pdf else None
    rep = score_all(cv, jd_text, pdf_obj, exclude=[job["company"]] if job and job.get("company") else [])
    keywords = rep.pop("_keywords")
    profile_name = None
    if job and job.get("vendor"):
        profile_name = VENDOR_TO_PROFILE.get(job["vendor"])
    for p in rep["platforms"]:
        p["this_job"] = p["name"] == profile_name
    inj = injectable_set()
    edits = improve(cv, keywords, inj, rep, profile_name)
    if rep["keywords"]:
        for m in rep["keywords"]["missing"]:
            m["injectable"] = any(p.file != path.name for p in inj.terms.get(m["term"], []))
    out = {
        "variant": variant, "path": str(path),
        "job": ({**job, "profile": profile_name} if job else None),
        "pdf": ({"analysed": True, "path": str(pdf), "pages": pdf_obj.pages,
                 "extractor": pdf_obj.extractor, "warnings": pdf_obj.warnings}
                if pdf_obj else {"analysed": False,
                                 "reason": ("no PDF given" if not pdf else
                                            "pypdf not installed: pip install pypdf"
                                            if Path(str(pdf)).exists() else f"{pdf} does not exist")}),
        **rep,
        "edits": [{**asdict(e), "evidence": [asdict(p) for p in e.evidence]} for e in edits],
        "lint": lint(cv, [k.term for k in keywords], rep["signals"]["pages"]),
        "notes": ["Greenhouse and Lever " + NO_AUTO_SCORE,
                  "Workday, Taleo, iCIMS and SuccessFactors parse into structured fields and "
                  "support knockout filters; their pass marks here are thresholds ported from an "
                  "open-source screener (MIT), not the vendors' own numbers."],
    }
    return out


def self_report(variant: str = CANONICAL, pdf: Path | str | None = None) -> dict:
    return report_for_text(variant, None, None, pdf)


def build(conn, job_id: int, variant: str | None = None, pdf: Path | str | None = None) -> dict | None:
    row = conn.execute("""
        SELECT j.id, j.title, j.company, jt.description,
               (SELECT p.source_id FROM posting p WHERE p.job_id = j.id
                 AND p.source = 'ats' LIMIT 1) sid,
               (SELECT p.source FROM posting p WHERE p.job_id = j.id LIMIT 1) src
        FROM job j LEFT JOIN job_text jt ON jt.job_id = j.id WHERE j.id = ?""",
        (job_id,)).fetchone()
    if not row:
        return None
    vendor = None
    sid = row["sid"] or ""
    if ":" in sid and sid.split(":", 1)[0] in VENDOR_TO_PROFILE:
        vendor = sid.split(":", 1)[0]
    job = {"id": row["id"], "title": row["title"], "company": row["company"], "vendor": vendor}
    variant = variant or CANONICAL
    pdf = pdf if pdf is not None else default_pdf(variant)
    return report_for_text(variant, f"{row['title']}\n{row['description'] or ''}", job, pdf)


# ==================================================================== CLI ==

def render_text(rep: dict) -> str:
    out = [f"{rep['variant']}  ·  {rep['path']}"]
    if rep["job"]:
        j = rep["job"]
        out.append(f"against #{j['id']} {j['title']} — {j['company']}"
                   f"{'  (' + j['vendor'] + ' → ' + str(j['profile']) + ')' if j.get('vendor') else ''}")
    p = rep["pdf"]
    if p["analysed"]:
        out.append(f"pdf: analysed {p['path']} ({p['pages']} pages)")
    else:
        out.append(f"pdf: not analysed — {p['reason']}")
    s = rep["signals"]
    out.append(f"signals: {s['words']} words · {s['pages']} page(s) · columns={s['multi_column']} "
               f"tables={s['tables']} images={s['has_images']} · {s['bullets']} bullets, "
               f"{s['quantified']} quantified, {s['action_verbs']} verb-led · {s['skills_count']} skills")
    if s["strict_unknown_headers"]:
        out.append(f"non-standard headers: {', '.join(s['strict_unknown_headers'])}")
    out.append("")
    out.append(f"{'platform':<16}{'score':>6}{'pass':>7}   note")
    for pl in rep["platforms"]:
        flag = "✓" if pl["passes"] else "✗"
        tag = " ← this job" if pl.get("this_job") else ""
        note = "" if pl["auto_scores"] else "no auto-scoring; findability estimate"
        out.append(f"{pl['name']:<16}{pl['score']:>6}{flag + ' ' + str(pl['passing']):>7}   {note}{tag}")
    if rep["keywords"]:
        k = rep["keywords"]
        out.append("")
        out.append(f"keywords: {k['total']} from the advert · exact {k['score_by_strategy'].get('exact')} "
                   f"/ fuzzy {k['score_by_strategy'].get('fuzzy')} / semantic {k['score_by_strategy'].get('semantic')}")
        miss = [m for m in k["missing"]]
        if miss:
            out.append("missing: " + ", ".join(
                f"{m['term']}{'*' if m['required'] else ''}{' (in resume/)' if m.get('injectable') else ''}"
                for m in miss[:12]))
    if rep["edits"]:
        out.append("")
        out.append("edits (evidence in brackets):")
        for e in rep["edits"][:10]:
            tag = "NEEDS YOU" if e["needs_you"] else e["impact"]
            ev = f"  [{e['evidence'][0]['file']}:{e['evidence'][0]['line']}]" if e["evidence"] else ""
            delta = ""
            if e["delta"]:
                best = max(e["delta"].items(), key=lambda kv: kv[1])
                if best[1]:
                    delta = f"  (+{best[1]} {best[0]})"
            out.append(f"  {tag:<9} {e['summary']}{delta}{ev}")
    if rep["lint"]:
        out.append("")
        out.append(f"lint: {len(rep['lint'])} finding(s)")
        rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
        for f in sorted(rep["lint"], key=lambda f: rank.get(f["severity"], 4))[:8]:
            out.append(f"  {f['severity']:<6} {f['rule']:<20} L{f['line']:<4} {f['text'][:70]}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_id", nargs="?", type=int)
    ap.add_argument("--self", dest="self_", action="store_true", help="score the CV alone")
    ap.add_argument("--jd", help="a text file holding an advert")
    ap.add_argument("--variant", default=CANONICAL)
    ap.add_argument("--pdf", help="a rendered PDF to analyse instead of the markdown projection")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--parse", action="store_true", help="dump the parsed model and exit")
    args = ap.parse_args(argv)

    if args.parse:
        cv = load_cv(variant_path(args.variant))
        print(json.dumps(cv.to_dict(), indent=1)[:20000])
        return 0
    if args.jd:
        rep = report_for_text(args.variant, Path(args.jd).read_text(), None, args.pdf)
    elif args.job_id:
        sys.path.insert(0, str(HERE))
        from db import session
        with session() as conn:
            rep = build(conn, args.job_id, args.variant, args.pdf)
        if rep is None:
            print(f"no job {args.job_id}", file=sys.stderr)
            return 1
    else:
        rep = self_report(args.variant, args.pdf or default_pdf(args.variant))
    print(json.dumps(rep, indent=1, default=str) if args.json else render_text(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
