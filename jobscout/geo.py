#!/usr/bin/env python3
"""Stage 5: where the job is, and what that does to his day.

Colombo is **UTC+5:30**, which is the single most under-considered fact in
remote hiring from Sri Lanka. A New York company's 9-to-6 is 18:30 to 03:30 in
Colombo -- *zero* overlap with any sane working day. That job can pay
beautifully and still be a bad job, and no salary column will ever tell you so.

So the overlap is computed, banded, scored and drawn:

| band | overlap with a normal Colombo day |
|---|---|
| green | 4 hours or more |
| amber | 2 to 4 hours -- an early start or a late finish |
| red | under 2 hours -- a night shift with a day job's title |

Country resolution runs off `world.json` plus the synonym and US-state tables
below. Where it cannot tell, it records nothing rather than guessing: a job
pinned to the wrong country is worse on the map than a job with no pin, because
the wrong pin is invisible as an error.

    python3 geo.py               # resolve everything unresolved
    python3 geo.py --all         # re-resolve
    python3 geo.py --bands       # the overlap picture, by band and country
    python3 geo.py --unplaced 20 # what could not be placed, and what it said
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import cities                             # noqa: E402
from db import connect, log_stage, now      # noqa: E402
from sources import settings                # noqa: E402
from world import OUT as WORLD_JSON         # noqa: E402

# Names Natural Earth does not use but job adverts do.
SYNONYMS = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "america": "US", "stateside": "US", "us": "US",
    "uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "united kingdom": "GB",
    "holland": "NL", "the netherlands": "NL", "netherlands": "NL",
    "deutschland": "DE", "germany": "DE", "czechia": "CZ", "czech republic": "CZ",
    "south korea": "KR", "korea, republic of": "KR", "republic of korea": "KR",
    "north korea": "KP", "uae": "AE", "united arab emirates": "AE",
    "emirates": "AE", "russia": "RU", "russian federation": "RU",
    "vietnam": "VN", "viet nam": "VN", "ivory coast": "CI",
    "turkiye": "TR", "türkiye": "TR", "burma": "MM", "swaziland": "SZ",
    "macedonia": "MK", "north macedonia": "MK", "bosnia": "BA",
    "republic of ireland": "IE", "eire": "IE", "roi": "IE",
    "hong kong": "HK", "taiwan": "TW", "philippines": "PH",
    "sri lanka": "LK", "srilanka": "LK", "ceylon": "LK",
    "new zealand": "NZ", "aotearoa": "NZ", "south africa": "ZA",
    "brasil": "BR", "brazil": "BR", "mexico": "MX", "méxico": "MX",
    "espana": "ES", "españa": "ES", "spain": "ES",
}

# A US location field almost never says "United States" -- it says "Austin, TX"
# or "NYC". These two tables are what turn that into a country.
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
CITY_HINTS = {
    "new york": "US", "nyc": "US", "san francisco": "US", "sf bay": "US",
    "bay area": "US", "los angeles": "US", "seattle": "US", "austin": "US",
    "boston": "US", "chicago": "US", "denver": "US", "atlanta": "US",
    "miami": "US", "portland": "US", "san diego": "US", "washington dc": "US",
    "toronto": "CA", "vancouver": "CA", "montreal": "CA", "ottawa": "CA",
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "bristol": "GB",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE",
    "cologne": "DE", "köln": "DE", "frankfurt": "DE", "dresden": "DE",
    "stuttgart": "DE", "leipzig": "DE",
    "paris": "FR", "lyon": "FR", "amsterdam": "NL", "rotterdam": "NL",
    "utrecht": "NL", "brussels": "BE", "antwerp": "BE", "zurich": "CH",
    "zürich": "CH", "geneva": "CH", "vienna": "AT", "wien": "AT",
    "madrid": "ES", "barcelona": "ES", "valencia": "ES", "lisbon": "PT",
    "porto": "PT", "milan": "IT", "rome": "IT", "dublin": "IE",
    "warsaw": "PL", "krakow": "PL", "kraków": "PL", "wrocław": "PL",
    "prague": "CZ", "budapest": "HU", "bucharest": "RO", "sofia": "BG",
    "athens": "GR", "stockholm": "SE", "gothenburg": "SE", "oslo": "NO",
    "copenhagen": "DK", "helsinki": "FI", "tallinn": "EE", "riga": "LV",
    "vilnius": "LT", "kyiv": "UA", "kiev": "UA", "istanbul": "TR",
    "bangalore": "IN", "bengaluru": "IN", "mumbai": "IN", "delhi": "IN",
    "hyderabad": "IN", "chennai": "IN", "pune": "IN", "gurgaon": "IN",
    "colombo": "LK", "karachi": "PK", "lahore": "PK", "dhaka": "BD",
    "singapore": "SG", "kuala lumpur": "MY", "jakarta": "ID", "manila": "PH",
    "bangkok": "TH", "ho chi minh": "VN", "hanoi": "VN", "tokyo": "JP",
    "osaka": "JP", "seoul": "KR", "shanghai": "CN", "beijing": "CN",
    "shenzhen": "CN", "taipei": "TW", "dubai": "AE", "abu dhabi": "AE",
    "doha": "QA", "riyadh": "SA", "tel aviv": "IL", "cairo": "EG",
    "nairobi": "KE", "lagos": "NG", "cape town": "ZA", "johannesburg": "ZA",
    "sydney": "AU", "melbourne": "AU", "brisbane": "AU", "perth": "AU",
    "auckland": "NZ", "wellington": "NZ",
    "sao paulo": "BR", "são paulo": "BR", "rio de janeiro": "BR",
    "buenos aires": "AR", "santiago": "CL", "bogota": "CO", "bogotá": "CO",
    "lima": "PE", "mexico city": "MX", "guadalajara": "MX",
}

_world_cache: dict | None = None


def world() -> dict:
    global _world_cache
    if _world_cache is None:
        if not WORLD_JSON.exists():
            raise SystemExit("world.json missing — run `python3 world.py` first")
        _world_cache = json.loads(WORLD_JSON.read_text())
    return _world_cache


def _name_index() -> dict[str, str]:
    index = {}
    for iso, c in world()["countries"].items():
        index[c["name"].lower()] = iso
        index[iso.lower()] = iso
    index.update(SYNONYMS)
    index.update(CITY_HINTS)
    return index


# "Anywhere in the World" is not a place, and it has to be caught before
# anything else looks at it -- see SAFE_CODES below for why it mattered.
NOWHERE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|globally|remote only|"
    r"location independent|any country|distributed)\b", re.I)

# The only bare codes accepted, and the reason the rest were removed.
#
# The first version accepted any two-letter token matching an ISO code. "IN" is
# India, and "Anywhere in the World" contains the word "in" -- so eighty-two
# work-from-anywhere jobs were pinned to India, given a +5:30 offset and a green
# band they had not earned. `IT`, `IS`, `AT`, `AS`, `BE`, `NO`, `OR`, `SO`,
# `TO`, `ME`, `AM`, `AN`, `ID`, `LA`, `PA` and `DE` fail identically.
#
# These four survive because no English sentence uses them as words, and a
# location field that says "US" means the country.
SAFE_CODES = {"us": "US", "usa": "US", "uk": "GB", "uae": "AE"}


def resolve_country(text: str) -> tuple[str | None, str]:
    """(ISO-2, how). Longest name first, so "South Korea" is not shadowed by
    "Korea" and "United States" is not shadowed by "US"."""
    if not text or not text.strip():
        return None, "empty"
    low = text.lower()

    # A worldwide marker means the field is answering a different question.
    # Reading it for a country can only produce a wrong one.
    if NOWHERE.search(low):
        return None, "nowhere"

    index = _name_index()
    for name in sorted(index, key=len, reverse=True):
        if len(name) <= 3:
            continue          # codes are handled below, with tighter matching
        if re.search(rf"\b{re.escape(name)}\b", low):
            return index[name], f"name[{name}]"

    for token, iso in SAFE_CODES.items():
        if re.search(rf"\b{token}\b", low):
            return iso, f"code[{token}]"

    # "Austin, TX" -- a bare state abbreviation, but only after a comma, which
    # is what distinguishes the state OR from the word "or".
    match = re.search(r",\s*([a-z]{2})\b", low)
    if match and match.group(1) in US_STATES:
        return "US", f"us_state[{match.group(1)}]"

    return None, "unmatched"


# ------------------------------------------------------------- the clock --

def _windows(start: float, end: float) -> list[tuple[float, float]]:
    """A working day as UTC intervals, split where it crosses midnight."""
    start, end = start % 24, end % 24
    return [(start, end)] if start <= end else [(start, 24.0), (0.0, end)]


def overlap_hours(theirs: float, mine: float, day_start: float,
                  day_end: float) -> float:
    """Hours their working day shares with his, both expressed in UTC.

    Both offsets can be fractional -- Colombo is +5.5, Kathmandu +5.75, Chatham
    Island +12.75 -- so this stays in floats throughout. Rounding offsets to
    whole hours would quietly mislabel every South Asian job by half an hour,
    which is the difference between a 4.0 and a 3.5 hour overlap and therefore
    between a green band and an amber one.
    """
    mine_utc = _windows(day_start - mine, day_end - mine)
    theirs_utc = _windows(day_start - theirs, day_end - theirs)
    total = 0.0
    for a_start, a_end in mine_utc:
        for b_start, b_end in theirs_utc:
            total += max(0.0, min(a_end, b_end) - max(a_start, b_start))
    return round(total, 2)


def band(hours: float, green: float, amber: float) -> str:
    if hours >= green:
        return "green"
    return "amber" if hours >= amber else "red"


# ------------------------------------------------------------------ apply --

def apply(conn, redo: bool = False) -> dict:
    started = now()
    cfg = settings()
    mine = float(cfg["candidate"]["utc_offset"])
    day_start = float(cfg["hours"]["workday_start"])
    day_end = float(cfg["hours"]["workday_end"])
    green = float(cfg["hours"]["green_min_overlap"])
    amber = float(cfg["hours"]["amber_min_overlap"])
    countries = world()["countries"]

    where = "" if redo else " AND g.job_id IS NULL"
    rows = conn.execute(f"""
        SELECT j.id, j.company_slug, j.title,
               (SELECT p.location_raw FROM posting p WHERE p.job_id = j.id
                 AND p.location_raw <> '' ORDER BY length(p.location_raw) DESC
                 LIMIT 1) loc,
               e.state, e.evidence_quote,
               (SELECT GROUP_CONCAT(DISTINCT p.timezones) FROM posting p
                 WHERE p.job_id = j.id AND p.timezones NOT IN ('[]','')) tzs
        FROM job j
        LEFT JOIN eligibility e ON e.job_id = j.id
        LEFT JOIN geo g ON g.job_id = j.id
        WHERE 1 = 1 {where}
    """).fetchall()

    stats = {"placed": 0, "unplaced": 0, "from_timezones": 0,
             "green": 0, "amber": 0, "red": 0, "city_precise": 0}

    for r in rows:
        # A city, when the advert names one, beats the country every time.
        # "Austin, TX" is America/Chicago; the country table would have called
        # it America/New_York and been an hour out, and "San Francisco" would
        # have been three. Three hours is the width of a whole overlap band.
        # A field that says "anywhere" is answering a different question, and
        # reading it for a city produced exactly one answer: Puerto Rico, from
        # "Anywhere in the World; 🇦🇪 United Arab Emirates".
        loc_text = r["loc"] or ""
        city = (None if NOWHERE.search(loc_text)
                else cities.resolve(conn, loc_text, avoid=country_names()))
        if city:
            iso, how = city["country"], f"city[{city['name']}]"
        else:
            iso, how = resolve_country(r["loc"] or "")
            if not iso:
                # Nothing in the location field. The evidence quote sometimes
                # names a place the field did not.
                iso, how = resolve_country(r["evidence_quote"] or "")

        # Last resort, and clearly labelled as one.
        inferred_from = 0
        if not iso:
            iso, inferred_from = company_country(conn, r["company_slug"], r["id"])
            if iso:
                how = f"company[{inferred_from} other postings]"

        country = countries.get(iso) if iso else None
        offset = (city_offset(city) if city else None)
        if offset is None:
            offset = country["utc_offset"] if country else None

        # Himalayas publishes the required UTC offsets outright. That beats any
        # inference from a company's address -- it is the hours the job asks
        # for, not the hours the head office happens to keep.
        declared = _declared_offset(r["tzs"])
        if declared is not None:
            offset, how = declared, (how + "+declared_tz" if iso else "declared_tz")
            stats["from_timezones"] += 1

        if offset is None:
            hours = b = None
            stats["unplaced"] += 1
        else:
            hours = overlap_hours(offset, mine, day_start, day_end)
            b = band(hours, green, amber)
            stats[b] += 1
            stats["placed"] += 1

        # One write, and it always sets **every** column.
        #
        # There were two upserts here, and the unplaced one updated only
        # `decided_at`. That made `--all` a lie: eighty-two jobs wrongly pinned
        # to India kept their country, their +5:30 offset and their green band
        # through every re-run after the resolver was fixed, because the fixed
        # resolver returned None and None took the branch that changed nothing.
        # A re-decide has to be able to erase, or it is not a re-decide.
        conn.execute("""
            INSERT INTO geo (job_id, country, country_name, region, lat, lon,
                             precision, utc_offset, overlap_hours, band, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                country = excluded.country, country_name = excluded.country_name,
                region = excluded.region, lat = excluded.lat, lon = excluded.lon,
                precision = excluded.precision, utc_offset = excluded.utc_offset,
                overlap_hours = excluded.overlap_hours, band = excluded.band,
                decided_at = excluded.decided_at
        """, (r["id"], iso,
              (f"{city['name']}, {country['name']}" if city and country
               else city["name"] if city
               else country["name"] if country else None),
              country["region"] if country else None,
              city["lat"] if city else (country["lat"] if country else None),
              city["lon"] if city else (country["lon"] if country else None),
              "city" if city else ("company" if inferred_from
                                  else "country" if country else "unknown"),
              offset, hours, b, now()))

    conn.commit()
    log_stage(conn, "geo", True, json.dumps(stats), started)
    return stats


_country_names: set[str] | None = None


def company_country(conn, company_slug: str, exclude_job: int) -> tuple[str | None, int]:
    """(iso, how_many_agree) from the same employer's other postings.

    Three hundred jobs carry no usable location text at all -- an ATS entry with
    an empty field, a Hacker News comment that never says where. But the company
    is known, and if six of GitLab's other roles resolve to one country, the
    seventh is not a mystery.

    Only used where nothing else worked, recorded as `precision = 'company'` so
    it is never mistaken for the advert having said so, and only believed when
    the company's other postings actually agree with each other.
    """
    rows = conn.execute("""
        SELECT g.country, COUNT(*) n FROM geo g
        JOIN job j2 ON j2.id = g.job_id
        WHERE j2.company_slug = ? AND j2.id <> ? AND g.country IS NOT NULL
        GROUP BY g.country ORDER BY n DESC""", (company_slug, exclude_job)).fetchall()
    if not rows:
        return None, 0
    total = sum(r["n"] for r in rows)
    top = rows[0]
    # A company that posts across five countries tells us nothing about the
    # sixth. Two thirds agreement, and at least two postings, or nothing.
    if top["n"] < 2 or top["n"] / total < 0.66:
        return None, 0
    return top["country"], top["n"]


def country_names() -> set[str]:
    """Every country name and synonym, folded the way cities.py folds. Handed to
    cities.resolve so a country can never lose to a village of the same name."""
    global _country_names
    if _country_names is None:
        names = {cities.normalise(c["name"]) for c in world()["countries"].values()}
        names |= {cities.normalise(k) for k in SYNONYMS}
        _country_names = {n for n in names if n}
    return _country_names


def city_offset(city: dict | None) -> float | None:
    """The city's own IANA zone, read through the OS tzdata so daylight saving
    is right today rather than right when the dump was built."""
    if not city or not city.get("timezone"):
        return None
    from world import utc_offset
    return utc_offset(city["timezone"])


def _declared_offset(raw: str | None) -> float | None:
    """Himalayas' `timezoneRestrictions` is a list of acceptable UTC offsets.
    The one closest to Colombo is the one he would actually work, so that is
    the one scored -- taking the mean would invent an hour nobody offered."""
    if not raw:
        return None
    offsets: list[float] = []
    for chunk in raw.split("]["):
        try:
            offsets.extend(float(x) for x in
                           json.loads(chunk if chunk.startswith("[")
                                      else "[" + chunk.rstrip("]") + "]"))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    if not offsets:
        return None
    mine = float(settings()["candidate"]["utc_offset"])
    return min(offsets, key=lambda o: abs(o - mine))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--bands", action="store_true")
    ap.add_argument("--unplaced", type=int, metavar="N")
    args = ap.parse_args(argv)

    conn = connect()

    if args.bands:
        cfg = settings()
        print(f"\nColombo works {cfg['hours']['workday_start']}:00-"
              f"{cfg['hours']['workday_end']}:00 at UTC"
              f"{cfg['candidate']['utc_offset']:+}\n")
        for r in conn.execute("""
                SELECT g.band, COUNT(*) n, ROUND(AVG(g.overlap_hours), 1) avg
                FROM geo g JOIN eligibility e ON e.job_id = g.job_id
                WHERE g.band IS NOT NULL AND e.state NOT IN ('BLOCKED','ONSITE_NO_SPONSOR')
                GROUP BY g.band ORDER BY avg DESC"""):
            print(f"  {r['band']:<7} {r['n']:>5} jobs   {r['avg']}h average overlap")
        print("\n  by country (jobs he can take):")
        for r in conn.execute("""
                SELECT g.country_name c, g.utc_offset o, g.overlap_hours h,
                       g.band, COUNT(*) n
                FROM geo g JOIN eligibility e ON e.job_id = g.job_id
                WHERE g.band IS NOT NULL
                  AND e.state NOT IN ('BLOCKED','ONSITE_NO_SPONSOR')
                GROUP BY g.country ORDER BY n DESC LIMIT 15"""):
            print(f"    {r['c'] or '?':<24} UTC{r['o']:+5.1f}  "
                  f"{r['h']:>4}h  {r['band']:<6} {r['n']:>4} jobs")
        return 0

    if args.unplaced:
        for r in conn.execute("""
                SELECT j.company, j.title,
                       (SELECT p.location_raw FROM posting p WHERE p.job_id = j.id
                         ORDER BY length(p.location_raw) DESC LIMIT 1) loc
                FROM job j JOIN geo g ON g.job_id = j.id
                WHERE g.utc_offset IS NULL LIMIT ?""", (args.unplaced,)):
            print(f"  loc={str(r['loc'])[:40]:<40} {r['company'][:20]:<20} "
                  f"{r['title'][:36]}")
        return 0

    stats = apply(conn, redo=args.all)
    print(f"{stats['placed']} placed, {stats['unplaced']} could not be placed "
          f"({stats['from_timezones']} from a declared timezone)")
    print(f"  green {stats['green']}   amber {stats['amber']}   red {stats['red']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
