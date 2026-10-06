#!/usr/bin/env python3
"""The copy-paste pack: everything an application asks for, per job.

Applying is mostly re-typing. The cover letter, the "why should we hire you",
the same four screening questions, the same STAR stories in a different order.
That work is already done -- it is sitting in `resume/` as
`STAR-STORIES.md`, `INTERVIEW-LINES.md`, `INTERVIEW-DEFENCE.md` and
`cover-letter-template.md`, written properly and honestly. What was missing was
anything to *aim* it at a specific job.

So this file **reads those files** rather than restating them. STAR-STORIES.md
says so itself: "The `(use for: ...)` tag on each story is what a generator keys
off to decide which stories fit a given JD." This is that generator. Nothing
here invents a claim about him; if a sentence is not in `resume/`, it is not in
the pack.

Three kinds of content come out:

  * **written from the templates** -- the cover letter, the pitch, the stories
    that match this job's own words;
  * **computed from this job's data** -- the timezone answer works out the
    actual clock overlap in both cities, and the salary answer starts from the
    range the advert itself published;
  * **quoted defences** -- the awkward questions his CV invites, in his own
    prepared words, chosen by what this advert is likely to probe.

Nothing sends. This is a pack he copies from.

    python3 kit.py 753                 # the pack, on screen
    python3 kit.py 753 --write         # also save it under kits/
    python3 kit.py --top 5 --write     # packs for the current top five
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from db import connect                      # noqa: E402
from sources import settings                # noqa: E402

RESUME = ROOT / "resume"
STORIES_MD = RESUME / "STAR-STORIES.md"
LINES_MD = RESUME / "INTERVIEW-LINES.md"
TEMPLATE_MD = RESUME / "cover-letter-template.md"
OUT_DIR = HERE / "kits"

# What a JD word implies about which story to tell. The left side is matched
# against the advert; the right side against each story's own `(use for: ...)`
# tag, so the mapping stays honest to what STAR-STORIES.md already claims.
JD_TO_TAG = {
    "attention to detail": ["detail", "accuracy", "quality", "validation", "review"],
    "initiative": ["ownership", "self-starter", "autonomous", "proactive", "initiative"],
    "biggest achievement": ["achievement", "impact", "proud"],
    "requirements gathering": ["requirement", "specification", "spec", "elicit",
                               "user stor", "acceptance criteria", "business analy"],
    "working with non-technical people": ["stakeholder", "non-technical", "business users",
                                          "cross-functional", "liaison", "translate"],
    "delivering under a deadline": ["deadline", "fast-paced", "ship", "delivery", "urgency"],
    "uat": ["uat", "user acceptance", "testing", "qa", "validation"],
    "a failure": ["failure", "mistake", "learn", "retrospective", "post-mortem"],
    "iteration": ["iterate", "iterative", "agile", "sprint", "continuous improvement"],
    "what you'd do differently": ["reflect", "hindsight", "lesson"],
    "stakeholder management": ["stakeholder", "manage up", "influence", "negotiat"],
    "ambiguity": ["ambiguity", "ambiguous", "undefined", "greenfield", "zero to one",
                  "figure it out", "autonomy"],
    "conflict": ["conflict", "disagree", "pushback"],
    "working with developers": ["developer", "engineering team", "engineers", "technical team"],
}


# ----------------------------------------------------------------- sources --

def load_stories() -> list[dict]:
    """Parse STAR-STORIES.md. One dict per story, with its `use for` tags."""
    if not STORIES_MD.exists():
        return []
    text = STORIES_MD.read_text()
    stories = []
    for block in re.split(r"\n###\s+", text)[1:]:
        head, _, body = block.partition("\n")
        title = re.sub(r"\*\(use for:.*?\)\*", "", head).strip(" *")
        tags = []
        tag_match = re.search(r"\(use for:\s*([^)]+)\)", head, re.I)
        if tag_match:
            tags = [t.strip().lower() for t in tag_match.group(1).split(",")]

        def field(letter: str) -> str:
            m = re.search(rf"^\s*-\s*\*\*{letter}:\*\*\s*(.+?)(?=\n\s*-\s*\*\*|\Z)",
                          body, re.S | re.M)
            return " ".join(m.group(1).split()) if m else ""

        point = re.search(r"\*\*The point to land:\*\*\s*(.+?)(?=\n\s*-\s*\*\*|\Z)",
                          body, re.S)
        stories.append({
            "title": title, "tags": tags,
            "s": field("S"), "t": field("T"), "a": field("A"), "r": field("R"),
            "point": " ".join(point.group(1).split()) if point else "",
        })
    return [s for s in stories if s["s"]]


def _story_pool(stories: list[dict]) -> dict[str, str]:
    """Every STAR story as a match_line() source, keyed by its title and use-for
    tags. INTERVIEW-LINES.md is a narrow FAQ for hard interview questions; it
    was never written to cover an application form's open-ended "describe a
    project you're proud of" or "tell us about a challenge" essay boxes. The
    stories already answer exactly that, in his own words -- this just lets a
    form question find them, on top of the JD-targeted subset picked_stories()
    already surfaces for the cover letter."""
    pool = {}
    for st in stories:
        heading = " ".join([st["title"], *st.get("tags", [])])
        body = (f"Situation: {st['s']}\n\nTask: {st['t']}\n\nAction: {st['a']}\n\nResult: {st['r']}"
                + (f"\n\n{st['point']}" if st.get("point") else ""))
        if heading.strip():
            pool[heading] = body
    return pool


def _project_pool() -> dict[str, str]:
    """Selected Projects from the canonical CV, for the same reason: a form
    that asks to describe a personal project has a real answer sitting in
    resume/, and nothing was reading that section before this."""
    try:
        import ats
        cv = ats.load_cv(ats.variant_path(ats.CANONICAL))
    except Exception:                                # noqa: BLE001 -- a source, not a gate
        return {}
    section = cv.section("projects")
    if not section:
        return {}
    pool = {}
    for e in section.entries:
        heading = f"{e.title} {e.note or ''}".strip()
        body = " ".join(e.body or e.bullets)
        if heading and body:
            pool[heading] = body
    return pool


def load_lines() -> dict[str, str]:
    """Parse INTERVIEW-LINES.md into {question: his prepared answer}.

    The file is written as bolded prompts followed by blockquoted answers, so
    the parse is a walk rather than a regex over the whole thing.
    """
    if not LINES_MD.exists():
        return {}
    lines, current, buffer = {}, None, []

    def flush():
        if current and buffer:
            lines[current] = "\n\n".join(
                " ".join(part.split()) for part in "\n".join(buffer).split("\n\n")
                if part.strip())

    for raw in LINES_MD.read_text().splitlines():
        heading = re.match(r"^\*\*(.+?)\*\*\s*$", raw.strip())
        if heading:
            flush()
            current, buffer = heading.group(1).strip().strip(":"), []
            continue
        if raw.lstrip().startswith(">"):
            buffer.append(raw.lstrip()[1:].strip())
        elif raw.strip().startswith(("#", "---")) and buffer:
            flush()
            current, buffer = None, []
    flush()
    return {k: v for k, v in lines.items() if v}


# --------------------------------------------------------------- matching --

def pick_stories(job_text: str, stories: list[dict], limit: int = 3) -> list[dict]:
    """The stories this advert actually invites, best first.

    Scored by how many of the advert's own words map to a story's `use for`
    tags. Where nothing matches, the two strongest general-purpose stories are
    returned rather than none -- an empty pack helps nobody, and Story A and B
    are the ones that carry any interview.
    """
    text = (job_text or "").lower()
    scored = []
    for story in stories:
        score = 0
        for tag in story["tags"]:
            for phrase, keywords in JD_TO_TAG.items():
                if phrase not in tag:
                    continue
                score += sum(2 for k in keywords if k in text)
        scored.append((score, story))
    scored.sort(key=lambda pair: -pair[0])
    picked = [s for score, s in scored if score > 0][:limit]
    return picked or [s for _, s in scored[:2]]


# --------------------------------------------------- computed, per-job bits --

def clock_answer(conn, job: dict) -> str:
    """The timezone question, answered with real numbers rather than "flexible".

    Being able to state the actual window in both clocks is the single most
    reassuring thing a candidate twelve time zones away can do, and it is
    already in the database.
    """
    cfg = settings()
    mine = float(cfg["candidate"]["utc_offset"])
    start = float(cfg["hours"]["workday_start"])
    end = float(cfg["hours"]["workday_end"])

    theirs = job.get("utc_offset")
    overlap = job.get("overlap_hours")
    city = job.get("country_name") or "your team"

    if theirs is None:
        return (f"I'm in Colombo, Sri Lanka — UTC+{mine:g}. Happy to shift my "
                f"day to cover your core hours; tell me the window you need and "
                f"I'll confirm what I can hold consistently.")

    def clock(hour_utc: float) -> str:
        hour_utc %= 24
        return f"{int(hour_utc):02d}:{int(round((hour_utc % 1) * 60)):02d}"

    their_start_utc = start - theirs
    their_end_utc = end - theirs
    mine_for_their_start = clock(their_start_utc + mine)
    mine_for_their_end = clock(their_end_utc + mine)

    if overlap and overlap >= 4:
        return (f"I'm in Colombo (UTC+{mine:g}). Your 09:00–18:00 is "
                f"{mine_for_their_start}–{mine_for_their_end} my time, so we "
                f"share about {overlap:g} hours of normal working day without "
                f"either of us moving. I'd plan to be online for all of it.")
    if overlap and overlap > 0:
        return (f"I'm in Colombo (UTC+{mine:g}). Your 09:00–18:00 is "
                f"{mine_for_their_start}–{mine_for_their_end} my time — about "
                f"{overlap:g} hours of natural overlap. I'd start late and work "
                f"into your morning; that's a shift I'm willing to hold, and I'd "
                f"rather say so now than discover it in month two.")
    return (f"Straight answer: I'm in Colombo (UTC+{mine:g}) and your 09:00–18:00 "
            f"is {mine_for_their_start}–{mine_for_their_end} here — there's no "
            f"natural overlap with a normal day at either end. I can commit to a "
            f"fixed block of your hours if the role needs live collaboration; if "
            f"it's mostly asynchronous, that suits me better and I'd want to know "
            f"which it is.")


def current_ctc_answer() -> str:
    """What he currently earns has never been written down anywhere in
    resume/, and this file does not get to decide a number for him -- unlike
    salary_answer(), which is about what he wants for the *new* role, this is
    asking what he makes in the *current* one. The one true thing on file is
    that the current role is an internship, so that is what gets said; the redirect to expected compensation is
    standard practice, not evasion."""
    return ("My current role is internship-level, so it doesn't map "
            "cleanly onto a standard CTC figure. I'd rather talk about what "
            "this role pays than what that one does — happy to share a number "
            "if it's useful context.")


def salary_answer(job: dict) -> str:
    """Anchored on the advert's own number where it published one."""
    floor = settings()["pay"]["floor_usd_month"]
    lo, hi = job.get("lo"), job.get("hi")
    if job.get("pay_known") and (lo or hi):
        low = lo or hi
        high = hi or lo
        return (f"Your posting lists ${low*12:,.0f}–${high*12:,.0f} a year. "
                f"That range works for me and I'd expect to sit in the lower "
                f"half of it given my experience — I'd rather be priced "
                f"honestly and grow into it than negotiate hard on day one.")
    return (f"I'm looking for at least ${floor:,.0f} a month. I'd rather hear "
            f"your band first — I don't know what this role is worth in your "
            f"market and I'd only be guessing.")


