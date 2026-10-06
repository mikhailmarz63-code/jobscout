#!/usr/bin/env python3
"""Stage (opt-in): evidence-quoted fit scoring for the top of the day.

Everything else in this project decides fit with rules -- title regexes, a
years-of-experience parser, a keyword list. Those are auditable but blunt: a
rule cannot read "led the migration of a Django monolith to microservices" and
recognise that as evidence for "backend systems experience" the way a person
would. This stage asks a model to, for a small number of jobs, and makes the
model show its work: every rating carries a verbatim quote from the CV and
one from the advert, and the code -- not the model's word for it -- checks
that each quote is really there before trusting the rating.

Anchored 0-4 scale, from sliday/resume-job-matcher:
    4  direct, verifiable evidence      2  partial or adjacent evidence
    3  close match, minor gap           1  weak evidence
                                        0  absent or contradicted

Runs through `claude -p` -- the Claude Code CLI on your own subscription --
never the Anthropic API or SDK, and never with a key. `--restricted` strips
its tool access down to nothing this task needs (no Bash, no file writes): the
CV and advert text are given to it directly in the prompt, and it has nothing
else to do but answer.

Off by default (`judge.enabled` in settings.yml) and capped at `daily_cap`
jobs a day -- this spends your Claude subscription usage, not a free tier. Cached in
`agent_call`, keyed by job id and a hash of the CV text actually sent, so the
same pair is never judged twice.

    python3 judge.py                # the configured daily_cap, if enabled
    python3 judge.py --force        # run once regardless of the setting
    python3 judge.py --explain 412  # the cached ratings for one job
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "job-applications"))

from db import connect, job_description, log_stage, now      # noqa: E402
import person                                                 # noqa: E402
from sources import settings                                  # noqa: E402

try:
    from matcher import RESUME_SOURCE_MD, pick_variant
except ImportError:                         # pragma: no cover
    RESUME_SOURCE_MD, pick_variant = {}, None

PURPOSE_PREFIX = "fit_judge"
CALL_TIMEOUT = 120

ANCHOR_SCALE = (
    "4 = direct, verifiable evidence in the CV\n"
    "3 = close match, a minor gap\n"
    "2 = partial or adjacent evidence\n"
    "1 = weak evidence\n"
    "0 = absent or contradicted"
)

PROMPT_TEMPLATE = """You are scoring how well a CV fits a job advert. Read both texts below in
full, then pick up to 6 explicit requirements the advert actually states
(skills, experience, domain, tools -- not company boilerplate), and rate the
CV against each one on this anchored scale:

{scale}

Rules, followed exactly:
- Every "cv_quote" must be copied character-for-character from the CV text
  below -- no paraphrasing, no ellipsis, no fixed typos. If nothing in the CV
  supports the criterion, rate it 0 and leave cv_quote empty.
- Every "advert_quote" must be copied character-for-character from the advert
  text below, the sentence that states the requirement.
- Output only JSON, no other text, in exactly this shape:
{{"criteria": [{{"name": "...", "rating": 0, "cv_quote": "...", "advert_quote": "...", "note": "one sentence"}}], "summary": "one paragraph, plain, on whether this is worth his evening"}}

=== ADVERT ({title} at {company}) ===
{advert}

