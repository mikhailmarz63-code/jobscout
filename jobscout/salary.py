#!/usr/bin/env python3
"""Stage 3: every salary shape the eight sources produce -> USD per month.

Three rules, and the whole module is built around them.

**1. A missing salary is never zero.** RemoteOK sends `salary_min: 0` for "not
stated". Read literally, that ranks a third of the feed at the bottom and hides
it. `0`, `None` and absent all mean the same thing here: unknown.

**2. Unknown is not a rejection.** The floor in settings.yml applies to salaries we
*know*. Jobs that publish no number keep their place and are ranked on their
other signals, because a large share of the best-paying roles publish nothing.
A filter that quietly drops them removes the top of the market.

**3. An ambiguous number is unknown, not a guess.** "5000" with no period is
either $5k/month or $5k/year -- a 12x difference on the one number the whole
filter turns on. Where the period cannot be established, the string is recorded
in `salary_unparsed` and the salary is marked unknown. Being wrong by 12x is far
worse than admitting ignorance, and the recorded string is how the ladder below
gets better.

    python3 salary.py               # parse everything unparsed
    python3 salary.py --all         # re-parse from scratch (after a rule change)
    python3 salary.py --unparsed    # what the ladder could not read
    python3 salary.py --fx          # refresh exchange rates
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, now                    # noqa: E402
from sources import get, settings              # noqa: E402

# Hours/days/weeks in an average month, from a 40-hour week and a 52-week year.
# Stated once so an hourly rate and an annual one land on the same scale.
PER_MONTH = {
    "hour": 40 * 52 / 12,     # 173.33
    "day": 260 / 12,          # 21.67
    "week": 52 / 12,          # 4.33
    "month": 1.0,
    "year": 1 / 12,
}

# Frankfurter is the ECB's published reference rates, free and keyless.
FX_API = "https://api.frankfurter.app/latest"
FX_MAX_AGE = 7 * 24 * 3600

# Used only when the rate table is empty and the network is down. A stale rate
# beats refusing to rank a job, and these are order-of-magnitude correct.
FX_FALLBACK = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "CHF": 1.12, "SGD": 0.74, "INR": 0.012, "JPY": 0.0064, "NZD": 0.61,
    "SEK": 0.094, "NOK": 0.093, "DKK": 0.145, "PLN": 0.25, "BRL": 0.18,
    "ZAR": 0.055, "AED": 0.27, "LKR": 0.0033,
}

SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY", "₨": "LKR"}
CODES = set(FX_FALLBACK) | {"USD", "EUR", "GBP"}

PERIOD_WORDS = [
    (r"per\s*hour|/\s*hour|/\s*hr\b|hourly|an\s+hour", "hour"),
    (r"per\s*day|/\s*day|daily|a\s+day", "day"),
    (r"per\s*week|/\s*week|weekly|a\s+week", "week"),
    (r"per\s*month|/\s*month|/\s*mo\b|monthly|a\s+month|pcm\b", "month"),
    (r"per\s*year|/\s*year|/\s*yr\b|yearly|annually|annual|per\s*annum|p\.?a\.?\b", "year"),
]

# A bare number this large is annual. Below it and with no stated period, the
# reading is genuinely ambiguous -- see rule 3.
ANNUAL_FLOOR = 15_000


@dataclass
class Salary:
    known: bool = False
    min_usd_month: float | None = None
    max_usd_month: float | None = None
    src_currency: str | None = None
    src_period: str | None = None
    src_min: float | None = None
    src_max: float | None = None
    raw: str = ""
    derived_by: str = ""
    confidence: float = 0.0


# ------------------------------------------------------------ conversion --

def fx_rates(conn, refresh: bool = False) -> dict[str, float]:
    """USD per unit of each currency. Refreshed weekly; falls back rather than
    failing, because a stale rate still ranks jobs correctly to within a few
    percent and a missing one ranks them not at all."""
    row = conn.execute("SELECT MAX(fetched_at) t FROM fx").fetchone()
    fresh = row and row["t"] and (now() - row["t"]) < FX_MAX_AGE

    if refresh or not fresh:
        data = get(FX_API, params={"from": "USD"}, cfg=settings())
        rates = (data or {}).get("rates") or {}
        if rates:
            stamp = now()
            conn.execute("INSERT OR REPLACE INTO fx VALUES ('USD', 1.0, ?)", (stamp,))
            for code, per_usd in rates.items():
                if per_usd:
                    conn.execute("INSERT OR REPLACE INTO fx VALUES (?, ?, ?)",
                                 (code, 1.0 / float(per_usd), stamp))
            conn.commit()

    table = {r["currency"]: r["usd_per"] for r in conn.execute("SELECT * FROM fx")}
    return table or dict(FX_FALLBACK)


def to_usd_month(amount: float | None, currency: str, period: str,
                 rates: dict) -> float | None:
    if amount is None:
        return None
    rate = rates.get((currency or "USD").upper())
    if rate is None:
        rate = FX_FALLBACK.get((currency or "USD").upper())
    if rate is None:
        return None
    return round(amount * rate * PER_MONTH[period], 2)


def _biggest(*values: float | None) -> float | None:
    """The largest converted figure, or None when nothing converted.

    `to_usd_month` returns None for a currency with no rate -- the ECB table
    Frankfurter publishes covers about thirty, and the sources hand us more
    than that (a single Jobicy posting quoted in Costa Rican colon, CRC, is
    what found this). Taking `max()` of the empty result raised ValueError,
    which killed the whole run: the loop below writes inside one transaction,
    so one unconvertible posting discarded every other row parsed that day.
    """
    converted = [v for v in values if v is not None]
    return max(converted) if converted else None


# ---------------------------------------------------------------- parsing --

def _clean_number(text: str) -> float | None:
    """'211.4K' -> 211400.0, '120,000' -> 120000.0

    There is deliberately **no `m` = million suffix.** It was in the first
    version and it was wrong in every case it fired: "6-12 M" is months,
    "2-3 m" is months, and "$270 m" came out of a company blurb about funding.
    No salary in any currency this system handles is written in millions, so
    supporting the suffix could only ever produce false positives.
    """
    t = text.strip().replace(" ", "")

    # A comma is a thousands separator only when exactly three digits follow it.
    # Much of Europe writes the decimal point as a comma, and "$31,2k" means
    # 31.2 thousand -- stripping the comma made it 312 thousand, which turned a
    # Remote Office Assistant into a $26,000-a-month job at the top of the list.
    t = re.sub(r",(\d{3})(?!\d)", r"\1", t)
    t = t.replace(",", ".")

    mult = 1.0
    if t and t[-1] in "kK":
        mult, t = 1_000.0, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def detect_period(text: str, tight: str | None = None) -> str | None:
    """`tight` is the narrow window immediately around the number.

    Sub-monthly periods are only believed from the tight window. A job advert
    routinely says "4-day week", "day one", "weekly demo" somewhere in the same
    paragraph as a salary, and reading those as the pay period turned a
    £70,000-£80,000 *annual* band into a daily rate, and €10k a month into €10k
    a week. Monthly and annual markers are safe from sixty characters out;
    hourly, daily and weekly are not.
    """
    low = text.lower()
    narrow = (tight or text).lower()
    for pattern, period in PERIOD_WORDS:
        window = narrow if period in ("hour", "day", "week") else low
        if re.search(pattern, window):
            return period
    return None


def _repair_range(lo: float | None, hi: float | None, matched: str
                  ) -> tuple[float | None, float | None]:
    """'$300-450K' means 300K to 450K, not 300 to 450,000.

    Writing the suffix once at the end of a range is the commonest way people
    write a salary band, and taking it literally understates the bottom of the
    band by a thousand times -- which then sails through every other check,
    because 450,000 a year is a perfectly plausible number.
    """
    if lo is None or hi is None or lo == 0:
        return lo, hi
    if re.search(r"[kK]", matched) and hi / lo > 100:
        return lo * 1000, hi
    return lo, hi


def detect_currency(text: str) -> tuple[str, float]:
    """(currency, confidence). '$' is ambiguous across USD/CAD/AUD/SGD, so it
    resolves to USD at reduced confidence rather than silently claiming
    certainty."""
    upper = text.upper()
    for code in sorted(CODES, key=len, reverse=True):
        if re.search(rf"\b{code}\b", upper):
            return code, 1.0
    for symbol, code in SYMBOLS.items():
        if symbol in text:
            return code, 0.8 if symbol == "$" else 1.0
    return "USD", 0.4


# A range, or a single figure, with optional symbols and a k suffix.
RANGE_RE = re.compile(
    r"([$£€₹¥]\s*)?(\d[\d,\.]*\s?[kK]?)\s*(?:-|–|—|to|\.\.)\s*([$£€₹¥]\s*)?(\d[\d,\.]*\s?[kK]?)")
SINGLE_RE = re.compile(r"([$£€₹¥]\s*)(\d[\d,\.]*\s?[kK]?)")

# How far either side of a number to look for the words that give it meaning.
# The first version searched the whole 4,000-character description, which is how
# "£70,000 - £80,000" came back as a *daily* rate: the phrase "per day" appeared
# two paragraphs away, about something else entirely.
CONTEXT = 60

# The narrow window. Hourly/daily/weekly markers must sit this close to the
# number to be believed -- see detect_period.
TIGHT = 20

# Words that make a number a salary rather than a headcount, a revenue figure or
# a funding round. Required when reading a description; not required when the
# source handed us a dedicated salary field.
CUE_RE = re.compile(
    r"salar|compensat|\bpay\b|\bpaid\b|\bbase\b|\bOTE\b|remunerat|\bwage|"
    r"\bper (?:hour|day|week|month|year|annum)\b|/(?:hr|hour|yr|year|mo|month)\b|"
    r"\bannual|\brate\b|\bband\b|\bpackage\b", re.I)

# Above this, it is a parse error rather than a very good job -- $200k/month is
# $2.4m a year. Below the floor, it is a match on something that is not money.
MAX_SANE_USD_MONTH = 200_000
MIN_SANE_USD_MONTH = 100


def from_structured(raw: dict, rates: dict) -> Salary | None:
    """The Himalayas/Jobicy/RemoteOK path: real fields, so no guessing."""
    lo, hi = raw.get("min"), raw.get("max")
    lo = float(lo) if isinstance(lo, (int, float)) and lo > 0 else None
    hi = float(hi) if isinstance(hi, (int, float)) and hi > 0 else None
    if lo is None and hi is None:
        return None            # rule 1: 0 and None are both "not stated"

    period = (raw.get("period") or "").lower().rstrip("ly")
    period = {"year": "year", "annual": "year", "yearly": "year", "month": "month",
              "week": "week", "day": "day", "hour": "hour",
              "": ""}.get(period, period)
    confidence = 1.0
    if period not in PER_MONTH:
        # No stated period. Only the unambiguous magnitude case is accepted.
        biggest = max(x for x in (lo, hi) if x is not None)
        if biggest >= ANNUAL_FLOOR:
            period, confidence = "year", 0.75
        else:
            return None        # rule 3
    currency = (raw.get("currency") or "USD").upper()
    min_usd = to_usd_month(lo, currency, period, rates)
    max_usd = to_usd_month(hi, currency, period, rates)

    # A structured field can be wrong too -- a board that stores an annual
    # figure in a field labelled monthly produces a number twelve times too
    # big, and it arrives looking authoritative.
    biggest = _biggest(min_usd, max_usd)
    if biggest is None:
        return None            # no rate for this currency -- unknown, not zero
    if biggest > MAX_SANE_USD_MONTH or biggest < MIN_SANE_USD_MONTH:
        return None

    return Salary(
        known=True, min_usd_month=min_usd, max_usd_month=max_usd,
        src_currency=currency, src_period=period, src_min=lo, src_max=hi,
        raw=json.dumps(raw), derived_by="structured", confidence=confidence,
    )


def _candidates(text: str):
    """Every number-or-range in the text, with two windows around it. Yields
    (lo, hi, matched_text, context, tight, is_range)."""
    def windows(match):
        return (text[max(0, match.start() - CONTEXT): match.end() + CONTEXT],
                text[max(0, match.start() - TIGHT): match.end() + TIGHT])

    seen_spans = []
    for match in RANGE_RE.finditer(text):
        seen_spans.append(match.span())
        lo, hi = _repair_range(_clean_number(match.group(2)),
                               _clean_number(match.group(4)), match.group(0))
        context, tight = windows(match)
        yield lo, hi, match.group(0), context, tight, True

    for match in SINGLE_RE.finditer(text):
        # Skip figures already consumed as half of a range.
        if any(s <= match.start() < e for s, e in seen_spans):
            continue
        value = _clean_number(match.group(2))
        context, tight = windows(match)
        yield value, value, match.group(0), context, tight, False


def from_text(text: str, rates: dict, require_cue: bool = False) -> Salary | None:
    """The Remotive/Ashby/HN path.

    Every judgment is made from the *context window around the number*, never
    from the document as a whole, and every candidate must survive four
    refusals before it is believed. It would far rather return nothing than a
    figure that is out by a factor of twelve -- an unknown salary costs a job
    its place in the pay ranking; a wrong one puts it at the top.
    """
    if not text:
        return None

    for lo, hi, matched, context, tight, is_range in _candidates(text[:6000]):
        if lo is None and hi is None:
            continue
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        # A range that starts at zero is a template nobody filled in.
        if lo == 0 and hi == 0:
            continue
        # A band 10x wide is not a band. "1083161-2682" -- an id, or a phone
        # number -- passed every other check and came out as $224-$90,263.
        if is_range and lo and hi and hi / lo > 10:
            continue
        # A lone "$1" or "$106" beside the word "pay" is a price, a fee or a
        # footnote. Only a figure with an hourly marker right next to it is
        # small enough to be a real rate.
        if not is_range and (hi or 0) < 500 and not re.search(
                r"/\s*h|per\s*hour|hourly|an\s+hour", tight, re.I):
            continue

        # 1. It has to look like money, in this window and not two paragraphs away.
        if require_cue and not CUE_RE.search(context):
            continue
        currency, cur_conf = detect_currency(matched)
        if cur_conf < 0.8 and require_cue:
            currency, cur_conf = detect_currency(context)
            if cur_conf < 0.8:
                continue          # no currency near it: not a salary

        confidence = 0.7 * cur_conf
        period = detect_period(context, tight)

        # 2. The period has to be established, or the magnitude unambiguous.
        if period is None:
            biggest = max(x for x in (lo, hi) if x is not None)
            if biggest >= ANNUAL_FLOOR:
                period, confidence = "year", confidence * 0.9
            elif biggest <= 500 and re.search(r"[$£€]", matched):
                period, confidence = "hour", confidence * 0.6
            else:
                continue          # rule 3: 500-15,000 with no period is ambiguous

        # 3. The number and the period have to agree with each other. "$1,750"
        # beside the word "annual" is not a salary of $1,750 a year -- it is a
        # fee, a bonus or a stipend that happened to sit near the word.
        if period == "year" and max(x for x in (lo, hi) if x is not None) < ANNUAL_FLOOR:
            continue

        min_usd = to_usd_month(lo, currency, period, rates)
        max_usd = to_usd_month(hi, currency, period, rates)

        # 4. And the answer has to be a plausible salary.
        biggest_usd = _biggest(min_usd, max_usd)
        if biggest_usd is None:
            continue           # no rate for this currency
        if biggest_usd > MAX_SANE_USD_MONTH or biggest_usd < MIN_SANE_USD_MONTH:
            continue

        return Salary(
            known=True, min_usd_month=min_usd, max_usd_month=max_usd,
            src_currency=currency, src_period=period, src_min=lo, src_max=hi,
            raw=matched.strip(), derived_by=f"text:{period}", confidence=confidence,
        )

    return None


def parse(salary_raw: dict, description: str, rates: dict) -> Salary:
    """Structured first, then the source's own salary string, then the
    description -- and the description is held to a higher bar, because most
    numbers in a job advert are not salaries."""
    structured = from_structured(salary_raw or {}, rates)
    if structured:
        return structured

    text_field = (salary_raw or {}).get("text")
    if text_field:
        parsed = from_text(text_field, rates, require_cue=False)
        if parsed:
            return parsed

    if description:
        parsed = from_text(description, rates, require_cue=True)
        if parsed:
            return parsed

    return Salary(known=False, raw=json.dumps(salary_raw or {})[:500],
                  derived_by="none", confidence=0.0)


# ------------------------------------------------------------------- apply --

def apply(conn, redo: bool = False) -> tuple[int, int, int, int]:
    rates = fx_rates(conn)
    where = "" if redo else " AND s.job_id IS NULL"
    rows = conn.execute(f"""
        SELECT j.id, jt.description,
               (SELECT p.raw FROM posting p WHERE p.job_id = j.id
                 ORDER BY (p.raw LIKE '%"min":%') DESC, length(p.raw) DESC LIMIT 1) praw,
               (SELECT p.source FROM posting p WHERE p.job_id = j.id LIMIT 1) src
        FROM job j LEFT JOIN salary s ON s.job_id = j.id
        LEFT JOIN job_text jt ON jt.job_id = j.id
        WHERE 1 = 1 {where}
    """).fetchall()

    known = unknown = rejected = failed = 0
    for r in rows:
        try:
            salary_raw = json.loads(r["praw"] or "{}").get("salary_raw") or {}
        except json.JSONDecodeError:
            salary_raw = {}
        try:
            result = parse(salary_raw, r["description"] or "", rates)
        except Exception as exc:
            # One posting must never cost a whole run. Everything here is
            # written in a single transaction, so an exception escaping this
            # loop rolls back every row parsed before it -- which is exactly
            # how one Costa-Rican-colon posting stalled the stage for days.
            # Recorded and printed, never swallowed.
            failed += 1
            print(f"salary.py: job {r['id']} ({r['src']}) raised "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            conn.execute(
                "INSERT OR IGNORE INTO salary_unparsed (job_id, raw, source, seen_at) "
                "VALUES (?, ?, ?, ?)",
                (r["id"], f"PARSE ERROR {type(exc).__name__}: {exc} "
                          f"-- {json.dumps(salary_raw)[:200]}", r["src"], now()))
            continue

        try:
            conn.execute("""
                INSERT INTO salary (job_id, known, min_usd_month, max_usd_month,
                                    src_currency, src_period, src_min, src_max, raw,
                                    derived_by, confidence, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (job_id) DO UPDATE SET
                    known = excluded.known, min_usd_month = excluded.min_usd_month,
                    max_usd_month = excluded.max_usd_month,
                    src_currency = excluded.src_currency, src_period = excluded.src_period,
                    src_min = excluded.src_min, src_max = excluded.src_max,
                    raw = excluded.raw, derived_by = excluded.derived_by,
                    confidence = excluded.confidence, decided_at = excluded.decided_at
            """, (r["id"], 1 if result.known else 0, result.min_usd_month,
                  result.max_usd_month, result.src_currency, result.src_period,
                  result.src_min, result.src_max, result.raw, result.derived_by,
                  result.confidence, now()))
        except sqlite3.IntegrityError:
            # The parser was supposed to have refused this. The constraint is
            # the backstop for a rule nobody thought of -- record what got
            # through, keep the run alive, and fix the ladder from the evidence.
            rejected += 1
            conn.execute(
                "INSERT OR IGNORE INTO salary_unparsed (job_id, raw, source, seen_at) "
                "VALUES (?, ?, ?, ?)",
                (r["id"], f"REJECTED BY CHECK: {result.raw[:200]} "
                          f"-> {result.min_usd_month}..{result.max_usd_month} "
                          f"({result.derived_by})", r["src"], now()))
            continue

        if result.known:
            known += 1
        else:
            unknown += 1
            # Only record a string that looked like it held a salary. Logging
            # every description would make the table useless for its purpose.
            text = (salary_raw.get("text") or "")[:300]
            if not text and (salary_raw.get("min") or salary_raw.get("max")):
                # The source stated a figure and the ladder still could not
                # read it -- the commonest cause is a currency with no rate.
                text = json.dumps(salary_raw)[:300]
            if text and re.search(r"[$£€₹]|\d{4,}|\bsalary\b", text, re.I):
                conn.execute(
                    "INSERT OR IGNORE INTO salary_unparsed (job_id, raw, source, seen_at) "
                    "VALUES (?, ?, ?, ?)", (r["id"], text, r["src"], now()))

    conn.commit()
    return known, unknown, rejected, failed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="re-parse everything")
    ap.add_argument("--unparsed", action="store_true")
    ap.add_argument("--fx", action="store_true")
    args = ap.parse_args(argv)

    conn = connect()

    if args.fx:
        rates = fx_rates(conn, refresh=True)
        print(f"{len(rates)} rates. GBP={rates.get('GBP')} EUR={rates.get('EUR')} "
              f"LKR={rates.get('LKR')}")
        return 0

    if args.unparsed:
        rows = conn.execute(
            "SELECT raw, source, COUNT(*) n FROM salary_unparsed "
            "GROUP BY raw ORDER BY n DESC LIMIT 40").fetchall()
        for r in rows:
            print(f"  {r['n']:>3}  {r['source']:<12} {r['raw'][:90]}")
        print(f"\n{len(rows)} distinct unreadable strings — each one is a rule "
              f"the ladder is missing.")
        return 0

    known, unknown, rejected, failed = apply(conn, redo=args.all)
    floor = settings()["pay"]["floor_usd_month"]
    above = conn.execute(
        "SELECT COUNT(*) c FROM salary WHERE known = 1 AND "
        "COALESCE(max_usd_month, min_usd_month) >= ?", (floor,)).fetchone()["c"]
    print(f"{known} with a salary, {unknown} without."
          + (f" {rejected} refused by the CHECK -- see --unparsed." if rejected else "")
          + (f" {failed} raised -- see --unparsed." if failed else ""))
    print(f"{above} of the known ones clear the ${floor:,}/month floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