def sponsorship_answer(job: dict) -> str:
    state = job.get("state")
    if state == "ONSITE_SPONSORED":
        return ("Yes — I'd need sponsorship. Your posting says you offer it, "
                "which is why I applied. I'm a Sri Lankan citizen, currently in "
                "Sri Lanka, with no existing right to work in your country.")
    if state == "ONSITE_LK":
        return ("No sponsorship needed — I'm a Sri Lankan citizen living in "
                "Colombo, so I can start on-site without any paperwork.")
    return ("For a remote role, none — I'd be working from Sri Lanka as a "
            "contractor or through whatever employer-of-record you use. I have "
            "no right to work in the US, UK or EU, so if this role turns out to "
            "need one, better we establish that now.")


# ------------------------------------------------------------------ render --

# `companies.py` takes the first usable sentence off the advert, and sometimes
# that sentence is addressed to the *candidate* rather than about the company.
# Omnea's came out as "You've spent 2 to 3 years in a chief-of-staff role...",
# which the template then quoted back as what drew him to them. A blank is far
# better than that, and the docstring below already says why.
NOT_ABOUT_THEM = re.compile(
    r"^(?:you|we're looking|we are looking|the ideal|candidates?|applicants?|"
    r"this role|in this role|responsibilities)\b|"
    r"years (?:of experience|in a)|you'(?:ve|ll|re)\b|"
    r"\bideally\b.{0,30}\b(?:you|experience)\b", re.I)


