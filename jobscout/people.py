#!/usr/bin/env python3
"""Add anyone's CV and find jobs for them.

    python3 people.py add path/to/cv.pdf --name ajey --floor 800
    python3 people.py list
    python3 people.py show ajey
    python3 run.py --person ajey            # their daily pass, their database

`add` reads the CV (PDF, Word .docx, Markdown or plain text), asks Claude for a
profile through the Claude Code CLI (`claude -p`, one call, on your own
subscription), and writes a folder under `people/` that every stage then reads
instead of the owner's files. See person.py for how the switch works.

Read the profile before the first run. It is a model's reading of a CV: the
target titles decide what counts as "their field", so a wrong one changes the
whole shortlist. `show` prints it; edit `people/<name>/profile.yml` to fix it.

If `claude` is not installed, `add` still writes the folder with an empty
profile to fill in by hand, and says so.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from html import unescape
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import person                               # noqa: E402

TUNED_FOR = "LK"    # eligibility.py's region tables are written for Sri Lanka

PROFILE_FIELDS = {
    "name": "", "headline": "", "country": "", "city": "", "utc_offset": 0.0,
    "seniority": "junior", "years_experience": 0,
    "target_titles": [], "adjacent_titles": [], "avoid_titles": [],
    "moving_into": [], "skills": [],
}

PROMPT = """You are reading a CV to set up a job search for this person.
Return ONLY a JSON object, no prose, with exactly these keys:

  "name": full name as written,
  "headline": the role they are aiming for, in a few words,
  "country": ISO 3166-1 alpha-2 code of where they live (e.g. "LK"),
  "city": the city they live in,
  "utc_offset": that city's UTC offset in hours as a number (e.g. 5.5),
  "seniority": one of "junior", "mid", "senior",
  "years_experience": total professional years as a number,
  "target_titles": 6 to 15 lowercase job-title words or phrases they could be
      hired for today, from what the CV shows they have done
      (e.g. "data analyst", "developer", "solutions engineer"),
  "adjacent_titles": up to 10 titles they could plausibly move into but have
      not done yet,
  "avoid_titles": up to 15 professions that share words with theirs but are a
      different job (e.g. for a developer: "sales", "nurse", "driver"),
  "moving_into": [] unless the CV says they are retraining into a field,
  "skills": 15 to 40 lowercase skills, tools and domain words from the CV.

Use only what the CV supports. Do not invent experience.

CV:
"""


# ------------------------------------------------------------------ reading --

def read_cv(path: Path) -> str:
    """Plain text from a CV file. Raises SystemExit with a reason if it can't."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt", ".markdown"):
        return path.read_text(errors="replace")
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return unescape(re.sub(r"<[^>]+>", "", xml))
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except ImportError:
            pass
        try:
            from pdfminer.high_level import extract_text
            return extract_text(str(path))
        except ImportError:
            raise SystemExit("reading a PDF needs pypdf or pdfminer.six: "
                             "pip install -r requirements.txt")
    raise SystemExit(f"can't read {suffix or 'that'} files: use PDF, .docx, .md or .txt")