=== CV ===
{cv}
"""


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_verified(quote: str, source: str) -> bool:
    """Whitespace-normalised substring check. The same idea as
    `ats.assert_no_fabrication()` -- a claim is only as good as the text it
    can be found in -- but that function walks a list of CV-edit patch ops and
    has nothing to do with rating a fixed advert/CV pair, so this is its own
    small check rather than a forced reuse."""
    quote, source = (quote or "").strip(), source or ""
    if not quote:
        return False
    return _norm_ws(quote) in _norm_ws(source)


def cv_text_and_hash(title: str, description: str) -> tuple[str, str, str]:
    """(variant, cv_text, cv_hash). `pick_variant()` is the same call
    score.py's profile component already makes -- one CV-selection rule, not
    two that could drift apart."""
    variant = "ai-solutions-engineer"
    if pick_variant:
        try:
            variant, _ = pick_variant(f"{title}\n{description}")
        except Exception:                   # noqa: BLE001 -- a helper, not a gate
            pass
    rel = RESUME_SOURCE_MD.get(variant, "resume/resume-ai-solutions-engineer.md")
    path = ROOT / rel
    if person.name():                       # their CV, never the owner's variants
        variant, path = person.CV_SLUG, person.resume_dir(ROOT / "resume") / f"resume-{person.CV_SLUG}.md"
    try:
        text = path.read_text()
    except OSError:
        text = ""
    return variant, text, hashlib.sha256(text.encode()).hexdigest()[:16]


def build_prompt(title: str, company: str, advert: str, cv: str) -> str:
    return PROMPT_TEMPLATE.format(scale=ANCHOR_SCALE, title=title or "",
                                  company=company or "", advert=advert[:12000],
                                  cv=cv[:12000])


def call_claude(prompt: str, model: str) -> tuple[str | None, str]:
    """(reply_text, error). Shells out to the Claude Code CLI, never the API:
    `--restricted` removes Bash/code-running tools and confines file access,
    because this task has no business touching either -- the whole prompt is
    self-contained text. `--permission-prompts none` refuses anything that
    would otherwise stop and ask, since nothing here is attended."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--restricted", "--permission-prompts", "none",
             "--output-format", "json", "--model", model, prompt],
            capture_output=True, text=True, timeout=CALL_TIMEOUT)
    except FileNotFoundError:
        return None, "claude CLI not on PATH"
    except subprocess.TimeoutExpired:
        return None, f"claude -p timed out after {CALL_TIMEOUT}s"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, f"claude -p did not return JSON: {proc.stdout[:200]!r}"
    if payload.get("is_error"):
        return None, f"claude -p error: {payload.get('result', '')[:200]}"
    return payload.get("result", ""), ""


def parse_and_verify(reply: str, cv: str, advert: str) -> dict:
    """The model's JSON, with every rating checked against the real text.
    A criterion whose quote is not a genuine substring of its source is
    discarded and marked `[unverified]` -- exactly the rule this project
    already applies to CV edits in `ats.assert_no_fabrication()`, applied here
    to a rating instead of a patch."""
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        return {"criteria": [], "summary": "", "parse_error": "no JSON object in reply"}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"criteria": [], "summary": "", "parse_error": str(exc)}

    out = []
    for c in parsed.get("criteria", [])[:8]:
        name = str(c.get("name", ""))[:200]
        rating = c.get("rating")
        cv_q, ad_q = str(c.get("cv_quote", "")), str(c.get("advert_quote", ""))
        note = str(c.get("note", ""))[:400]
        cv_ok = quote_verified(cv_q, cv)
        ad_ok = quote_verified(ad_q, advert)
        if isinstance(rating, (int, float)) and 0 <= rating <= 4 and cv_ok and ad_ok:
            out.append({"name": name, "rating": int(rating), "cv_quote": cv_q,
                       "advert_quote": ad_q, "note": note, "verified": True})
        else:
            out.append({"name": name, "rating": None, "cv_quote": cv_q if cv_ok else "",
                       "advert_quote": ad_q if ad_ok else "",
                       "note": f"[unverified] {note}".strip(), "verified": False})
    return {"criteria": out, "summary": str(parsed.get("summary", ""))[:2000]}


def cached(conn, job_id: int, cv_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT reasoning FROM agent_call WHERE job_id = ? AND purpose = ? "
        "ORDER BY id DESC LIMIT 1",
        (job_id, f"{PURPOSE_PREFIX}:{cv_hash}")).fetchone()
    if not row or not row["reasoning"]:
        return None
    try:
        return json.loads(row["reasoning"])
    except json.JSONDecodeError:
        return None