def why_this_company(conn, job: dict) -> tuple[str, str]:
    """(sentence, source_url). Empty when nothing is evidenced.

    The bracket stays a bracket when there is no summary on file. That is the
    point: a generated specific that turns out to be wrong is worse than a
    blank he fills in ten seconds, and the one person guaranteed to read this
    line is somebody who works there.
    """
    import companies
    found = companies.lookup(conn, job.get("company_slug") or "")
    if not found or not found.get("summary"):
        return "", ""
    first = re.split(r"(?<=[.!?])\s+", found["summary"])[0].strip()
    if len(first) < 40 or NOT_ABOUT_THEM.search(first):
        return "", ""
    return (f"What drew me to {job.get('company', 'you')} specifically: "
            f"\u201c{first}\u201d \u2014 that is the kind of product I want to "
            f"be close to, and the work I have done is the unglamorous half of "
            f"making one usable."), found.get("source_url", "")


def cover_letter(job: dict, stories: list[dict], hours_line: str = "",
                 why_line: str = "") -> str:
    """The template, filled. Anything that still needs his judgment is left in
    [BRACKETS] rather than invented -- the template's own convention, and the
    right one: a generated specific that turns out to be wrong is worse than a
    blank he fills in ten seconds."""
    cfg = settings()["candidate"]
    company = job.get("company") or "[COMPANY]"
    title = job.get("title") or "[ROLE]"
    today = datetime.now().strftime("%d %B %Y")

    # The strongest matching story, written as a paragraph rather than dropped
    # in as the raw A and R fields -- "Sat with them, watched the actual
    # workflow" is a note to himself, not a sentence in a letter.
    lead = stories[0] if stories else None
    evidence = ""
    if lead:
        situation = lead["s"].rstrip(".")
        evidence = (f"\n\nThe clearest example: {situation[0].lower()}{situation[1:]}. "
                    f"{lead['a']} {lead['r']}")

    # The hours sentence is the one thing a Colombo applicant must answer
    # before being asked, so it goes in the letter rather than waiting for the
    # screening call.
    why = why_line or (
        "[ONE SENTENCE ON WHY THIS COMPANY — name something specific from "
        "their posting or product. Do not skip this line; it is the only part "
        "of the letter that could not have been sent to anyone else.]")

    hours = hours_line or (
        "I'm based in Colombo, Sri Lanka, and working remotely suits how I "
        "already operate.")

    return f"""{today}

Hiring Team
{company}

Dear Hiring Team at {company},

I'm applying for the {title} role. I'm a Management Information Systems
graduate and Anthropic-certified AI practitioner, and I've spent the last
eighteen months delivering systems that people use daily rather than
prototypes — requirements gathered first-hand, built, taken through UAT, and
put into production.{evidence}

{why}

{hours}

I'd welcome the chance to talk it through.

Kind regards,
{cfg['name']}
{cfg['email']} · {cfg['phone']}
{cfg['linkedin']}
"""


