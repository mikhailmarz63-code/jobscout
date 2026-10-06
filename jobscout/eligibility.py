#!/usr/bin/env python3
"""Stage 4: the gate. Can the candidate actually take this job?

Most "remote" postings are silently geo-locked. A system that skips this
question produces a beautiful ranked list of jobs he cannot have, and the
failure is invisible -- every row looks fine until he reads the small print in
week three. So this runs before scoring, and every permissive answer has to
carry the sentence that produced it.

**Evidence is not optional.** `schema.sql` refuses to store any of the five
sendable states without a quote, a URL and a rule name. That is deliberate: the
one thing that must never happen is an unattended sender applying to a US-only
role because a regex was loose.

**The precedence order matters more than the individual rules.** A posting can
say three contradictory things -- "Remote (US)" in the location field and "we
sponsor visas" in paragraph nine. They are ranked here, best-case first, and the
first rule that fires with evidence wins.

**Uncertainty resolves to UNKNOWN, never to a guess in either direction.**
"EMEA" is the clearest case: Sri Lanka is not in Europe, the Middle East or
Africa, so the literal reading is BLOCKED -- but plenty of EMEA-labelled roles
hire across South Asia in practice. Marking those BLOCKED hides real jobs;
marking them OPEN_REGION would let the sender apply to jobs he is ineligible
for. They go to review, which is what review is for.

    python3 eligibility.py                # decide everything undecided
    python3 eligibility.py --all          # re-decide (after a rule change)
    python3 eligibility.py --breakdown    # counts by state, and why
    python3 eligibility.py --review 20    # what needs a human
    python3 eligibility.py --explain 412  # one job, every rule that looked at it
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

SENDABLE = ("OPEN_WORLDWIDE", "OPEN_REGION", "OPEN_CONTRACTOR",
            "ONSITE_LK", "ONSITE_SPONSORED")


@dataclass
class Decision:
    state: str
    quote: str = ""
    url: str = ""
    rule: str = ""
    confidence: float = 0.0
    decided_by: str = "rule"


# ------------------------------------------------------------ vocabulary --
# Kept as plain lists rather than one clever regex, because every entry here is
# a judgment about geography that someone may need to argue with later.

WORLDWIDE = [
    "anywhere in the world", "anywhere in world", "work from anywhere",
    "worldwide", "world wide", "globally remote", "fully global", "any country",
    "anywhere globally", "location independent", "no location restriction",
    "no geographic restriction", "remote - global", "remote (global)",
    "global remote", "anywhere on earth", "100% remote worldwide",
]

# Sri Lanka itself: the best case, because he is already in the country and
# neither a visa nor a timezone compromise is involved.
SRI_LANKA = ["sri lanka", "srilanka", "colombo", "kandy", "galle", "negombo"]

# Regions that genuinely contain Sri Lanka.
ASIA_INCLUSIVE = [
    "asia", "apac", "asia-pacific", "asia pacific", "south asia",
    "southern asia", "indian subcontinent", "south-east asia",
    "southeast asia", "india, sri lanka", "asean",
]

# Adjacent-but-arguable. Literally these exclude Sri Lanka; in practice many of
# them hire across South Asia anyway. Neither reading is safe, so: review.
AMBIGUOUS = [
    "emea", "europe, middle east", "global south", "eastern hemisphere",
    "any timezone", "most countries", "select countries", "several countries",
    "multiple regions", "international",
]

# A restriction naming only these is a closed door.
EXCLUSIVE = [
    "united states", "usa", "u.s.", "us only", "us-based", "united kingdom",
    "uk only", "canada", "north america", "latam", "latin america", "europe",
    "european union", "eu only", "eea", "australia", "new zealand", "anz",
    "germany", "france", "spain", "netherlands", "poland", "portugal",
    "ireland", "brazil", "mexico", "argentina", "colombia", "japan", "china",
    "singapore", "philippines", "south africa", "nigeria", "kenya", "egypt",
    "israel", "uae", "saudi", "turkey", "ukraine", "romania",
    # Bare codes, and only the four that cannot be read as an ordinary word.
    # "Remote (US)" reduces to two letters once the generic words and
    # punctuation are stripped, so the named-place catch-all never sees it and
    # something has to name it here.
    #
    # The full ISO-2 list was tried and reverted: `no` is Norway *and* the word
    # "no", so "Remote — no location restrictions" came back BLOCKED by Norway.
    # `de`, `co`, `ca`, `ar`, `se` and `is` fail the same way. Every other
    # country reaches BLOCKED through named_place regardless, so the two-letter
    # codes were buying nothing and costing correctness on the one kind of
    # posting that matters most here.
    "us", "uk", "eu", "uae",
]

# Sentence-level rules read against the description, for the sources that
# publish no structured location field at all (ATS boards, HN, Arbeitnow).
TEXT_RULES = [
    ("worldwide_text", "OPEN_WORLDWIDE", 0.9, [
        r"work from anywhere in the world", r"anywhere in the world",
        r"fully remote,? (?:from )?anywhere", r"remote,? worldwide",
        r"hire (?:from )?anywhere", r"no matter where you (?:are|live)",
        r"we are (?:a )?(?:fully )?(?:globally )?distributed (?:team|company)",
        r"anywhere on the globe", r"any country in the world",
    ]),
    ("contractor_text", "OPEN_CONTRACTOR", 0.75, [
        r"employer of record", r"\bEOR\b", r"\bvia Deel\b", r"through Deel",
        r"\bDeel\b", r"remote\.com", r"independent contractor",
        r"contractor agreement", r"b2b contract", r"\bumbrella company\b",
    ]),
    # Every one of these must mention a visa or a work permit. An earlier
    # version accepted "relocation support" on its own and immediately matched
    # Mistral's boilerplate -- "Benefits vary by country and may include
    # healthcare coverage, parental leave, relocation support" -- which promises
    # nothing to anyone. Relocation help is what a company gives an employee it
    # can already hire; sponsorship is what makes hiring him possible at all,
    # and only the second one is the claim this state is making.
    ("sponsorship_text", "ONSITE_SPONSORED", 0.8, [
        r"visa sponsorship (?:is )?(?:available|provided|offered)",
        r"(?:we|company) (?:will |can |do )?sponsor(?:s|ship)? (?:your |work )?"
        r"(?:visa|permit)",
        r"sponsor(?:ship)? (?:for )?(?:work )?(?:permits?|visas?)",
        r"visa (?:and|&|\+) relocation",
        r"relocation (?:package|assistance|support)[^.]{0,80}visa",
        r"visa[^.]{0,80}relocation (?:package|assistance|support)",
        r"work permit (?:is )?(?:provided|arranged|sponsored)",
        r"visas? sponsorship", r"sponsor(?:ing|s)? (?:work )?visas?",
        r"relocation packages? and visa",
    ]),
    ("blocked_text", "BLOCKED", 0.85, [
        r"must (?:be |reside |live )(?:located |based )?in the (?:US|United States|UK|EU)",
        r"must be (?:legally )?authoriz\w+ to work in the (?:US|United States|UK)",
        r"(?:US|U\.S\.|UK|EU)[- ]based (?:candidates|applicants) only",
        r"only (?:candidates|applicants) (?:based |located |residing )?in",
        r"no visa sponsorship", r"unable to sponsor", r"cannot sponsor",
        r"do not (?:offer|provide) (?:visa )?sponsorship",
        r"work authoriz\w+ in the (?:US|United States) is required",
        # Greenhouse's own footer boilerplate. The four sponsorship patterns
        # above all need "no", "unable", "cannot" or "do not" -- none of them
        # reach "The employer **will not** sponsor applicants for work visas",
        # which is the exact wording on a large share of US postings. It cost a
        # real one: Hungryroot #16211 reached the board at score 72.
        r"will not sponsor",
        # "You can work remotely anywhere in the US" reads as generous and is
        # a geo-lock. Deliberately not folded into EXCLUSIVE -- that list is
        # matched against the *location field*, and this is prose.
        # "...or abroad" turns the same phrase into an *open* job, and one
        # advert really does say "based anywhere in the United States or
        # abroad". Naming another country does not rescue it -- "the US or
        # Canada" is still two doors he has no key to.
        r"anywhere in the (?:US|U\.S\.A?\.?|United States)\b"
        r"(?!\s*(?:or|/|,)\s*(?:abroad|worldwide|internationally|globally|anywhere))",
        r"\d+\+?\s*U\.?S\.?\s*states",
    ]),
]

ONSITE_MARKERS = [r"\bon[- ]site\b", r"\bin[- ]office\b", r"\bhybrid\b",
                  r"\d+ days? (?:a|per) week in (?:the )?office",
                  # German, for the Arbeitnow feed.
                  r"\bvor Ort\b", r"\bim Büro\b", r"\bPräsenz\b"]

# German remote and visa vocabulary.
#
# Arbeitnow is EU-heavy and a third of its undecided jobs are written in German,
# where every English rule above is blind. Measured honestly: this recovers
# roughly twenty to forty jobs, not hundreds -- "weltweit" appears in 22 adverts
# and "ortsunabhängig" in one. Included because it is a short list and those are
# real jobs, not because it is a large win.
GERMAN_RULES = [
    ("worldwide_de", "OPEN_WORLDWIDE", 0.85, [
        r"\bweltweit\b", r"\bortsunabh(?:ä|ae)ngig\b",
        r"von (?:überall|ueberall)", r"\bwelt(?:weit)? remote\b",
        r"arbeite(?:n|st) von (?:überall|ueberall|zu Hause)",
    ]),
    ("sponsorship_de", "ONSITE_SPONSORED", 0.8, [
        r"\bVisum(?:sponsoring|unterst(?:ü|ue)tzung)?\b",
        r"\bArbeitserlaubnis\b", r"\bArbeitsvisum\b",
        r"\bUmzugs(?:hilfe|paket|unterst(?:ü|ue)tzung)\b",
        r"\bRelocation[- ]Paket\b", r"\bBlue Card\b",
    ]),
    ("blocked_de", "BLOCKED", 0.8, [
        r"Wohnsitz in Deutschland", r"in Deutschland ans(?:ä|ae)ssig",
        r"deutsche Arbeitserlaubnis erforderlich",
        r"nur (?:für )?Bewerber(?:innen)? (?:aus|in) Deutschland",
    ]),
]

# German negation, for the same reason NEGATION exists in English: every
# permissive phrase has a negated form that contains it.
NEGATION_DE = re.compile(r"\b(kein|keine|keinen|nicht|ohne|leider nicht)\b", re.I)

# Words a location field uses that are not places. Everything else in the field
# is a place, which is what the catch-all in from_location_field turns on.
GENERIC_LOCATION = (
    r"\b(remote|hybrid|on[- ]?site|in[- ]?office|office|offices|hq|headquarters|"
    r"full[- ]?time|part[- ]?time|contract|permanent|temporary|freelance|intern|"
    r"internship|flexible|various|multiple|locations?|based|optional|preferred|"
    r"or|and|any|other|others|position|role|work|"
    # Currency codes and pay words. A Hacker News header put "$100-140K USD"
    # where the location should have been, and "USD" is three letters, so the
    # catch-all read it as a place and blocked the job.
    r"usd|eur|gbp|cad|aud|chf|inr|sgd|sek|nok|dkk|pln|salary|equity|comp|"
    r"benefits|visa|sponsorship|junior|senior|staff|principal|lead|manager|"
    r"engineer|developer|designer|analyst)\b|[^\w\s]|\d")


def _find(haystack: str, needles: list[str]) -> str | None:
    """The matched phrase, so it can be quoted as evidence.

    Word-boundary matched, not substring. Plain `in` looked fine until
    "Malaysia" matched "asia" and a Kuala Lumpur role was declared open to South
    Asia. Multi-word needles keep their internal spacing flexible so "asia
    pacific" still matches "Asia  Pacific".
    """
    for needle in needles:
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in needle.split()) + r"\b"
        if re.search(pattern, haystack, re.I):
            return needle
    return None


NEGATION = re.compile(r"\b(no|not|cannot|can't|won't|unable|without|do not|"
                      r"does not|doesn't|are unable|is not)\b", re.I)

# A "worldwide" phrase only counts if the sentence is about employment. Replit's
# advert says its product lets builders "transact reliably anywhere in the
# world" -- marketing copy about payments, which the pattern read as a hiring
# policy and promoted to a sendable state.
WORK_CUE = re.compile(
    r"\b(work|works|working|hire|hiring|hired|employ|employee|employment|"
    r"candidate|applicant|apply|team ?mate|teammate|team member|staff|role|"
    r"position|job|located|location|based|reside|residing|live|living|"
    r"remote|relocat|timezone|time zone|onboard|"
    # German, or every German rule below is dead on arrival: the guard fires
    # before them and an advert written in German contains none of the English
    # words above. This cost the whole German rule set on its first run —
    # the patterns matched, the guard rejected the sentence, and the job went
    # to review as though nothing had been found.
    r"arbeit|mitarbeit|bewerb|kandidat|stelle|anstellung|besch(ä|ae)ftig|"
    r"team|wohn|leben|ansässig|ansaessig|standort|b(ü|ue)ro|einstell)\w*\b",
    re.I)


# The sentence a match sits in -- one definition for every stage.
_sentence = sentence_around


# ---------------------------------------------------------------- rules --

def from_location_field(location_raw: str, url: str) -> Decision | None:
    """The structured path: Himalayas' locationRestrictions, WWR's region,
    Remotive's candidate_required_location, Jobicy's jobGeo.

    These are the board's own answer, not an inference from prose, which is why
    they run first and carry the highest confidence.
    """
    if not location_raw or not location_raw.strip():
        return None
    field = location_raw.strip()
    quote = f"location field: {field}"

    hit = _find(field, SRI_LANKA)
    if hit:
        return Decision("ONSITE_LK", quote, url, "location:sri_lanka", 0.95)

    hit = _find(field, WORLDWIDE)
    if hit:
        return Decision("OPEN_WORLDWIDE", quote, url, "location:worldwide", 0.95)

    hit = _find(field, ASIA_INCLUSIVE)
    if hit:
        return Decision("OPEN_REGION", quote, url, f"location:asia[{hit}]", 0.85)

    hit = _find(field, AMBIGUOUS)
    if hit:
        # Sri Lanka is not in EMEA, and a lot of EMEA postings hire there
        # anyway. Both readings are wrong often enough to be dangerous.
        return Decision("UNKNOWN", quote, url, f"location:ambiguous[{hit}]", 0.5)

    hit = _find(field, EXCLUSIVE)
    if hit:
        return Decision("BLOCKED", quote, url, f"location:exclusive[{hit}]", 0.9)

    return None


def named_place(location_raw: str, url: str) -> Decision | None:
    """The catch-all, and it decides more jobs than the whole vocabulary above.

    A location field exists in order to restrict. If it names a place at all --
    "Dresden, Sachsen, Deutschland", "Remote, South Korea", "New York, NY (HQ);
    San Francisco, CA; Remote" -- and that place is not Sri Lanka, not Asia and
    not the whole world, the door is closed, whether or not the country appears
    in EXCLUSIVE. Enumerating every country and city on earth was the first
    approach and it left 789 obviously-restricted jobs in the review queue,
    whose entire job is to stay small enough to actually work through.

    It runs *after* the text rules, not before. An ATS location field is often
    the company's head office rather than a hiring restriction, so a posting
    that says "Berlin" in the field and "we hire from anywhere" in the body is
    open -- and running this first declared ninety-nine such jobs blocked.

    Confidence is lower than a named match, because this infers from the
    field's existence rather than from its content.
    """
    if not location_raw or not location_raw.strip():
        return None
    residue = re.sub(GENERIC_LOCATION, " ", location_raw, flags=re.I)
    if re.search(r"[a-z]{3}", residue, re.I):
        return Decision("BLOCKED", f"location field: {location_raw.strip()}",
                        url, "location:named_place", 0.7)
    return None


def from_text(description: str, url: str) -> Decision | None:
    """The prose path, for the sources with no location field worth the name."""
    if not description:
        return None
    text = description[:12000]

    for rule_name, state, confidence, patterns in TEXT_RULES + GERMAN_RULES:
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            sentence = _sentence(text, match)
            # "We do not offer visa sponsorship" contains "visa sponsorship".
            # Every permissive rule here has a negated form that reads almost
            # identically, so the sentence is checked rather than the phrase.
            if state != "BLOCKED" and (NEGATION.search(sentence)
                                       or NEGATION_DE.search(sentence)):
                continue
            # And the sentence has to be about hiring somebody, not about what
            # the product does.
            if state != "BLOCKED" and not WORK_CUE.search(sentence):
                continue
            return Decision(state, sentence, url, f"text:{rule_name}", confidence)

    hit = _find(text, SRI_LANKA)
    if hit:
        return Decision("ONSITE_LK", _sentence(text, hit), url,
                        "text:sri_lanka", 0.8)
    return None


def contradiction_check(decision: Decision, description: str) -> Decision:
    """A permissive verdict, checked against the body for a flat contradiction.

    We Work Remotely lets a poster tag a job "Anywhere in the World" and then
    write "you must be authorized to work in the US" three paragraphs down. The
    location field fires first and never sees the body, so the job arrives at
    the top of the list wearing a WORLDWIDE badge.

    Where the two disagree, neither is trusted. It goes to review -- the one
    place a contradiction can actually be settled -- carrying both quotes, so
    whoever reads it can see the disagreement rather than re-finding it.
    """
    if decision.state not in SENDABLE or not description:
        return decision

    for rule_name, state, _confidence, patterns in TEXT_RULES:
        if state != "BLOCKED":
            continue
        for pattern in patterns:
            match = re.search(pattern, description[:12000], re.I)
            if not match:
                continue
            counter = _sentence(description, match)
            return Decision(
                "UNKNOWN",
                f"CONTRADICTION — {decision.rule} says: {decision.quote[:130]} "
                f"|| but the advert says: {counter[:130]}",
                decision.url, f"contradiction[{decision.rule} vs {rule_name}]",
                0.4)
    return decision


def decide(location_raw: str, description: str, title: str, url: str,
           tags: list[str], location_trusted: bool = True) -> Decision:
    """Best case first. The first rule that fires with evidence wins.

    `location_trusted` is False for Hacker News, whose "location" is whatever
    landed third in a pipe-separated line the poster may not have followed --
    one of them was `$100-140K USD`. Blocking a job on a field that guessed
    wrong is the expensive mistake, because it removes the job silently, so HN
    falls through to the text rules and, failing those, to review.
    """
    combined_tags = " ".join(tags or []).lower()
    remote_ish = ("remote" in combined_tags or "remote" in (location_raw or "").lower()
                  or "remote" in (title or "").lower())

    # Precedence, best case first. The location field's *explicit* answers
    # outrank prose; prose outranks the mere fact that a place was named.
    for decision in (from_location_field(location_raw, url),
                     from_text(description, url),
                     named_place(location_raw, url) if location_trusted else None):
        if decision:
            return contradiction_check(decision, description)

    # Nothing said anything about where. An advert that is explicitly on-site
    # and silent on sponsorship is a closed door; one that is silent on
    # everything is genuinely unknown, and unknown is an answer.
    if description and not remote_ish:
        for pattern in ONSITE_MARKERS:
            match = re.search(pattern, description[:12000], re.I)
            if match:
                return Decision("ONSITE_NO_SPONSOR",
                                _sentence(description, match), url,
                                "text:onsite_no_sponsorship", 0.6)

    return Decision("UNKNOWN", "", "", "", 0.0)


# ------------------------------------------------------------------ apply --

def apply(conn, redo: bool = False) -> dict:
    started = now()
    where = "" if redo else " AND e.job_id IS NULL"
    rows = conn.execute(f"""
        SELECT j.id, j.title, jt.description, j.canonical_url,
               (SELECT p.location_raw FROM posting p WHERE p.job_id = j.id
                 AND p.location_raw <> '' ORDER BY length(p.location_raw) DESC
                 LIMIT 1) loc,
               (SELECT p.tags FROM posting p WHERE p.job_id = j.id LIMIT 1) tags,
               (SELECT GROUP_CONCAT(DISTINCT p.source) FROM posting p
                 WHERE p.job_id = j.id) srcs
        FROM job j
        LEFT JOIN job_text jt ON jt.job_id = j.id
        LEFT JOIN eligibility e ON e.job_id = j.id
        WHERE 1 = 1 {where}
    """).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        # Trusted unless Hacker News is the only place the location came from.
        trusted = (r["srcs"] or "") != "hackernews"
        d = decide(r["loc"] or "", r["description"] or "", r["title"] or "",
                   r["canonical_url"] or "", tags, location_trusted=trusted)

        # A sendable state with no URL cannot be stored, and should not be.
        # Falling back to UNKNOWN is the correct failure: it means a human
        # looks at it, rather than a sender acting on an unprovable claim.
        if d.state in SENDABLE and not (d.quote and d.url and d.rule):
            d = Decision("UNKNOWN", "", "", "", 0.0)

        conn.execute("""
            INSERT INTO eligibility (job_id, state, evidence_quote, evidence_url,
                                     rule, decided_by, confidence, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                state = excluded.state, evidence_quote = excluded.evidence_quote,
                evidence_url = excluded.evidence_url, rule = excluded.rule,
                decided_by = excluded.decided_by, confidence = excluded.confidence,
                decided_at = excluded.decided_at
            WHERE eligibility.decided_by <> 'human'
        """, (r["id"], d.state, d.quote or None, d.url or None, d.rule or None,
              d.decided_by, d.confidence, now()))
        counts[d.state] = counts.get(d.state, 0) + 1

    conn.commit()
    log_stage(conn, "eligibility", True, json.dumps(counts), started)
    return counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--breakdown", action="store_true")
    ap.add_argument("--review", type=int, metavar="N")
    ap.add_argument("--explain", type=int, metavar="JOB_ID")
    args = ap.parse_args(argv)

    conn = connect()

    if args.explain:
        r = conn.execute(
            "SELECT j.title, j.company, j.canonical_url, e.* FROM job j "
            "LEFT JOIN eligibility e ON e.job_id = j.id WHERE j.id = ?",
            (args.explain,)).fetchone()
        if not r:
            print(f"no job {args.explain}")
            return 1
        print(f"{r['company']} — {r['title']}\n{r['canonical_url']}\n")
        print(f"  state      {r['state']}")
        print(f"  rule       {r['rule']}")
        print(f"  confidence {r['confidence']}")
        print(f"  by         {r['decided_by']}")
        print(f"  evidence   {r['evidence_quote']}")
        return 0

    if args.review:
        rows = conn.execute("""
            SELECT j.id, j.company, j.title, j.canonical_url,
                   COALESCE(s.max_usd_month, s.min_usd_month) pay
            FROM job j JOIN eligibility e ON e.job_id = j.id
            LEFT JOIN salary s ON s.job_id = j.id
            WHERE e.state = 'UNKNOWN' AND j.status = 'open'
            ORDER BY pay DESC NULLS LAST, j.last_seen DESC LIMIT ?
        """, (args.review,)).fetchall()
        for r in rows:
            pay = f"${r['pay']:,.0f}/mo" if r["pay"] else "pay unknown"
            print(f"  #{r['id']:<6} {pay:<16} {r['company'][:22]:<22} {r['title'][:44]}")
        print(f"\n{len(rows)} needing a human. Each one is a job the rules could "
              f"not place — not a job that is unavailable.")
        return 0

    if args.breakdown:
        for r in conn.execute(
                "SELECT state, COUNT(*) n FROM eligibility GROUP BY state ORDER BY n DESC"):
            mark = "  <- sendable" if r["state"] in SENDABLE else ""
            print(f"  {r['state']:<20} {r['n']:>5}{mark}")
        print()
        for r in conn.execute(
                "SELECT rule, COUNT(*) n FROM eligibility WHERE rule IS NOT NULL "
                "GROUP BY rule ORDER BY n DESC LIMIT 15"):
            print(f"    {r['rule']:<36} {r['n']:>5}")
        return 0

    counts = apply(conn, redo=args.all)
    surface = set(settings()["eligibility"]["surface"])
    total = sum(counts.values())
    reachable = sum(n for s, n in counts.items() if s in surface)
    for state, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {state:<20} {n:>5}")
    print(f"\n{reachable} of {total} are worth surfacing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