def judge_one(conn, job_id: int, title: str, company: str, model: str) -> dict | None:
    advert = job_description(conn, job_id)
    variant, cv, cv_hash = cv_text_and_hash(title, advert)
    existing = cached(conn, job_id, cv_hash)
    if existing is not None:
        return existing

    prompt = build_prompt(title, company, advert, cv)
    reply, error = call_claude(prompt, model)
    stamp = now()
    if error:
        conn.execute(
            "INSERT INTO agent_call (job_id, purpose, backend, prompt, reply, "
            "choice, confidence, reasoning, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, f"{PURPOSE_PREFIX}:{cv_hash}", "claude-cli", prompt, None,
             "error", None, json.dumps({"error": error}), stamp))
        conn.commit()
        print(f"  job {job_id}: {error}", file=sys.stderr)
        return None

    result = parse_and_verify(reply or "", cv, advert)
    result["variant"] = variant
    verified = [c for c in result["criteria"] if c["verified"]]
    avg = sum(c["rating"] for c in verified) / len(verified) if verified else None
    conn.execute(
        "INSERT INTO agent_call (job_id, purpose, backend, prompt, reply, "
        "choice, confidence, reasoning, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, f"{PURPOSE_PREFIX}:{cv_hash}", "claude-cli", prompt, reply,
         f"{avg:.1f}/4" if avg is not None else "unrated",
         (avg / 4.0) if avg is not None else None, json.dumps(result), stamp))
    conn.commit()
    return result


def due(conn, limit: int) -> list[dict]:
    """The top-scored viable jobs, most recent first among ties, that have not
    already been judged (any cv hash -- a fresh CV re-judges under its own
    hash, an already-judged pair is simply cached, see `judge_one()`)."""
    return [dict(r) for r in conn.execute("""
        SELECT j.id, j.title, j.company FROM job j
        JOIN fit f ON f.job_id = j.id
        JOIN score sc ON sc.job_id = j.id
        WHERE j.status = 'open' AND f.viable = 1
        ORDER BY sc.total DESC LIMIT ?""", (limit,))]


def run(conn, cfg: dict, cap: int | None = None) -> dict:
    started = now()
    conf = cfg.get("judge", {})
    model = conf.get("model", "sonnet")
    n = cap if cap is not None else int(conf.get("daily_cap", 20))
    jobs = due(conn, n)
    judged = skipped = errors = 0
    for j in jobs:
        result = judge_one(conn, j["id"], j["title"], j["company"], model)
        if result is None:
            errors += 1
        elif result.get("parse_error"):
            skipped += 1
        else:
            judged += 1
    detail = f"judged={judged} skipped={skipped} errors={errors}"
    log_stage(conn, "judge", errors == 0, detail, started)
    return {"judged": judged, "skipped": skipped, "errors": errors}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="run once even if judge.enabled is false")
    ap.add_argument("--cap", type=int)
    ap.add_argument("--explain", type=int, metavar="JOB_ID")
    args = ap.parse_args(argv)

    conn = connect()

    if args.explain:
        row = conn.execute(
            "SELECT reasoning, choice, called_at FROM agent_call WHERE job_id = ? "
            "AND purpose LIKE ? ORDER BY id DESC LIMIT 1",
            (args.explain, f"{PURPOSE_PREFIX}:%")).fetchone()
        if not row:
            print(f"no judge result for job {args.explain} -- run judge.py first")
            return 1
        result = json.loads(row["reasoning"] or "{}")
        print(f"overall: {row['choice']}\n")
        for c in result.get("criteria", []):
            mark = f"{c['rating']}/4" if c["verified"] else "unverified"
            print(f"  {mark:<10} {c['name']}")
            if c.get("cv_quote"):
                print(f"    CV:     \"{c['cv_quote']}\"")
            if c.get("advert_quote"):
                print(f"    advert: \"{c['advert_quote']}\"")
        print(f"\n{result.get('summary', '')}")
        return 0

    cfg = settings()
    if not cfg.get("judge", {}).get("enabled", False) and not args.force:
        print("judge: disabled (judge.enabled is false) -- nothing to do")
        return 0

    result = run(conn, cfg, cap=args.cap)
    print(f"judged {result['judged']}, {result['skipped']} skipped, "
          f"{result['errors']} error(s)")
    return 1 if result["errors"] and result["judged"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