# ------------------------------------------------- the advert's own form --

# `forms.py` fetches what the application page actually asks. Each real
# question is routed to an answer this file already knows how to make. The
# rule from the top of this module still holds: a question with no source in
# `resume/` gets a marker, never a sentence someone made up.
#
# Order matters -- the first pattern that matches wins, so the specific ones
# ("cover letter") sit above the general ones ("letter").
ROUTES = [
    (re.compile(r"^preferred first name", re.I), "preferred_name"),
    (re.compile(r"first name", re.I), "first_name"),
    (re.compile(r"last name|surname|family name", re.I), "last_name"),
    (re.compile(r"^e-?mail|email address", re.I), "email"),
    (re.compile(r"phone|mobile|contact number", re.I), "phone"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"resume|cv\b", re.I), "resume"),
    (re.compile(r"cover letter", re.I), "cover_letter"),
    (re.compile(r"authoriz\w+ to work|right to work|legally (?:able|entitled)",
                re.I), "work_auth"),
    (re.compile(r"sponsor|\bvisa\b|work permit", re.I), "sponsorship"),
    (re.compile(r"current\s+(?:annual\s+)?ctc|cost to company|present salary|"
                r"current (?:salary|pay|compensation)|last drawn salary", re.I),
     "current_ctc"),
    (re.compile(r"salary|compensation|pay expectation|expected (?:pay|rate)",
                re.I), "salary"),
    (re.compile(r"hours|time ?zone|shift|on[- ]call|availability|overlap",
                re.I), "hours"),
    (re.compile(r"why (?:are you |do you |you )?.{0,24}(?:excited|interested|"
                r"want to|join|apply|us\b)", re.I), "why_company"),
    # "Why Anthropic?" -- a bare "why <company>" with no verb at all. Thirty of
    # the fifty-six forms on file ask it in exactly this shape.
    (re.compile(r"^\s*why\s+[\w.&' -]{2,40}\??\s*$", re.I), "why_company"),
    # Ashby asks "What excites you about this role...", which is the same
    # question and starts with the wrong word for the patterns above.
    (re.compile(r"what excites you|what interests you|why do you want|"
                r"why this role", re.I), "why_company"),
    (re.compile(r"^\s*(?:full )?name\s*$", re.I), "full_name"),
    (re.compile(r"start date|when (?:can|could) you start|notice period", re.I),
     "start_date"),
    # "Describe your best personal project" -- a common form shape that
    # INTERVIEW-LINES.md was never written to answer (that file is hard
    # interview questions, not project write-ups); the CV's own Selected
    # Projects section already has the answer.
    (re.compile(r"describe.{0,40}\bproject\b|\btell (?:us|me) about a project\b|"
                r"\b(?:best|favou?rite|proudest)\b.{0,15}\bproject\b", re.I), "best_project"),
    (re.compile(r"website|portfolio|github|personal site", re.I), "links"),
    # "Country", "Country of residence", "Where are you based?" -- the one
    # answer that never changes.
    (re.compile(r"^\s*country\b|country of residence|where are you (?:located|based)|"
                r"current location|^\s*location\s*$", re.I), "country"),
    # Self-identification: gender, race/ethnicity, veteran and disability
    # status. These are his to answer, never the pack's -- see GUARD_KINDS
    # below, which is the part that actually stops them being auto-filled.
    (re.compile(r"\bgender\b|\bsex\b(?!ual)|\brace\b|ethnicit\w+|\bveteran\b|"
                r"disabilit\w+|hispanic or latino|self[- ]identif", re.I), "self_id"),
]