def parse_profile(raw: str) -> dict:
    """The first JSON object in the model's reply, checked against the fields
    every stage relies on. Missing keys take a safe default; wrong types are
    refused, because a string where a list belongs would quietly match nothing."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    data = json.loads(m.group(0))
    out = dict(PROFILE_FIELDS)
    for key, default in PROFILE_FIELDS.items():
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if isinstance(default, list):
            if not isinstance(value, list):
                raise ValueError(f"{key} should be a list")
            out[key] = [str(v).strip().lower() for v in value if str(v).strip()]
        elif isinstance(default, float) or key == "years_experience":
            out[key] = float(value)
        else:
            out[key] = str(value).strip()
    out["seniority"] = out["seniority"].lower() if out["seniority"].lower() in (
        "junior", "mid", "senior") else "junior"
    out["country"] = out["country"].upper()[:2]
    return out


def ask_claude(cv_text: str) -> dict | None:
    """One `claude -p` call. None when the CLI is missing or the reply is unusable."""
    if not shutil.which("claude"):
        return None
    try:
        reply = subprocess.run(["claude", "-p", PROMPT + cv_text[:20000]],
                               capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"claude -p failed: {e}", file=sys.stderr)
        return None
    if reply.returncode != 0:
        print(f"claude -p exited {reply.returncode}: {reply.stderr.strip()[:300]}",
              file=sys.stderr)
        return None
    try:
        return parse_profile(reply.stdout)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"couldn't use Claude's reply ({e}); fill the profile in by hand",
              file=sys.stderr)
        return None


# ------------------------------------------------------------------ writing --

def write_settings(folder: Path, prof: dict, floor: float | None) -> None:
    """Their settings.yml: the example file with their details swapped in."""
    cfg = yaml.safe_load((HERE / "settings.example.yml").read_text())
    cand = cfg.setdefault("candidate", {})
    cand["name"] = prof["name"] or cand.get("name", "")
    cand["email"], cand["phone"], cand["linkedin"] = "", "", ""
    if prof["country"]:
        cand["country"] = prof["country"]
    if prof["city"]:
        cand["city"] = prof["city"]
    if prof["utc_offset"] or prof["country"]:
        cand["utc_offset"] = prof["utc_offset"]
    if floor is not None:
        cfg["pay"]["floor_usd_month"] = floor
    (folder / "settings.yml").write_text(
        "# Written by people.py add. Fill in email, phone and LinkedIn if the\n"
        "# application packs should carry them.\n"
        + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def add(cv: Path, slug: str, floor: float | None, force: bool) -> int:
    if not person.SLUG_RE.match(slug):
        raise SystemExit("--name: lowercase letters, digits and dashes only")
    if not cv.is_file():
        raise SystemExit(f"no such file: {cv}")
    folder = person.PEOPLE / slug
    if folder.exists() and not force:
        raise SystemExit(f"people/{slug} already exists; --force to replace its "
                         f"profile and CV (their database is kept)")
    text = read_cv(cv).strip()
    if len(text) < 200:
        raise SystemExit("got almost no text from that file. A scanned PDF has "
                         "no text layer: export the CV as a text PDF or .docx")

    (folder / "resume").mkdir(parents=True, exist_ok=True)
    prof = ask_claude(text)
    from_model = prof is not None
    prof = prof or dict(PROFILE_FIELDS)

    name = prof["name"] or slug
    (folder / "resume" / f"resume-{person.CV_SLUG}.md").write_text(
        text if text.lstrip().startswith("#") else f"# {name}\n\n{text}\n")
    (folder / "profile.yml").write_text(
        "# Read this before the first run. target_titles decide what counts as\n"
        "# their field; a wrong one changes the whole shortlist.\n"
        + yaml.safe_dump(prof, sort_keys=False, allow_unicode=True))
    write_settings(folder, prof, floor)

    print(f"added people/{slug}")
    if not from_model:
        print("  profile.yml is EMPTY: claude was not available. Fill in at least "
              "target_titles, skills, country and utc_offset before running.")
    show(slug)
    if prof["country"] and prof["country"] != TUNED_FOR:
        print(f"\n  heads up: they live in {prof['country']}. The eligibility gate "
              f"is tuned for {TUNED_FOR}, so 'which regions include them' will be "
              f"rough until eligibility.py learns their country.")
    print(f"\nnext:  python3 run.py --person {slug}")
    return 0


def show(slug: str) -> int:
    folder = person.PEOPLE / slug
    if not folder.is_dir():
        raise SystemExit(f"no person '{slug}'")
    prof = yaml.safe_load((folder / "profile.yml").read_text()) or {}
    floor = (yaml.safe_load((folder / "settings.yml").read_text()) or {}) \
        .get("pay", {}).get("floor_usd_month")
    print(f"  {prof.get('name') or slug}: {prof.get('headline') or 'no headline'}")
    print(f"  {prof.get('city') or '?'}, {prof.get('country') or '?'} "
          f"(UTC{float(prof.get('utc_offset') or 0):+g}), {prof.get('seniority')}, "
          f"{prof.get('years_experience') or 0:g} years, floor ${floor}/month")
    for key in ("target_titles", "adjacent_titles", "avoid_titles", "skills"):
        values = prof.get(key) or []
        print(f"  {key:16} {', '.join(values) if values else '(none)'}")
    return 0


def list_people() -> int:
    folders = sorted(p for p in person.PEOPLE.glob("*") if (p / "profile.yml").exists())
    if not folders:
        print("nobody added yet: python3 people.py add <cv> --name <name>")
    for p in folders:
        prof = yaml.safe_load((p / "profile.yml").read_text()) or {}
        has_db = "has run" if (p / "jobscout.db").exists() else "not run yet"
        print(f"  {p.name:16} {prof.get('headline') or '':40} {has_db}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="read a CV and set up a person")
    a.add_argument("cv", type=Path)
    a.add_argument("--name", required=True, help="short id, e.g. ajey")
    a.add_argument("--floor", type=float, help="salary floor, USD a month")
    a.add_argument("--force", action="store_true")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("name")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        return add(args.cv, args.name, args.floor, args.force)
    if args.cmd == "show":
        return show(args.name)
    return list_people()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
