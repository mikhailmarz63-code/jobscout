#!/usr/bin/env python3
"""The advert's own application form, fetched rather than guessed.

`kit.py` used to build a pack around seven questions it assumed a form would
ask. Every form is different, and the difference was retyped by hand every
time. Greenhouse publishes the real one: the same board API `sources/ats.py`
already reads returns the entire application form -- every field, whether it is
required, its type, and the options behind each dropdown -- for
`?questions=true`. It costs one request and no credentials.

Only Greenhouse does. Ashby's form endpoint answers `Unauthorized`, and Lever's
public posting carries an `applyUrl` and nothing else. So this covers about a
third of the viable board, and the rest keeps the generic pack. That is a
smaller number than it sounds: Greenhouse is the single biggest source of
viable jobs here by a wide margin.

Nothing here submits anything. It reads a form and remembers it.

    python3 forms.py 16211           # the real form for one job
    python3 forms.py --fetch 40      # cache forms for the top N viable jobs
    python3 forms.py --status        # coverage, and what is left to fetch
    python3 forms.py --blockers      # forms whose own questions rule him out
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402
from sources import get, settings           # noqa: E402

GH_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{ident}"

# Ashby publishes the form only through the endpoint its own apply page calls.
# No key, no login -- the same JSON any visitor's browser receives.
ASHBY_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
ASHBY_QUERY = (
    "query ApplyFormJob($organizationHostedJobsPageName: String!, "
    "$jobPostingId: String!) { jobPosting("
    "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { title applicationForm { sections { "
    "fieldEntries { field isRequired } } } } }")

# A posting is `greenhouse:6145795004` or `ashby:<uuid>` in source_id, and the
# board slug only exists in the URL. Both halves are needed and neither column
# has both.
SLUG_RE = {
    "greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"ashbyhq\.com/([a-z0-9_.-]+)", re.I),
}


# --------------------------------------------------------------- questions --

# Questions whose honest answer closes the door. Each is (pattern, the answer
# he would have to give, why it is fatal). This is the second gate: the advert
# can be silent about location and still ask, on the form, whether you are
# authorised to work in a country he has no right to work in. Hungryroot's
# prose was the only warning on that one, and the prose is not always there.
# A sponsorship question contains "right to work" too -- "Will you require
# sponsorship for your right to work?" -- and it is the opposite case: the
# honest answer is *yes*, and plenty of companies still hire. Treating it as a
# blocker silently deleted four ClickHouse roles he could actually get, which
# is the expensive direction to be wrong in.
SPONSORSHIP_Q = re.compile(r"sponsor", re.I)

BLOCKING = [
    (re.compile(r"authoriz\w+ to work in|legally (?:able|entitled) to work|"
                r"right to work in", re.I),
     "No",
     "asks for work authorisation he does not have"),
    (re.compile(r"(?:currently )?(?:reside|residing|located|living|based) in "
                r"(?:the )?(?:us|u\.s\.|united states|uk|united kingdom|canada|eu)",
                re.I),
     "No",
     "requires residence in a country he is not in"),
]

# Questions that need his judgement rather than a template. Not fatal, but the
# pack must not answer them for him.
FLAGGED = [
    (re.compile(r"in[- ]person|on[- ]?site|relocat|come into the office", re.I),
     "asks about being physically present"),
    (re.compile(r"require .{0,20}sponsorship|need .{0,20}sponsorship", re.I),
     "sponsorship question -- answer is yes, and that is often the filter"),
    # Added for the "repeated questions" panel guard list: self-identification
    # and a salary figure are his to answer, never auto-filled, whatever kit.py
    # can honestly compute for the copy-paste pack. kit.GUARD_KINDS enforces
    # the same rule on the browser extension's one-click fill.
    (re.compile(r"\bgender\b|\bsex\b(?!ual)|\brace\b|ethnicit\w+|\bveteran\b|"
                r"disabilit\w+|hispanic or latino|self[- ]identif", re.I),
     "self-identification -- answer yourself, never auto-filled"),
    (re.compile(r"salary expectation|expected salary|desired salary|"
                r"compensation expectation|what are your salary", re.I),
     "salary expectation -- answer yourself, never auto-filled"),
]


def _ref(source_id: str, url: str) -> tuple[str, str, str] | None:
    """(vendor, board slug, the vendor's own job id), or None if unsupported."""
    vendor = (source_id or "").split(":", 1)[0]
    if vendor not in SLUG_RE:
        return None
    ident = source_id.split(":", 1)[1] if ":" in source_id else ""
    found = SLUG_RE[vendor].search(url or "")
    if not found or not ident:
        return None
    if vendor == "greenhouse" and not ident.isdigit():
        return None
    return (vendor, found.group(1), ident)


def parse_ashby(payload: dict) -> list[dict]:
    """Ashby's shape -> ours. `field` is a raw JSON scalar, and `isRequired`
    lives on the entry rather than inside it."""
    posting = ((payload or {}).get("data") or {}).get("jobPosting") or {}
    out = []
    for section in (posting.get("applicationForm") or {}).get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            field = entry.get("field") or {}
            if not field.get("title"):
                continue
            out.append({
                "label": field["title"].strip(),
                "required": 1 if entry.get("isRequired") else 0,
                "field_type": field.get("type") or "String",
                "values": [v.get("label") for v in
                           (field.get("selectableValues") or []) if v.get("label")],
            })
    return out


def parse(payload: dict) -> list[dict]:
    """Greenhouse's shape -> ours. One row per field, in the form's own order.

    A question can carry more than one field (a compound address, say). Each
    becomes its own row, because the pack answers fields, not questions.
    """
    out = []
    for question in payload.get("questions") or []:
        label = (question.get("label") or "").strip()
        required = 1 if question.get("required") else 0
        for field in question.get("fields") or []:
            values = [v.get("label") for v in (field.get("values") or [])
                      if v.get("label")]
            out.append({
                "label": label,
                "required": required,
                "field_type": field.get("type") or "input_text",
                "values": values,
            })
    return out


def classify(rows: list[dict]) -> tuple[list[str], list[str]]:
    """(blockers, flags) for one form, as plain sentences."""
    blockers, flags = [], []
    for row in rows:
        if not row["required"]:
            continue
        if SPONSORSHIP_Q.search(row["label"]):
            continue
        for pattern, answer, why in BLOCKING:
            if pattern.search(row["label"]):
                blockers.append(f'"{row["label"]}" — honest answer is '
                                f'"{answer}"; {why}')
                break
    for row in rows:
        for pattern, why in FLAGGED:
            if pattern.search(row["label"]):
                flags.append(f'"{row["label"]}" — {why}')
                break
    return blockers, flags


# ------------------------------------------------------------------ store --

def cached(conn, job_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT label, required, field_type, values_json FROM form_question "
        "WHERE job_id = ? ORDER BY position", (job_id,)).fetchall()
    return [{"label": r["label"], "required": r["required"],
             "field_type": r["field_type"],
             "values": json.loads(r["values_json"] or "[]")} for r in rows]


def _store(conn, job_id: int, rows: list[dict], vendor: str = "greenhouse") -> None:
    stamp = now()
    conn.execute("DELETE FROM form_question WHERE job_id = ?", (job_id,))
    conn.executemany(
        "INSERT INTO form_question (job_id, position, label, required, "
        "field_type, values_json, vendor, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        [(job_id, i, r["label"], r["required"], r["field_type"],
          json.dumps(r["values"]), vendor, stamp) for i, r in enumerate(rows)])
    conn.execute("DELETE FROM form_miss WHERE job_id = ?", (job_id,))
    conn.commit()