NEEDS_YOU = "[NEEDS YOU]"

# Kinds a form asks about that are his to answer, never the system's to fill
# in for him -- even when `answer_for()` below can honestly compute a correct
# text for the copy-paste pack. Added with the "repeated questions" panel
# after noticing `answers_for_labels()` was handing the browser extension a
# `choice` for a sponsorship/salary dropdown whenever the computed text
# happened to match one of the field's own options, and `fillSafe()` on the
# extension side would one-click it along with the identity fields it was
# meant to be limited to. Self-identification never had an answer at all
# (nothing routed it before this), but it gets the same explicit guard rather
# than relying on silence.
GUARD_KINDS = {"self_id", "work_auth", "sponsorship", "salary", "current_ctc"}


def _is_file(field: dict) -> bool:
    """Greenhouse calls it input_file; Ashby calls it File."""
    return field.get("field_type", "").lower() in ("input_file", "file")


def _honest_choice(values: list[str], want: str) -> str:
    """Pick the option that matches the true answer, by its first word."""
    for value in values:
        if value.strip().lower().startswith(want.lower()):
            return value
    return ""


# A sponsorship dropdown is not always Yes/No. Omnea offers three full
# sentences and the true one is "I would need Omnea to provide a visa". Picking
# by first word would have taken "No, I already have the right to work", which
# is a lie on a form that can be checked.
NEEDS_VISA = re.compile(r"would need|require .{0,30}(?:visa|sponsor|support)|"
                        r"\bsponsorship\b.{0,20}(?:needed|required)", re.I)
ALREADY_AUTHORISED = re.compile(r"already have|do not require|don't require|"
                                r"no,? i (?:am|have)", re.I)


def _sponsorship_choice(values: list[str]) -> str:
    """The option that says he needs sponsorship, whatever words it uses."""
    for value in values:
        if NEEDS_VISA.search(value) and not ALREADY_AUTHORISED.search(value):
            return value
    return _honest_choice(values, "yes")


def route(label: str) -> str | None:
    """Which kind of answer a form label asks for, or None if nothing routes."""
    return next((name for pattern, name in ROUTES if pattern.search(label or "")), None)


def answer_for(conn, job: dict, field: dict, ctx: dict) -> str:
    """One field of a real form. Empty string means 'he has to write this'."""
    label, values = field["label"], field.get("values") or []
    who = settings().get("candidate", {})
    kind = route(label)

    if kind == "full_name":
        return who.get("name", "")
    if kind == "first_name":
        return (who.get("name", "").split() or [""])[0]
    if kind == "preferred_name":
        return (who.get("name", "").split() or [""])[0]
    if kind == "last_name":
        return (who.get("name", "").split() or [""])[-1]
    if kind == "email":
        return who.get("email", "")
    if kind == "phone":
        return who.get("phone", "")
    if kind == "linkedin":
        return who.get("linkedin", "")
    if kind == "links":
        return who.get("linkedin", "")
    if kind == "country":
        name = who.get("country_name") or {"LK": "Sri Lanka"}.get(str(who.get("country", "")).upper(), "")
        return (_honest_choice(values, name) if values else name) or name
    if kind == "resume":
        if _is_file(field):
            # Default CV changed 2026-09-03: AI Solutions Engineer, not the
            # generic tech variant. Same evidence, higher band. The PDF is
            # rendered from the markdown by builders/build_resume_from_md.py;
            # until it has been built once, the markdown is what there is.
            import ats
            pdf = ats.default_pdf(ats.CANONICAL)
            return f"upload: {pdf or RESUME / 'resume-ai-solutions-engineer.md'}"
        return NEEDS_YOU + " paste the CV text"
    if kind == "cover_letter":
        if _is_file(field):
            return "upload: the letter below, as a PDF"
        return ctx["cover_letter"]
    if kind == "work_auth":
        # The only country he can answer yes for is Sri Lanka.
        if re.search(r"sri lanka", label, re.I):
            return _honest_choice(values, "yes") or "Yes"
        return _honest_choice(values, "no") or "No — I am a Sri Lankan citizen in Colombo."
    if kind == "sponsorship":
        return _sponsorship_choice(values) or sponsorship_answer(job)
    if kind == "salary":
        return salary_answer(job)
    if kind == "current_ctc":
        return current_ctc_answer()
    if kind == "self_id":
        # Empty, not a NEEDS_YOU marker -- the same shape a demographic
        # question with no route at all already returned (see
        # TestKit.test_it_never_answers_a_demographic_or_opinion_question in
        # test_jobscout.py). GUARD_KINDS is what actually enforces "never
        # auto-filled" downstream, in answers_for_labels(); this branch only
        # stops the label falling through to the interview-lines/story/project
        # fallback matcher, which has no business ever answering it.
        return ""
    if kind == "hours":
        # A yes/no about a specific shift is his call, but the clock maths is
        # the thing he would otherwise do by hand at the time.
        if values:
            return f"{NEEDS_YOU} — decide, then pick. {ctx['hours']}"
        return ctx["hours"]
    if kind == "why_company":
        return ctx["why_company"] or f"{NEEDS_YOU} — no company summary on file"
    if kind == "best_project":
        projects = ctx.get("projects") or {}
        if not projects:
            return ""
        # The richest write-up, not the first one -- more to work with beats a
        # one-line project when nothing in the question narrows the choice.
        heading, body = max(projects.items(), key=lambda kv: len(kv[1]))
        return f"{heading} — {body}"
    if kind == "start_date":
        return ctx["start_date"]

    # Nothing routed. Before giving up, look for a prepared answer in
    # INTERVIEW-LINES.md whose heading covers the same ground. This is the
    # lever that makes the pack fill itself over time: every answer he writes
    # once into that file answers the same question on every form after it,
    # and the file is the one place the wording is already his.
    line = match_line(label, ctx["lines"])
    if line:
        return line
    # An open-ended "describe a project / a challenge / your best work"
    # question was never written as an interview line -- his STAR stories and
    # the CV's own Selected Projects answer exactly that shape of question.
    # Still never invented: every word here already exists in resume/.
    story = match_line(label, ctx.get("story_pool") or {})
    if story:
        return story
    return match_line(label, ctx.get("projects") or {})


