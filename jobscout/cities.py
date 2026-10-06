#!/usr/bin/env python3
"""City-accurate geography, from GeoNames, held locally.

`geo.py` resolved a job to its *country* and used that country's principal
timezone. For most of the world that is fine. For the countries that actually
advertise the most remote work it is wrong in the one way that matters:

    Austin, TX      -> America/Chicago   UTC-5, not America/New_York's -4
    Denver          -> America/Denver    UTC-6
    San Francisco   -> America/Los_Angeles UTC-7, three hours off New York
    Perth           -> Australia/Perth   UTC+8, not Sydney's +10

A three-hour error in the offset is a three-hour error in the overlap with a
Colombo working day, which is the difference between a green band and a red one
-- and the timezone band is 20 of the 130 scoring points.

GeoNames publishes the IANA timezone **per city**, which removes the guess
entirely. The dump is loaded once into the same SQLite file and matched locally
afterwards, so there is no per-lookup network call, no rate limit, and no third
party learning which cities the candidate is looking at. Job searches are
private; this keeps them that way.

Licence: GeoNames is CC BY 4.0. Attribution belongs in anything published.

    python3 cities.py --load        # download and load, ~25k cities
    python3 cities.py --check       # spot-check the ones that matter
    python3 cities.py "Austin, TX"  # resolve one string
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import unicodedata
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now      # noqa: E402
from sources import settings                # noqa: E402

# cities15000 is every city above 15,000 people -- about 25,000 of them, 1.5 MB
# zipped. cities5000 exists and triples the count for a handful of extra
# matches; the bigger file is not worth the load time when a smaller town in a
# job advert is nearly always accompanied by its country anyway.
DUMP_URL = "https://download.geonames.org/export/dump/cities15000.zip"
DUMP_MEMBER = "cities15000.txt"

# GeoNames column order, documented at
# https://download.geonames.org/export/dump/readme.txt
COL_NAME, COL_ASCII, COL_ALT = 1, 2, 3
COL_LAT, COL_LON = 4, 5
COL_CC = 8
COL_ADMIN1 = 10
COL_POP = 14
COL_TZ = 17

# Words that appear beside a city in a location field and are not part of its
# name. Stripped before matching so "Remote — Austin, TX (HQ)" still resolves.
NOISE = re.compile(
    r"\b(remote|hybrid|on[- ]?site|onsite|in[- ]?office|office|offices|hq|"
    r"headquarters|full[- ]?time|part[- ]?time|contract|permanent|flexible|"
    r"various|multiple|locations?|based|preferred|optional|anywhere|worldwide|"
    r"global|metro|area|region|greater|downtown)\b", re.I)

# US state and Canadian province abbreviations, so "Austin, TX" can be
# disambiguated from any other Austin.
ADMIN1_HINTS = {
    "al": "AL", "ak": "AK", "az": "AZ", "ar": "AR", "ca": "CA", "co": "CO",
    "ct": "CT", "de": "DE", "fl": "FL", "ga": "GA", "hi": "HI", "id": "ID",
    "il": "IL", "in": "IN", "ia": "IA", "ks": "KS", "ky": "KY", "la": "LA",
    "me": "ME", "md": "MD", "ma": "MA", "mi": "MI", "mn": "MN", "ms": "MS",
    "mo": "MO", "mt": "MT", "ne": "NE", "nv": "NV", "nh": "NH", "nj": "NJ",
    "nm": "NM", "ny": "NY", "nc": "NC", "nd": "ND", "oh": "OH", "ok": "OK",
    "or": "OR", "pa": "PA", "ri": "RI", "sc": "SC", "sd": "SD", "tn": "TN",
    "tx": "TX", "ut": "UT", "vt": "VT", "va": "VA", "wa": "WA", "wv": "WV",
    "wi": "WI", "wy": "WY", "dc": "DC",
    "on": "ON", "qc": "QC", "bc": "BC", "ab": "AB", "mb": "MB", "sk": "SK",
    "ns": "NS", "nb": "NB", "nl": "NL", "pe": "PE",
}

# The `city` table is declared in schema.sql, which db.connect() applies on
# every connection -- see the note there for why it lives with the rest.


def normalise(name: str) -> str:
    """Fold accents and punctuation so "Zürich", "Zurich" and "ZURICH" match.

    NFKD then dropping combining marks is the whole trick -- München becomes
    Munchen, São Paulo becomes Sao Paulo. Job adverts spell these both ways and
    the alternate-names column does not always carry the ASCII form.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    spaced = re.sub(r"[^a-z0-9]+", " ", stripped.lower())
    # Collapse the runs. "St. John's" turns into "st  john s" with a doubled
    # space otherwise -- because the dot and the space beside it each become
    # one -- and would then never match a query spelled "St Johns".
    return re.sub(r"\s+", " ", spaced).strip()