def _miss(conn, job_id: int, reason: str) -> None:
    conn.execute("INSERT INTO form_miss (job_id, reason, tried_at) VALUES (?,?,?) "
                 "ON CONFLICT (job_id) DO UPDATE SET reason = excluded.reason, "
                 "tried_at = excluded.tried_at", (job_id, reason, now()))
    conn.commit()


# ------------------------------------------------------------------ fetch --

def fetch_one(conn, job_id: int, force: bool = False, cfg=None) -> list[dict]:
    """The form for one job. Cached unless `force`. [] when there is none."""
    if not force:
        existing = cached(conn, job_id)
        if existing:
            return existing

    ref = None
    for row in conn.execute(
            "SELECT p.source_id, p.url FROM posting p WHERE p.job_id = ?",
            (job_id,)):
        ref = _ref(row["source_id"], row["url"])
        if ref:
            break
    if not ref:
        _miss(conn, job_id, "no board that publishes a form")
        return []

    vendor, slug, ident = ref
    if vendor == "greenhouse":
        payload = get(GH_URL.format(slug=slug, ident=ident),
                      params={"questions": "true"}, cfg=cfg)
        rows = parse(payload) if isinstance(payload, dict) else None
    else:
        payload = get(ASHBY_URL, params={"op": "ApplyFormJob"}, cfg=cfg,
                      json_body={"operationName": "ApplyFormJob",
                                 "variables": {
                                     "organizationHostedJobsPageName": slug,
                                     "jobPostingId": ident},
                                 "query": ASHBY_QUERY})
        rows = parse_ashby(payload) if isinstance(payload, dict) else None
    if rows is None:
        _miss(conn, job_id, "board did not answer")
        return []
    if not rows:
        _miss(conn, job_id, "no questions published")
        return []
    _store(conn, job_id, rows, vendor)
    return rows