# Words that appear in every second question and carry no signal about which
# prepared answer fits.
_STOP = {"the", "a", "an", "you", "your", "do", "did", "does", "have", "has",
         "in", "of", "to", "and", "or", "for", "with", "what", "which", "how",
         "why", "are", "is", "be", "been", "any", "please", "us", "we", "our",
         "this", "that", "at", "on", "it", "will", "would", "can", "many"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower())
            if len(w) > 2 and w not in _STOP}


def match_line(label: str, lines: dict[str, str], floor: float = 0.34) -> str:
    """The prepared answer whose heading overlaps this question most.

    A floor, because a weak match is worse than a blank: he can write two
    sentences faster than he can notice that the pack answered a question with
    something almost but not quite relevant.
    """
    asked = _words(label)
    if not asked:
        return ""
    best, best_score = "", 0.0
    for heading, answer in lines.items():
        known = _words(heading)
        if not known:
            continue
        score = len(asked & known) / len(asked | known)
        if score > best_score:
            best, best_score = answer.strip(), score
    return best if best_score >= floor else ""


def sections_from_form(conn, job: dict, fields: list[dict], ctx: dict) -> list[tuple]:
    """(heading, body) per real field, in the form's own order."""
    out = []
    for i, field in enumerate(fields, 1):
        mark = " *required*" if field["required"] else ""
        body = answer_for(conn, job, field, ctx)
        if not body:
            body = f"{NEEDS_YOU} — nothing in resume/ answers this."
        if field.get("values"):
            body += "\n\nOptions on the form: " + " · ".join(field["values"][:8])
        out.append((f"{i}. {field['label']}{mark}", body))
    return out


def load_job(conn, job_id: int) -> dict:
    row = conn.execute("""
        SELECT j.id, j.company, j.company_slug, j.title, jt.description,
               j.canonical_url,
               e.state, s.known pay_known, s.min_usd_month lo, s.max_usd_month hi,
               g.country_name, g.utc_offset, g.overlap_hours, g.band,
               f.reach, f.reach_why, f.work_mode, sc.total score
        FROM job j
        LEFT JOIN job_text jt ON jt.job_id = j.id
        LEFT JOIN eligibility e ON e.job_id = j.id
        LEFT JOIN salary s ON s.job_id = j.id
        LEFT JOIN geo g ON g.job_id = j.id
        LEFT JOIN fit f ON f.job_id = j.id
        LEFT JOIN score sc ON sc.job_id = j.id
        WHERE j.id = ?""", (job_id,)).fetchone()
    return dict(row) if row else {}


def context(conn, job: dict) -> dict:
    """Everything an answer can be made from for one job: the letter, the
    clock maths, the why-this-company line, the start date and his prepared
    lines. `build()` and the browser helper share it, so the two never answer
    the same question differently."""
    stories = load_stories()
    lines = load_lines()
    picked = pick_stories(f"{job['title']} {job.get('description') or ''}", stories)
    why_line, why_source = why_this_company(conn, job)
    return {
        "cover_letter": cover_letter(job, picked, clock_answer(conn, job), why_line),
        "hours": clock_answer(conn, job),
        "why_company": why_line,
        "why_source": why_source,
        "start_date": start_date_line(),
        "lines": lines,
        "stories": picked,
        "story_pool": _story_pool(stories),
        "projects": _project_pool(),
    }