def load(conn) -> dict:
    """Download the dump and load it. Re-runnable: the table is rebuilt."""
    import requests

    started = now()
    cfg = settings()
    print(f"fetching {DUMP_URL} …")
    response = requests.get(
        DUMP_URL, timeout=180,
        headers={"User-Agent": cfg.get("http", {}).get("user_agent", "jobscout/0.1")})
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        raw = archive.read(DUMP_MEMBER).decode("utf-8")

    conn.execute("DELETE FROM city")

    rows, alt_rows = [], []
    reader = csv.reader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE)
    for parts in reader:
        if len(parts) <= COL_TZ or not parts[COL_TZ]:
            continue
        try:
            lat, lon = float(parts[COL_LAT]), float(parts[COL_LON])
            population = int(parts[COL_POP] or 0)
        except ValueError:
            continue

        name, country = parts[COL_NAME], parts[COL_CC]
        rows.append((None, name, normalise(name), country, parts[COL_ADMIN1],
                     lat, lon, parts[COL_TZ], population))

        # A few alternate spellings per city -- "Munich" for München, "Kyiv"
        # for Kiev. Capped, because the full list runs to dozens per city in
        # every script on earth and would triple the table for no extra matches.
        #
        # The ASCII form is always indexed first: GeoNames names Munich
        # "Munich", and the German "München" lives only in alternatenames. An
        # earlier version skipped every non-ASCII alias to keep Chinese and
        # Cyrillic out, and lost "München" with them -- normalise() folds the
        # umlaut anyway, so the right filter is on the *folded* result having
        # Latin letters, not on the original being ASCII.
        # Scan the whole list and keep every Latin-folding spelling, rather
        # than stopping after the first few. GeoNames orders alternatenames by
        # nothing in particular, and capping at four dropped "München" — which
        # sits well down Munich's list, behind Czech, Italian and Polish forms.
        # The cap traded a correct match for a table that is a few thousand
        # rows smaller, which is the wrong way round.
        seen = {normalise(name)}
        alternates = [parts[COL_ASCII]] + (parts[COL_ALT] or "").split(",")
        for alt in alternates:
            key = normalise(alt)
            # Long folded forms are descriptions, not names ("Munich Bavaria
            # Germany"), and short ones are airport codes.
            if not key or key in seen or len(key) > 30:
                continue
            if not re.search(r"[a-z]{3}", key):
                continue
            seen.add(key)
            alt_rows.append((None, alt, key, country, parts[COL_ADMIN1],
                             lat, lon, parts[COL_TZ], population))

    conn.executemany("INSERT INTO city VALUES (?,?,?,?,?,?,?,?,?)", rows + alt_rows)
    conn.commit()

    stats = {"cities": len(rows), "aliases": len(alt_rows)}
    log_stage(conn, "cities:load", True, str(stats), started)
    return stats