def due(conn, limit: int) -> list[int]:
    """Top viable jobs with no form on file and no recorded miss."""
    return [r["id"] for r in conn.execute("""
        SELECT j.id FROM job j
        JOIN fit f ON f.job_id = j.id
        JOIN score sc ON sc.job_id = j.id
        JOIN posting p ON p.job_id = j.id AND (p.source_id LIKE 'greenhouse:%'
                                            OR p.source_id LIKE 'ashby:%')
        LEFT JOIN form_question q ON q.job_id = j.id
        LEFT JOIN form_miss m ON m.job_id = j.id
        WHERE f.viable = 1 AND q.job_id IS NULL AND m.job_id IS NULL
        GROUP BY j.id ORDER BY sc.total DESC LIMIT ?""", (limit,))]


def fetch_many(conn, limit: int, cfg=None) -> dict:
    started = now()
    got = missed = 0
    for job_id in due(conn, limit):
        if fetch_one(conn, job_id, cfg=cfg):
            got += 1
        else:
            missed += 1
    detail = json.dumps({"fetched": got, "no_form": missed})
    log_stage(conn, "forms", True, detail, started)
    return {"fetched": got, "no_form": missed}


# ----------------------------------------------------------------- render --

def render(conn, job_id: int) -> str:
    job = conn.execute("SELECT title, company, canonical_url FROM job WHERE id = ?",
                       (job_id,)).fetchone()
    if not job:
        return f"no job {job_id}"
    rows = cached(conn, job_id)
    out = [f"# {job['title']}", f"**{job['company']}**  ·  job {job_id}"]
    if job["canonical_url"]:
        out.append(job["canonical_url"])
    if not rows:
        miss = conn.execute("SELECT reason FROM form_miss WHERE job_id = ?",
                            (job_id,)).fetchone()
        out.append(f"\nNo form on file — {miss['reason'] if miss else 'not fetched yet'}.")
        return "\n".join(out)

    required = sum(r["required"] for r in rows)
    out.append(f"\n{len(rows)} fields, {required} required\n")
    for i, r in enumerate(rows, 1):
        mark = "*" if r["required"] else " "
        out.append(f"{i:>3}.{mark} {r['label']}   <{r['field_type']}>")
        for value in r["values"][:6]:
            out.append(f"        · {value}")

    blockers, flags = classify(rows)
    if blockers:
        out.append("\nBLOCKERS — the form itself rules him out:")
        out.extend(f"  ✗ {b}" for b in blockers)
    if flags:
        out.append("\nNeeds his own answer:")
        out.extend(f"  ? {f}" for f in flags)
    return "\n".join(out)