def context_generic() -> dict:
    """The same materials context() builds, for a job that is not in the
    database at all. No company summary to quote (nothing to look up), no
    salary range to anchor to, no timezone to compare -- but identity, CTC,
    the prepared lines and the story/project pools all still apply, and
    clock_answer() already degrades to an honest "tell me the window you
    need" with no job data at all. This exists because the browser helper
    used to fall back to a much narrower local-only matcher for exactly this
    case, which left every field blank, including his own name."""
    stories = load_stories()
    return {
        "cover_letter": "", "hours": clock_answer(None, {}), "why_company": "",
        "why_source": "", "start_date": start_date_line(), "lines": load_lines(),
        "stories": [], "story_pool": _story_pool(stories), "projects": _project_pool(),
    }


def answers_for_labels_generic(fields: list[dict]) -> dict:
    """answers_for_labels() without a resolved job -- same routes, same
    answers, same refusal to invent. Whatever needs a specific job (why this
    company, the pay band, sponsorship state) comes back honest rather than
    company-specific, but identity, CTC, and story/project matching all still
    work. Never returns {} -- there is always something to say about who he
    is, even about a job the board has never seen."""
    import forms
    job = {"id": None, "title": "", "company": "", "state": None}
    ctx = context_generic()
    norm = []
    for f in fields[:100]:
        label = str(f.get("label") or "")[:300]
        ftype = str(f.get("type") or f.get("field_type") or "input_text")
        norm.append({"label": label, "required": bool(f.get("required")),
                     "field_type": "input_file" if ftype == "file" else ftype,
                     "values": [str(v)[:120] for v in (f.get("options") or f.get("values") or [])][:40]})
    blockers, flags = forms.classify(norm)
    answers = []
    for f in norm:
        text = answer_for(None, job, f, ctx) or ""
        kind = route(f["label"])
        choice = text if f["values"] and text in f["values"] else None
        item = {"label": f["label"], "kind": kind, "answer": text, "choice": choice,
                "needs_you": (not text) or text.startswith(NEEDS_YOU)}
        if kind in GUARD_KINDS:
            item["choice"], item["needs_you"] = None, True
        if kind == "resume":
            import ats
            pdf = ats.default_pdf(ats.CANONICAL)
            item["file"] = str(pdf) if pdf else None
            item["pdf_url"] = f"/pdf/{ats.CANONICAL}" if pdf else None
        answers.append(item)
    return {"job_id": None, "company": "", "title": "", "applied": None,
            "blockers": blockers, "flags": flags, "answers": answers}


def start_date_line() -> str:
    """From the canonical CV's Availability section, so the notice period has
    one home. Three files used to disagree about it."""
    md = RESUME / "resume-ai-solutions-engineer.md"
    try:
        m = re.search(r"^## Availability\s*\n+(.+?)\s*(?:\n##|\Z)", md.read_text(), re.S | re.M)
    except OSError:
        m = None
    notice = " ".join(m.group(1).split()).rstrip(".") if m else "One week's notice"
    return f"{notice}, so I could start inside a week of an offer."


def answers_for_labels(conn, job_id: int, fields: list[dict]) -> dict:
    """The browser helper's question: here are the labels on the page in
    front of him -- what would the pack say for each? Same routes, same
    answers, same refusal to invent; plus the blockers computed on the *live*
    form, so a work-authorisation question is caught even where forms.py
    never fetched this board."""
    import forms
    job = load_job(conn, job_id)
    if not job:
        return {}
    ctx = context(conn, job)
    norm = []
    for f in fields[:100]:
        label = str(f.get("label") or "")[:300]
        ftype = str(f.get("type") or f.get("field_type") or "input_text")
        norm.append({"label": label, "required": bool(f.get("required")),
                     "field_type": "input_file" if ftype == "file" else ftype,
                     "values": [str(v)[:120] for v in (f.get("options") or f.get("values") or [])][:40]})
    blockers, flags = forms.classify(norm)
    answers = []
    for f in norm:
        text = answer_for(conn, job, f, ctx) or ""
        kind = route(f["label"])
        choice = text if f["values"] and text in f["values"] else None
        item = {"label": f["label"], "kind": kind, "answer": text, "choice": choice,
                "needs_you": (not text) or text.startswith(NEEDS_YOU)}
        if kind in GUARD_KINDS:
            item["choice"], item["needs_you"] = None, True
        if kind == "resume":
            import ats
            pdf = ats.default_pdf(ats.CANONICAL)
            item["file"] = str(pdf) if pdf else None
            item["pdf_url"] = f"/pdf/{ats.CANONICAL}" if pdf else None
        answers.append(item)
    sent = conn.execute("SELECT sent_at FROM application WHERE job_id = ? AND status = 'sent'",
                        (job_id,)).fetchone()
    return {"job_id": job_id, "company": job["company"], "title": job["title"],
            "applied": sent["sent_at"] if sent else None,
            "blockers": blockers, "flags": flags, "answers": answers}