def _candidates(text: str) -> list[str]:
    """The comma-separated pieces of a location field, longest first.

    "New York, NY (HQ); San Francisco, CA; Remote" yields the individual place
    names rather than the whole string, and the longest is tried first so
    "San Francisco" beats "San".
    """
    cleaned = NOISE.sub(" ", text or "")
    pieces = re.split(r"[;,/|()\[\]\n]+", cleaned)
    out = []
    for piece in pieces:
        piece = piece.strip(" -–—·")
        if len(piece) >= 3:
            out.append(piece)
    return sorted(out, key=len, reverse=True)


def resolve(conn, text: str, avoid: set[str] | None = None) -> dict | None:
    """The best city this location field names, or None.

    Population breaks ties, which is the right default: an advert saying
    "Cambridge" with no country means the one people have heard of, and a
    system that picked a village of 400 would be confidently wrong. Where a
    state or province abbreviation is present it is used first, because
    "Austin, TX" is a genuinely different answer from "Austin" alone.

    `avoid` is a set of normalised names the caller has already claimed -- in
    practice, every country name. There is a village called Ireland in Indiana
    and a hamlet called Hungary Station, and population tie-breaking happily
    picked them over the countries, putting Irish jobs on UTC-5. A candidate
    that is a country's name is the country's, not a same-named village's.
    """
    if not text or not text.strip():
        return None
    avoid = avoid or set()

    lowered = text.lower()
    admin1 = None
    match = re.search(r",\s*([a-z]{2})\b", lowered)
    if match and match.group(1) in ADMIN1_HINTS:
        admin1 = ADMIN1_HINTS[match.group(1)]

    for piece in _candidates(text):
        key = normalise(piece)
        if len(key) < 3 or key in avoid:
            continue
        if admin1:
            row = conn.execute(
                "SELECT * FROM city WHERE norm = ? AND admin1 = ? "
                "ORDER BY population DESC LIMIT 1", (key, admin1)).fetchone()
            if row:
                return dict(row) | {"matched": piece, "via": f"city+admin1[{admin1}]"}
        row = conn.execute(
            "SELECT * FROM city WHERE norm = ? ORDER BY population DESC LIMIT 1",
            (key,)).fetchone()
        if row:
            return dict(row) | {"matched": piece, "via": "city"}

    # "New York" is not a GeoNames name -- the city is "New York City". A
    # whole-word prefix match closes that gap, with a five-character floor so
    # "San" cannot silently become San Francisco and "Cam" cannot become
    # Cambridge.
    for piece in _candidates(text):
        key = normalise(piece)
        if len(key) < 5 or key in avoid:
            continue
        row = conn.execute(
            "SELECT * FROM city WHERE norm LIKE ? ORDER BY population DESC LIMIT 1",
            (key + " %",)).fetchone()
        if row:
            return dict(row) | {"matched": piece, "via": "city:prefix"}
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("query", nargs="*")
    args = ap.parse_args(argv)

    conn = connect()

    if args.load:
        stats = load(conn)
        print(f"{stats['cities']:,} cities and {stats['aliases']:,} aliases loaded")
        return 0

    have = conn.execute("SELECT COUNT(*) c FROM city").fetchone()["c"]
    if not have:
        print("no cities loaded — run `python3 cities.py --load`", file=sys.stderr)
        return 1

    if args.check:
        print(f"{have:,} rows\n")
        # The cases that motivated the file: same country, different clocks.
        for probe in ("Austin, TX", "San Francisco, CA", "New York, NY",
                      "Denver, CO", "Colombo", "Berlin", "München", "Zürich",
                      "Perth", "Sydney", "Bengaluru", "São Paulo",
                      "Remote — Austin, TX (HQ)", "London"):
            city = resolve(conn, probe)
            if city:
                print(f"  {probe:<28} {city['name']:<16} {city['country']}  "
                      f"{city['timezone']:<22} pop {city['population']:>9,}")
            else:
                print(f"  {probe:<28} —")
        return 0

    if args.query:
        city = resolve(conn, " ".join(args.query))
        print(city or "no match")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
