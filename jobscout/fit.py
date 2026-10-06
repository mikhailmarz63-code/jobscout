#!/usr/bin/env python3
"""Stage 6: the two questions that decide whether a job is worth an evening.

Added after an audit of the top forty jobs against the real CV showed the
ranking was sorting jobs the candidate could legally take, without checking
whether they could actually do them, or do them *from where they live*.

So two more gates, both of which the eligibility gate deliberately does not ask.

**1. Is it remote?** "Open to APAC" is not "remote". A job can be geographically
open to him and still want a desk in Singapore. Unless it is in Sri Lanka, or
carries a sponsored relocation, it has to be remote or it is not a job.

**2. Would they take him?** An early-career CV does not get shortlisted for
Senior titles. A Senior title he will never be shortlisted for is worth less
than a junior one he might get, whatever it pays. `reach` says so out loud instead of
burying it in a score.

`viable` is both answers combined, and it is what the daily list filters on.

    python3 fit.py                # decide everything undecided
    python3 fit.py --all          # re-decide
    python3 fit.py --breakdown    # the picture, by reach and work mode
    python3 fit.py --rejected 20  # what got dropped, and why
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402
from sources import sentence_around, settings                # noqa: E402

# --------------------------------------------------------------- work mode --
# Ordered by how much the phrase actually commits the employer. Hybrid and
# on-site are checked first because "we are a remote-friendly company that meets
# in the office three days a week" contains both, and the three days are the
# binding part.

HYBRID_RULES = [
    (r"\bhybrid\b", "says hybrid"),
    (r"\d+\s*(?:-\s*\d+\s*)?days?\s*(?:a|per)\s*week\s*(?:in|at|from)\s*(?:the\s*)?office",
     "days per week in the office"),
    (r"(?:in|at)\s*(?:the\s*)?office\s*\d+\s*days?", "days in the office"),
    (r"\bpartially\s+remote\b", "partially remote"),
    (r"\bremote[- ]friendly\b.{0,60}\boffice\b", "remote-friendly but office-based"),
]

ONSITE_RULES = [
    (r"\bon[- ]?site\b(?!\s*(?:or|/)\s*remote)", "says on-site"),
    (r"\bin[- ]?office\b", "in-office"),
    (r"\bmust be able to commute\b", "must commute"),
    (r"\brelocat\w+ to\b", "asks you to relocate"),
    (r"\bbased (?:in|at) our\b", "based at their office"),
    (r"\bthis is not a remote (?:role|position|job)\b", "explicitly not remote"),
    (r"\bno remote work\b", "no remote work"),
]

REMOTE_RULES = [
    (r"\b(?:100%|fully|entirely|completely)\s*remote\b", "fully remote"),
    (r"\bremote[- ]first\b", "remote-first"),
    (r"\bwork from (?:home|anywhere)\b", "work from home/anywhere"),
    (r"\bwork remotely\b", "work remotely"),
    (r"\bremote (?:role|position|job|opportunity|team)\b", "a remote role"),
    (r"\b(?:fully )?distributed (?:team|company|workforce)\b", "distributed team"),
    (r"\bthis (?:role|position) is remote\b", "the role is remote"),
    (r"\banywhere in the world\b", "anywhere in the world"),
]

# The location field says it outright far more reliably than the prose does.
MODE_FROM_LOCATION = [
    (r"\bhybrid\b", "hybrid", "location field says hybrid"),
    (r"\bon[- ]?site\b", "onsite", "location field says on-site"),
    (r"\bremote\b|\banywhere\b|\bworldwide\b", "remote", "location field says remote"),
]

# Is this his profession at all?
#
# The skills list further down is read against the whole advert, and Anthropic's
# Enterprise Account Executive posting mentions AI, Claude, LLMs, APIs and
# automation twelve times -- because that is what the company sells, not what
# the job does. It scored full marks on profile fit for a sales role. The title
# is the honest signal, so it gets its own component and its own weight.
#
# Two tiers, because a single flat list gave the same full credit to "Backend
# Engineer" -- which is his CV -- and to "Product Manager", which is a
# profession he has never held. The audit judged every product-management
# posting in the top 40 as not his field.
#
# STRONG is what he has actually done and can evidence. ADJACENT is work he
# could plausibly move into, scored at partial credit rather than full.
HIS_FAMILY = re.compile(
    r"\b(software|engineer|engineering|developer|programmer|backend|back[- ]end|"
    r"frontend|front[- ]end|full[- ]?stack|data|analyst|analytics|business "
    r"analys\w+|automation|integration|devops|sre\b|"
    r"qa\b|quality assurance|tester|testing|"
    r"machine learning|\bai\b|\bml\b|prompt|database|"
    # Added 2026-09-03 with the repositioning from Business Analyst to AI
    # Solutions Engineer. These are the target titles now, not adjacent ones:
    # the role that scopes with the client and then builds it. "Forward
    # deployed" is the Palantir/Anthropic/OpenAI name for the same job and
    # previously matched nothing at all.
    r"solutions? engineer|solutions? architect|solutions? consultant|"
    r"forward[- ]deployed|implementation engineer|applied ai|"
    r"\bllms?\b|generative ai|gen[- ]?ai|\brag\b|"
    r"scripting|web develop\w+)\b", re.I)

ADJACENT_FAMILY = re.compile(
    r"\b(product manager|product owner|project manager|programme manager|"
    r"technical writer|documentation|developer advocate|developer educator|"
    r"solutions?|implementation|support engineer|customer success|"
    r"operations|ops\b|systems?|technical|it\b|platform|infrastructure|"
    # Moved here from OFF_FAMILY on 2026-09-03. The earlier audit called GRC "a
    # security-compliance career with its own certifications" and rejected it
    # outright. Adjacent is the honest score: plausible to move into, not yet
    # something he has done. Not HIS_FAMILY.
    r"\bgrc\b|governance,? risk|risk and compliance|compliance analyst|"
    r"compliance officer|security analyst|information security|infosec|"
    # Presales is half the target role; STRADDLE_OK carries the sales half.
    r"pre[- ]?sales|"
    r"application|specialist|coordinator|consultant)\b", re.I)

# Professions that are not his, however well the advert happens to score.
OFF_FAMILY = re.compile(
    r"\b(account executive|account manager|sales|seller|sdr\b|bdr\b|"
    r"business development representative|quota|recruiter|recruiting|talent "
    # \w* prefixes, because "Teleradiologist" has no word boundary before
    # "radiolog" and was scored as merely an unfamiliar title rather than
    # another profession entirely.
    r"acquisition|nurse|nursing|physician|\w*radiolog\w+|\w*patholog\w+|"
    r"clinician|therapist|"
    r"pharmac\w+|dental|veterinar\w+|attorney|paralegal|counsel|litigation|"
    r"accountant|bookkeep\w+|auditor|actuar\w+|underwrit\w+|teacher|tutor|"
    r"instructor|professor|driver|courier|chef|cook|barista|welder|"
    r"electrician|plumber|carpenter|janitor|custodian|security guard|"
    r"insurance agent|real estate|realtor|loan officer|copywriter|"
    r"social media manager|interpreter|translator|transcription\w*|"
    # Added 2026-08-24 from the independent audit, which named each of these a
    # different profession even though every one of them matched HIS_FAMILY on
    # a single generic word.
    #
    # "Precast Design Engineer" (Fisher Associates) matched "Engineer" and is
    # structural engineering -- concrete, not code.
    r"precast|structural engineer|civil engineer|mechanical engineer|"
    r"electrical engineer|hvac|geotechnical|surveyor|draughts\w*|drafter|"
    # "Auditor"/"internal audit" stay off-family — that is the accounting
    # profession. GRC and security-compliance titles moved OUT of this list on
    # 2026-09-03; see ADJACENT_FAMILY for why.
    r"internal audit|"
    # "Contract Legal Assistant" (Customer.io) matched nothing good and was
    # carried by its description.
    r"legal assistant|legal counsel|contracts? (?:manager|administrator)|"
    # "Benefits Operations Lead" (Gusto) matched "Operations".
    r"benefits (?:operations|analyst|specialist)|payroll|"
    # "Medical Licensing Specialist (Contract)" (Galileo) reached the adjacent
    # tier on the bare word "Specialist".
    r"medical licensing|licensing specialist|credentialing|"
    r"claims (?:adjuster|specialist)|billing specialist|"
    r"human resources|people operations|hris)\b", re.I)


# The only off-family words that genuinely *combine* with a technical role
# rather than qualifying it out of his field.
#
# "Sales Engineer" is half his job. "Precast Design Engineer" is not: "precast"
# says which discipline of engineering, and the answer is concrete rather than
# code. The first version treated every off-family-plus-his-family title as a
# hybrid and scored "Precast Design Engineer", "GRC Analyst" and "Benefits
# Operations Lead" at +0.1 instead of rejecting them — all three had been named
# a different profession by the independent audit.
STRADDLE_OK = re.compile(
    r"\b(sales|account|solutions?|customer success|pre[- ]?sales|"
    r"business development|partner)\b", re.I)


# Fields he is moving INTO, which qualify a his-family word rather than sharing
# it. Added 2026-09-03 with the security repositioning.
#
# Without this, "GRC Analyst" and "Security Analyst" both scored 1.0 — because
# the bare word "analyst" is in HIS_FAMILY, so every *-Analyst title collected
# full credit the moment GRC left OFF_FAMILY. That is the same bug the earlier
# audit caught with "Precast Design Engineer", in the opposite direction:
# rejecting outright was wrong, but calling it "on his CV" is also wrong.
#
# He has done security WORK — a hardening pass on a live ASP.NET application, a
# PCI DSS capstone — but has never held a security ROLE. Adjacent is the honest
# score, and it keeps these roles surfaced without letting them outrank the
# engineering titles he can actually evidence.
MOVING_INTO = re.compile(
    r"\b(grc|governance,? risk|risk and compliance|compliance|"
    r"security|infosec|cyber\w*|soc analyst|penetration|pentest\w*)\b", re.I)


def role_fit(title: str) -> tuple[float, str]:
    """-1.0 to +1.0, from the title alone.

    Read only from the title, never the body. A job advert's body describes the
    company as much as the role, and the company is usually a technology
    company whichever job it is advertising.
    """
    title = title or ""
    off = OFF_FAMILY.search(title)
    his = HIS_FAMILY.search(title)
    near = ADJACENT_FAMILY.search(title)

    if off and not (his or near):
        return -1.0, f"'{off.group(0)}' is a different profession"
    if off and (his or near):
        if STRADDLE_OK.search(off.group(0)):
            # Raised from 0.1 on 2026-09-03. "Sales Engineer" / "Solutions
            # Engineer" are the target lane under the AI-solutions positioning,
            # not a near-miss to be buried: the job is to scope with the client
            # and then build it. Still below HIS_FAMILY (1.0) — the sales half
            # is real and he has never carried a quota — but above a merely
            # ADJACENT title (0.4), which it outranks on fit.
            return 0.5, f"straddles: '{off.group(0)}' and '{(his or near).group(0)}'"
        # The off-family word is qualifying the role, not sharing it.
        return -1.0, f"'{off.group(0)}' — a different kind of {(his or near).group(0)}"
    moving = MOVING_INTO.search(title)
    if moving and (his or near):
        return 0.4, (f"'{moving.group(0)}' — a field he is moving into, "
                     f"not one he has worked in")
    if his:
        return 1.0, f"'{his.group(0)}' — on his CV"
    if near:
        return 0.4, f"'{near.group(0)}' — adjacent, not something he has done"
    return -0.2, "title names no role he has done"

_UNUSED_YEARS = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?years?(?:\s+of)?\s+"
                   r"(?:relevant\s+|professional\s+|industry\s+)?experience", re.I)

# What he can actually do, from resume/resume-ai-solutions-engineer.md. Separate from the CV
# variant keywords because those pick *which CV*; these say whether any of them
# would land at all.


# ------------------------------------------------------------------ reach --
# Every pattern here came out of the audit. The comment on each says which job
# it was written for, because a rule with no case behind it is a guess.

# Two tiers, and the split matters more than either list.
#
# HARD markers outrank an entry marker: "Associate Director" is a director.
# SOFT markers lose to one: "Associate Product Manager" is a graduate job, and
# "Junior Product Manager" plainly is. The first version had a single list with
# lookbehinds -- `(?<!associate )\bmanager\b` -- and it failed, because the entry
# word was stripped from the title *before* the pattern ran, so the lookbehind
# had nothing left to see. Both genuine graduate roles in the top 40 were
# dropped as no_chance.
HARD_SENIOR_TITLE = re.compile(
    r"\b(chief|c[teoif]o\b|vp\b|vice[- ]president|head of|director|"
    r"principal|distinguished|fellow|architect|partner|"
    r"general manager|managing director|"
    # "Staff Product Manager" (Brightwheel) -- staff sits above senior.
    r"staff\b|"
    # "Benefits Operations Lead" (Gusto) -- lead means reports.
    r"\blead\b|"
    # "Backend Developer Level III" (Hudson Manpower), "DevOps Engineer IV"
    # (Jumio) -- level three and up is a senior band everywhere.
    r"level\s*(?:iii|iv|v|vi|[3-9])\b|\b(?:iii|iv|v)\b)", re.I)

# Senior on their own; junior when something junior is in front of them.
SOFT_SENIOR_TITLE = re.compile(
    # "Engineering Manager, Experimentation" (LaunchDarkly),
    # "Revenue Strategy & Operations Manager" (Mixpanel).
    r"\bmanager\b|"
    # "Product Owner, Tools & Systems" (E. Breuning), "Product Owner" (Duel).
    r"\bproduct owner\b|\bowner\b", re.I)

STRETCH_TITLE = re.compile(
    # "Senior AI Engineer" (Lemon.io), "Senior Product Manager" (Toast),
    # "Application Security Engineer II" (Abnormal).
    r"\b(senior|sr\.?|snr\.?|expert|specialist ii|level\s*(?:ii|2)\b|\bii\b)\b", re.I)

ENTRY_TITLE = re.compile(
    r"\b(junior|jr\.?|graduate|grad\b|entry[- ]level|intern|internship|trainee|"
    r"apprentice|associate|assistant|new grad|early career|placement)\b", re.I)

MID_TITLE = re.compile(
    r"\b(mid[- ]level|mid[- ]weight|analyst|coordinator|administrator|officer|"
    r"engineer|developer|consultant|specialist|designer|scientist)\b", re.I)

# "8–10+ years of professional software engineering experience".
#
# The first version had a fixed whitelist of modifier words between "years of"
# and "experience", and an ASCII-hyphen-only range. Both failed constantly on
# real adverts: an en-dash range, or any wording the whitelist did not
# anticipate, and the requirement became invisible. It now allows an en/em dash
# and up to six arbitrary words in between, stopping at a sentence boundary so
# it cannot reach across into an unrelated clause.
YEARS = re.compile(
    r"(\d+)\s*(?:\+|or more)?\s*(?:[-–—]\s*(\d+)\s*\+?\s*)?years?"
    # Either "... experience" a few words later, or one of the phrasings that
    # states a requirement without ever using the word: "5+ years in a similar
    # role", "3+ years building distributed systems".
    r"(?:(?:\s+of)?\s+(?:(?!\.)[\w/&-]+\s+){0,6}?experience"
    r"|\s+(?:in|with|as an?|building|developing|writing|working|shipping|"
    r"leading|designing)\b)", re.I)


# Two contexts where a years figure is not a requirement he has to meet.
#
#   "2 years relevant experience, or 4 years in lieu of a degree" -- the four is
#   an alternative route for candidates without a degree. He has one, so taking
#   the maximum would invent a requirement the advert does not make of him.
#
#   "Bachelor's degree and less than 2 years of experience" -- a ceiling, and an
#   entry-level signal, read as a floor.
NOT_A_REQUIREMENT = re.compile(
    r"in lieu of|without a degree|instead of a degree|"
    r"less than|fewer than|up to|no more than|at most|maximum of", re.I)


def years_required(description: str) -> int | None:
    """The *largest* figure the advert genuinely asks for.

    Largest rather than first, because Lemon.io publishes several role tracks in
    one description -- "2+ years" for one and "7+ years" for another -- and
    reading only the leftmost match let a senior posting present itself as a
    graduate one.

    But "largest" has to skip the figures that are not requirements at all, or
    an advert offering a degree-free alternative route reads as stricter than
    the one it actually applies to him.
    """
    text = description or ""
    numbers: list[int] = []
    for match in YEARS.finditer(text):
        context = text[max(0, match.start() - 40):match.end() + 20]
        if NOT_A_REQUIREMENT.search(context):
            continue
        for value in match.groups():
            if value and value.isdigit() and int(value) < 40:
                numbers.append(int(value))
    return max(numbers) if numbers else None


# Some adverts gate on experience in plain English instead of a number. Elite
# Software Automation's Business Analyst -- which was sitting third on his list
# -- says outright that beginners will be rejected.
BEGINNERS_REJECTED = re.compile(
    r"only accepting experienced candidates|experienced candidates only|"
    r"if you (?:are|'re) a beginner[^.]{0,60}reject|"
    r"no (?:junior|entry[- ]level|graduate)s? (?:need apply|considered)|"
    r"not (?:a|an) (?:junior|entry[- ]level|graduate) (?:role|position)|"
    r"this is not an entry[- ]level", re.I)

# A title can hide a people-management job that the body states plainly.
MANAGES_TEAM = re.compile(
    r"manag(?:e|ing) a team|lead(?:ing)? a team of|direct reports|"
    r"you will manage \d+|grow(?:ing)? (?:and lead(?:ing)? )?the team|"
    r"hire and manage|people management", re.I)

# He has about eighteen months. These thresholds are set against that, not
# against a general idea of seniority.
YEARS_NO_CHANCE = 5
YEARS_STRETCH = 3
YEARS_HAVE = 1.5
SENIORITY = "junior"

# Running for someone else: their profile replaces the owner's vocabulary.
# Everything above stays the default, so the owner's run is unchanged.
import person                               # noqa: E402
_PROFILE = person.profile()
if _PROFILE:
    HIS_FAMILY = person.title_regex(_PROFILE.get("target_titles")) or HIS_FAMILY
    ADJACENT_FAMILY = person.title_regex(_PROFILE.get("adjacent_titles")) or person.NEVER
    OFF_FAMILY = person.title_regex(_PROFILE.get("avoid_titles")) or OFF_FAMILY
    MOVING_INTO = person.title_regex(_PROFILE.get("moving_into")) or person.NEVER
    YEARS_HAVE = float(_PROFILE.get("years_experience") or YEARS_HAVE)
    SENIORITY = str(_PROFILE.get("seniority") or SENIORITY).lower()
    # The thresholds keep the same distance from what they have.
    YEARS_STRETCH = YEARS_HAVE + 1.5
    YEARS_NO_CHANCE = YEARS_HAVE + 3.5

REACH_ORDER = ["likely", "plausible", "stretch", "no_chance"]


@dataclass
class Fit:
    work_mode: str = "unknown"
    mode_quote: str = ""
    mode_rule: str = ""
    reach: str = "plausible"
    reach_why: str = ""
    viable: bool = False
    viable_why: str = ""


_sentence = sentence_around   # one definition, one window, every stage


def work_mode(location_raw: str, description: str, tags: list[str]
              ) -> tuple[str, str, str]:
    """(mode, quote, rule). The location field first, then the prose.

    Hybrid and on-site are tested before remote, because an advert that says
    "remote-friendly" and "three days a week in the office" is a hybrid job, and
    the three days are the half that binds.
    """
    field = (location_raw or "").strip()
    for pattern, mode, rule in MODE_FROM_LOCATION:
        if re.search(pattern, field, re.I):
            return mode, f"location field: {field}"[:240], rule

    if "remote" in " ".join(tags or []).lower():
        return "remote", "tagged remote by the board", "tag:remote"

    text = (description or "")[:14000]
    for rules, mode in ((HYBRID_RULES, "hybrid"), (ONSITE_RULES, "onsite"),
                        (REMOTE_RULES, "remote")):
        for pattern, rule in rules:
            match = re.search(pattern, text, re.I)
            if match:
                return mode, _sentence(text, match), f"text:{rule}"

    return "unknown", "", ""


def reach(title: str, description: str) -> tuple[str, str]:
    """How likely is he to actually be taken? Title first, always.

    The ordering is the whole point. An earlier version read years-of-experience
    from the body before it looked at the title, so "Senior AI Engineer" whose
    advert mentioned "2 years" anywhere scored a near-maximum *entry-level*
    bonus. Thirty-one of 306 jobs had a senior marker in the title and escaped
    the penalty entirely.
    """
    title = title or ""
    body = description or ""

    # 1. A hard marker outranks everything, including an entry word.
    hard = HARD_SENIOR_TITLE.search(title)
    if hard:
        return "no_chance", f"'{hard.group(0).strip()}' is a level above him"

    # 2. A body that manages people, or refuses beginners outright, is a hard
    #    no whatever the title says.
    manages = MANAGES_TEAM.search(body)
    if manages:
        return "no_chance", f"the advert says '{manages.group(0)}'"
    refuses = BEGINNERS_REJECTED.search(body)
    if refuses:
        return "no_chance", f"the advert says '{refuses.group(0)}'"

    years = years_required(body)

    # 3. An entry marker then beats a soft one: "Associate Product Manager" and
    #    "Junior Product Manager" are graduate jobs that happen to say Manager.
    entry = ENTRY_TITLE.search(title)
    if entry:
        if years and years >= YEARS_NO_CHANCE:
            return "stretch", (f"'{entry.group(0)}' title, but the advert asks "
                               f"for {years}+ years")
        return "likely", f"'{entry.group(0)}' in the title"

    # 4. A soft marker with nothing junior in front of it means people or a P&L.
    soft = SOFT_SENIOR_TITLE.search(title)
    if soft and SENIORITY in ("mid", "senior"):
        return "plausible", f"'{soft.group(0).strip()}' at a {SENIORITY} level"
    if soft:
        return "no_chance", f"'{soft.group(0).strip()}' with no junior qualifier"

    stretch = STRETCH_TITLE.search(title)
    if stretch:
        # A senior title can only be made worse by the body, never better --
        # a years figure used to silently overrule it.
        if years and years >= YEARS_NO_CHANCE:
            return "no_chance", f"'{stretch.group(0)}' and asks for {years}+ years"
        if SENIORITY == "senior":
            return "likely", f"'{stretch.group(0)}' title, their level"
        if SENIORITY == "mid":
            return "plausible", f"'{stretch.group(0)}' title, one step up"
        return "stretch", f"'{stretch.group(0)}' title"

    # 5. Only now does the body get a say on its own.
    if years:
        if years >= YEARS_NO_CHANCE:
            return "no_chance", f"asks for {years}+ years; he has about {YEARS_HAVE:g}"
        if years >= YEARS_STRETCH:
            return "stretch", f"asks for {years}+ years; he has about {YEARS_HAVE:g}"
        return "likely", f"asks for {years} years"

    if MID_TITLE.search(title):
        return "plausible", "mid-level title, no seniority marker"
    return "plausible", "level not stated"


def viability(state: str, country: str | None, mode: str, reach_level: str,
              me_country: str, role_value: float = 0.0,
              role_why: str = "") -> tuple[bool, str]:
    """Can he take it, and would they have him? Both, or it is not on the list.

    On-site is only acceptable in two cases: the job is in Sri Lanka, where he
    already lives, or it comes with the sponsored relocation he asked to see.
    Everything else has to be remote.
    """
    # The eligibility gate comes first. A US-only role that is remote and
    # pitched at his level is still a role he cannot have, and an earlier
    # version let 337 of them through by checking only the work mode.
    if state in ("BLOCKED", "ONSITE_NO_SPONSOR"):
        return False, "closed to anyone in Sri Lanka"
    if reach_level == "no_chance":
        return False, "a level you will not be shortlisted for"

    # Profession is a gate, not a penalty.
    #
    # An adversarial review made the point that settled this: eligibility,
    # timezone, pay and freshness are 80 of the 130 scoring points and none of
    # them say anything about whether he is qualified. A job in the wrong
    # profession lost 20 points and kept its place, so nineteen of the ninety-
    # eight viable jobs were somebody else's career -- an Online English
    # Teacher, a Medical Licensing Specialist, a Contract Legal Assistant.
    # Scoring cannot fix that; only removing them can.
    if role_value <= -1.0:
        return False, f"a different profession — {role_why}"

    in_sri_lanka = state == "ONSITE_LK" or country == me_country
    if in_sri_lanka:
        return True, "in Sri Lanka — on-site is fine"
    if state == "ONSITE_SPONSORED":
        return True, "on-site abroad, but sponsored"

    if mode == "onsite":
        return False, "on-site abroad, no sponsorship offered"
    if mode == "hybrid":
        return False, "hybrid — an office you cannot reach"
    if mode == "remote":
        return True, "remote"

    # Mode unknown. "Open to anyone, anywhere" is remote by construction --
    # nobody offers a desk in every country at once. A region-limited job is not
    # so lucky: it could easily be an office in Singapore.
    if state in ("OPEN_WORLDWIDE", "OPEN_CONTRACTOR"):
        return True, "open worldwide, so remote by construction"
    return False, "never says whether it is remote, and is not worldwide"


def apply(conn, redo: bool = False) -> dict:
    started = now()
    me = settings()["candidate"]["country"]
    where = "" if redo else " AND f.job_id IS NULL"
    rows = conn.execute(f"""
        SELECT j.id, j.title, jt.description, e.state, g.country,
               (SELECT p.location_raw FROM posting p WHERE p.job_id = j.id
                 AND p.location_raw <> '' ORDER BY length(p.location_raw) DESC
                 LIMIT 1) loc,
               (SELECT p.tags FROM posting p WHERE p.job_id = j.id LIMIT 1) tags
        FROM job j
        LEFT JOIN job_text jt ON jt.job_id = j.id
        JOIN eligibility e ON e.job_id = j.id
        LEFT JOIN geo g ON g.job_id = j.id
        LEFT JOIN fit f ON f.job_id = j.id
        WHERE j.status = 'open' {where}
    """).fetchall()

    stats = {"viable": 0, "dropped": 0, **{r: 0 for r in REACH_ORDER},
             "remote": 0, "hybrid": 0, "onsite": 0, "unknown_mode": 0,
             "off_profession": 0}

    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []

        mode, quote, rule = work_mode(r["loc"] or "", r["description"] or "", tags)
        level, why = reach(r["title"] or "", r["description"] or "")
        role_value, role_why = role_fit(r["title"] or "")
        ok, ok_why = viability(r["state"], r["country"], mode, level, me,
                               role_value, role_why)

        stats[level] += 1
        stats["unknown_mode" if mode == "unknown" else mode] += 1
        stats["viable" if ok else "dropped"] += 1
        if role_value <= -1.0:
            stats["off_profession"] += 1

        conn.execute("""
            INSERT INTO fit (job_id, work_mode, mode_quote, mode_rule, reach,
                             reach_why, viable, viable_why, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                work_mode = excluded.work_mode, mode_quote = excluded.mode_quote,
                mode_rule = excluded.mode_rule, reach = excluded.reach,
                reach_why = excluded.reach_why, viable = excluded.viable,
                viable_why = excluded.viable_why, decided_at = excluded.decided_at
        """, (r["id"], mode, quote or None, rule or None, level, why,
              1 if ok else 0, ok_why, now()))

    conn.commit()
    log_stage(conn, "fit", True, json.dumps(stats), started)
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--breakdown", action="store_true")
    ap.add_argument("--rejected", type=int, metavar="N")
    args = ap.parse_args(argv)

    conn = connect()

    if args.breakdown:
        print("\n  reach — would they take him?")
        for r in conn.execute(
                "SELECT f.reach, COUNT(*) n, SUM(f.viable) v FROM fit f "
                "JOIN job j ON j.id = f.job_id WHERE j.status = 'open' "
                "GROUP BY f.reach"):
            print(f"    {r['reach']:<12} {r['n']:>5}   {r['v'] or 0:>4} viable")
        print("\n  work mode")
        for r in conn.execute(
                "SELECT f.work_mode, COUNT(*) n FROM fit f JOIN job j ON j.id = f.job_id "
                "WHERE j.status = 'open' GROUP BY f.work_mode ORDER BY n DESC"):
            print(f"    {r['work_mode']:<12} {r['n']:>5}")
        print("\n  why jobs were dropped")
        for r in conn.execute(
                "SELECT f.viable_why, COUNT(*) n FROM fit f JOIN job j ON j.id = f.job_id "
                "WHERE f.viable = 0 AND j.status = 'open' "
                "GROUP BY f.viable_why ORDER BY n DESC"):
            print(f"    {r['n']:>5}  {r['viable_why']}")
        return 0

    if args.rejected:
        for r in conn.execute("""
                SELECT j.company, j.title, f.viable_why, f.reach_why
                FROM fit f JOIN job j ON j.id = f.job_id
                JOIN eligibility e ON e.job_id = j.id
                WHERE f.viable = 0 AND j.status = 'open'
                  AND e.state NOT IN ('BLOCKED', 'ONSITE_NO_SPONSOR')
                LIMIT ?""", (args.rejected,)):
            print(f"  {r['company'][:20]:<20} {r['title'][:42]:<42} {r['viable_why']}")
        return 0

    stats = apply(conn, redo=args.all)
    print(f"{stats['viable']} viable, {stats['dropped']} dropped")
    print(f"  likely {stats['likely']}   plausible {stats['plausible']}   "
          f"stretch {stats['stretch']}   no chance {stats['no_chance']}")
    print(f"  remote {stats['remote']}   hybrid {stats['hybrid']}   "
          f"onsite {stats['onsite']}   unstated {stats['unknown_mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