def build(conn, job_id: int) -> dict:
    job = load_job(conn, job_id)
    if not job:
        return {}
    ctx = context(conn, job)
    lines, picked, why_source = ctx["lines"], ctx["stories"], ctx["why_source"]

    # The defences this particular advert is most likely to provoke.
    text = (job.get("description") or "").lower()
    defences = []
    for question, answer in lines.items():
        q = question.lower()
        if any(k in q for k in ("four employers", "why you over", "weakness",
                                "why are you leaving", "how much of this did ai")):
            defences.append((question, answer))
        elif "ai" in text and "ai" in q:
            defences.append((question, answer))

    # The advert's own form, when `forms.py` managed to fetch one. Otherwise
    # the seven questions below, which is what this file did before forms
    # existed and what it still does for every non-Greenhouse board.
    import forms
    real_fields = forms.cached(conn, job_id)
    if real_fields:
        blockers, flags = forms.classify(real_fields)
        return {
            "job": job,
            "why_source": why_source,
            "form": True,
            "blockers": blockers,
            "flags": flags,
            "sections": sections_from_form(conn, job, real_fields, ctx),
            "stories": picked,
            "defences": defences[:6],
        }

    return {
        "job": job,
        "why_source": why_source,
        "form": False,
        "blockers": [],
        "flags": [],
        "sections": [
            ("Cover letter", ctx["cover_letter"]),
            ("Why me — 20 seconds",
             lines.get("Why you (20 sec)", "").strip()),
            ("Tell me about yourself — 90 seconds",
             lines.get("Tell me about yourself (90 sec)", "").strip()),
            ("Can you work our hours?", ctx["hours"]),
            ("What are your salary expectations?", salary_answer(job)),
            ("Do you need visa sponsorship?", sponsorship_answer(job)),
            ("When can you start?", ctx["start_date"]),
        ],
        "stories": picked,
        "defences": defences[:6],
    }


def render(pack: dict) -> str:
    job = pack["job"]
    out = [f"# {job['title']}", f"**{job['company']}**"]
    if pack.get("blockers"):
        out.append("\n> **STOP — the form itself rules him out.**")
        out.extend(f"> - {b}" for b in pack["blockers"])
    if pack.get("flags"):
        out.append("\n> Answer these yourself:")
        out.extend(f"> - {f}" for f in pack["flags"])
    if job.get("canonical_url"):
        out.append(f"\n{job['canonical_url']}")
    facts = []
    if job.get("score") is not None:
        facts.append(f"score {job['score']:.0f}")
    if job.get("reach"):
        facts.append(job["reach"].replace("_", " "))
    if job.get("work_mode"):
        facts.append(job["work_mode"])
    if job.get("overlap_hours") is not None:
        facts.append(f"{job['overlap_hours']:g}h overlap")
    if facts:
        out.append("\n`" + "` · `".join(facts) + "`")

    if pack.get("form"):
        out.append("\n---\n\n*The questions below are the advert's own "
                   "application form, in its order.*")
    for heading, body in pack["sections"]:
        if body:
            out.append(f"\n---\n\n## {heading}\n\n{body}")

    if pack["stories"]:
        out.append("\n---\n\n## Stories to tell in this interview\n")
        for story in pack["stories"]:
            out.append(f"### {story['title']}\n")
            out.append(f"- **Situation:** {story['s']}")
            out.append(f"- **Task:** {story['t']}")
            out.append(f"- **Action:** {story['a']}")
            out.append(f"- **Result:** {story['r']}")
            if story["point"]:
                out.append(f"- **Land this:** {story['point']}\n")

    if pack["defences"]:
        out.append("\n---\n\n## If they push\n")
        for question, answer in pack["defences"]:
            out.append(f"**{question}**\n\n{answer}\n")

    return "\n".join(out)


def pack_path(job_id: int, pack: dict) -> Path:
    """One filename per job, so `kit.py --write` and `applied.py --next` do not
    each leave their own copy of the same pack in kits/."""
    slug = re.sub(r"[^a-z0-9]+", "-",
                  f"{pack['job']['company']}-{pack['job']['title']}".lower())[:70]
    return OUT_DIR / f"{job_id}-{slug.strip('-')}.md"


def write_pack(job_id: int, pack: dict, text: str | None = None) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = pack_path(job_id, pack)
    path.write_text(text if text is not None else render(pack))
    return path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_id", nargs="?", type=int)
    ap.add_argument("--top", type=int, metavar="N")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    conn = connect()

    ids = []
    if args.job_id:
        ids = [args.job_id]
    elif args.top:
        ids = [r["job_id"] for r in conn.execute(
            "SELECT sc.job_id FROM score sc JOIN fit f ON f.job_id = sc.job_id "
            "WHERE f.viable = 1 ORDER BY sc.total DESC LIMIT ?", (args.top,))]
    else:
        ap.print_help()
        return 2

    for job_id in ids:
        pack = build(conn, job_id)
        if not pack:
            print(f"no job {job_id}", file=sys.stderr)
            continue
        text = render(pack)
        if args.write:
            path = write_pack(job_id, pack, text)
            print(f"wrote {path.relative_to(HERE)}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