def status(conn) -> str:
    q = lambda sql: conn.execute(sql).fetchone()[0]      # noqa: E731
    viable = q("SELECT COUNT(*) FROM fit WHERE viable = 1")
    gh = q("""SELECT COUNT(DISTINCT j.id) FROM job j JOIN fit f ON f.job_id = j.id
              JOIN posting p ON p.job_id = j.id
              WHERE f.viable = 1 AND (p.source_id LIKE 'greenhouse:%'
                                   OR p.source_id LIKE 'ashby:%')""")
    have = q("""SELECT COUNT(DISTINCT q.job_id) FROM form_question q
                JOIN fit f ON f.job_id = q.job_id WHERE f.viable = 1""")
    missed = q("""SELECT COUNT(*) FROM form_miss m JOIN fit f ON f.job_id = m.job_id
                  WHERE f.viable = 1""")
    return (f"\n  viable jobs        {viable:>6,}\n"
            f"  form published     {gh:>6,}   greenhouse + ashby\n"
            f"  forms on file      {have:>6,}\n"
            f"  asked, none there  {missed:>6,}\n"
            f"  left to fetch      {len(due(conn, 10_000)):>6,}\n")


def blockers_report(conn) -> str:
    out = []
    for r in conn.execute("""
            SELECT DISTINCT q.job_id, j.title, j.company FROM form_question q
            JOIN job j ON j.id = q.job_id
            JOIN fit f ON f.job_id = q.job_id WHERE f.viable = 1"""):
        found, _ = classify(cached(conn, r["job_id"]))
        if found:
            out.append(f"\n  {r['job_id']}  {r['title'][:52]}  — {r['company'][:22]}")
            out.extend(f"      ✗ {b}" for b in found)
    return "\n".join(out) if out else "\n  Nothing on file rules him out.\n"


def common_gaps(conn, limit: int = 25) -> str:
    """Unanswered questions, most-repeated first.

    The point of the list: each line is one answer he could write once into
    `resume/INTERVIEW-LINES.md` and never type again, and the count is how many
    applications it would fill. It is the cheapest work available.
    """
    import kit
    lines = kit.load_lines()
    tally: dict[str, list] = {}
    for r in conn.execute("""
            SELECT DISTINCT q.job_id FROM form_question q
            JOIN fit f ON f.job_id = q.job_id WHERE f.viable = 1"""):
        job = conn.execute("SELECT company FROM job WHERE id = ?",
                           (r["job_id"],)).fetchone()
        for field in cached(conn, r["job_id"]):
            label = field["label"]
            routed = any(pattern.search(label) for pattern, _ in kit.ROUTES)
            if routed or kit.match_line(label, lines):
                continue
            key = re.sub(r"\s+", " ", label.strip().lower())[:90]
            tally.setdefault(key, [label, 0, set()])
            tally[key][1] += 1
            tally[key][2].add(job["company"] if job else "?")

    ranked = sorted(tally.values(), key=lambda x: -x[1])[:limit]
    if not ranked:
        return "\n  Every question on file has an answer.\n"
    out = ["\n  Questions with no prepared answer, most-repeated first.",
           "  Write each one into resume/INTERVIEW-LINES.md and it fills itself"
           " in from then on.\n"]
    for label, count, companies in ranked:
        out.append(f"  {count:>3} x  {label[:88]}")
        out.append(f"         {', '.join(sorted(companies)[:4])}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_id", nargs="?", type=int)
    ap.add_argument("--fetch", type=int, metavar="N")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--blockers", action="store_true")
    ap.add_argument("--common", action="store_true",
                    help="unanswered questions, most-repeated first")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    with connect() as conn:
        if args.status:
            print(status(conn))
        elif args.blockers:
            print(blockers_report(conn))
        elif args.common:
            print(common_gaps(conn))
        elif args.fetch is not None:
            result = fetch_many(conn, args.fetch, cfg=settings())
            print(f"forms: {result['fetched']} fetched, "
                  f"{result['no_form']} had none")
        elif args.job_id:
            fetch_one(conn, args.job_id, force=args.force, cfg=settings())
            print(render(conn, args.job_id))
        else:
            ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
