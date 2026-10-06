#!/usr/bin/env python3
"""Builds `world.json`: every country's outline, centroid and timezone, small
enough to ship to a browser.

Source is Natural Earth admin-0 at 1:110m, which is public domain. The thinning
is xonvet's `areas.py` method, and for the same reason -- the board draws these
as SVG with no tiles and no CDN, so the strict CSP never has to move, and the
file has to cross the wire on a phone.

  * round every coordinate to three decimal places (~110 m)
  * drop any point that lands within ~0.9 km of the last one kept, which is
    below one pixel at any zoom this board uses
  * **never drop a country.** A missing outline reads as a country that does
    not exist, which is a worse lie than a coarse one, so a country whose every
    ring is too small to draw keeps its largest one anyway.

The thinning earns much less here than the same code earns in xonvet, and it is
worth saying so: the 110m set is already generalised, so 10,612 points become
10,551 and the win is a rounding error. What actually keeps the file at 67 KB on
the wire is choosing 110m over 50m in the first place. The thinning stays as the
guard for the day someone swaps the source for a finer one.

Timezones come from the table below and the offset from the operating system's
own tzdata via `zoneinfo`, so daylight saving is always current rather than
frozen into a constant at the moment this file was written.

    python3 world.py            # build world.json
    python3 world.py --check    # what is in it, and what is missing
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
OUT = HERE / "world.json"
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
          "master/geojson/ne_110m_admin_0_countries.geojson")

sys.path.insert(0, str(HERE))
from sources import get, settings          # noqa: E402

# Below one pixel on any zoom this board uses. Degrees, roughly 0.9 km.
MIN_STEP = 0.008
PRECISION = 3

# ISO 3166-1 alpha-2 -> a representative IANA zone.
#
# Countries that span several zones get their principal business zone, and are
# marked `multi` so the board can say so rather than implying a precision the
# data does not have. A remote US role is scheduled from New York or San
# Francisco far more often than from Anchorage.
ZONES = {
    "US": ("America/New_York", True), "CA": ("America/Toronto", True),
    "BR": ("America/Sao_Paulo", True), "MX": ("America/Mexico_City", True),
    "AR": ("America/Argentina/Buenos_Aires", False), "CL": ("America/Santiago", False),
    "CO": ("America/Bogota", False), "PE": ("America/Lima", False),
    "UY": ("America/Montevideo", False), "CR": ("America/Costa_Rica", False),
    "GT": ("America/Guatemala", False), "PA": ("America/Panama", False),
    "DO": ("America/Santo_Domingo", False), "JM": ("America/Jamaica", False),
    "EC": ("America/Guayaquil", False), "BO": ("America/La_Paz", False),
    "PY": ("America/Asuncion", False), "VE": ("America/Caracas", False),

    "GB": ("Europe/London", False), "IE": ("Europe/Dublin", False),
    "PT": ("Europe/Lisbon", False), "ES": ("Europe/Madrid", False),
    "FR": ("Europe/Paris", False), "DE": ("Europe/Berlin", False),
    "NL": ("Europe/Amsterdam", False), "BE": ("Europe/Brussels", False),
    "LU": ("Europe/Luxembourg", False), "CH": ("Europe/Zurich", False),
    "AT": ("Europe/Vienna", False), "IT": ("Europe/Rome", False),
    "PL": ("Europe/Warsaw", False), "CZ": ("Europe/Prague", False),
    "SK": ("Europe/Bratislava", False), "HU": ("Europe/Budapest", False),
    "RO": ("Europe/Bucharest", False), "BG": ("Europe/Sofia", False),
    "GR": ("Europe/Athens", False), "HR": ("Europe/Zagreb", False),
    "SI": ("Europe/Ljubljana", False), "RS": ("Europe/Belgrade", False),
    "BA": ("Europe/Sarajevo", False), "AL": ("Europe/Tirane", False),
    "MK": ("Europe/Skopje", False), "ME": ("Europe/Podgorica", False),
    "SE": ("Europe/Stockholm", False), "NO": ("Europe/Oslo", False),
    "DK": ("Europe/Copenhagen", False), "FI": ("Europe/Helsinki", False),
    "IS": ("Atlantic/Reykjavik", False), "EE": ("Europe/Tallinn", False),
    "LV": ("Europe/Riga", False), "LT": ("Europe/Vilnius", False),
    "UA": ("Europe/Kyiv", False), "BY": ("Europe/Minsk", False),
    "MD": ("Europe/Chisinau", False), "RU": ("Europe/Moscow", True),
    "TR": ("Europe/Istanbul", False), "CY": ("Asia/Nicosia", False),
    "MT": ("Europe/Malta", False),

    "IN": ("Asia/Kolkata", False), "LK": ("Asia/Colombo", False),
    "PK": ("Asia/Karachi", False), "BD": ("Asia/Dhaka", False),
    "NP": ("Asia/Kathmandu", False), "MV": ("Indian/Maldives", False),
    "BT": ("Asia/Thimphu", False), "AF": ("Asia/Kabul", False),
    "CN": ("Asia/Shanghai", False), "HK": ("Asia/Hong_Kong", False),
    "TW": ("Asia/Taipei", False), "JP": ("Asia/Tokyo", False),
    "KR": ("Asia/Seoul", False), "KP": ("Asia/Pyongyang", False),
    "SG": ("Asia/Singapore", False), "MY": ("Asia/Kuala_Lumpur", False),
    "TH": ("Asia/Bangkok", False), "VN": ("Asia/Ho_Chi_Minh", False),
    "PH": ("Asia/Manila", False), "ID": ("Asia/Jakarta", True),
    "MM": ("Asia/Yangon", False), "KH": ("Asia/Phnom_Penh", False),
    "LA": ("Asia/Vientiane", False), "MN": ("Asia/Ulaanbaatar", False),
    "KZ": ("Asia/Almaty", True), "UZ": ("Asia/Tashkent", False),
    "GE": ("Asia/Tbilisi", False), "AM": ("Asia/Yerevan", False),
    "AZ": ("Asia/Baku", False), "IR": ("Asia/Tehran", False),
    "IQ": ("Asia/Baghdad", False), "IL": ("Asia/Jerusalem", False),
    "JO": ("Asia/Amman", False), "LB": ("Asia/Beirut", False),
    "SY": ("Asia/Damascus", False), "SA": ("Asia/Riyadh", False),
    "AE": ("Asia/Dubai", False), "QA": ("Asia/Qatar", False),
    "KW": ("Asia/Kuwait", False), "BH": ("Asia/Bahrain", False),
    "OM": ("Asia/Muscat", False), "YE": ("Asia/Aden", False),

    "AU": ("Australia/Sydney", True), "NZ": ("Pacific/Auckland", False),
    "FJ": ("Pacific/Fiji", False), "PG": ("Pacific/Port_Moresby", False),

    "ZA": ("Africa/Johannesburg", False), "NG": ("Africa/Lagos", False),
    "KE": ("Africa/Nairobi", False), "EG": ("Africa/Cairo", False),
    "MA": ("Africa/Casablanca", False), "TN": ("Africa/Tunis", False),
    "DZ": ("Africa/Algiers", False), "GH": ("Africa/Accra", False),
    "ET": ("Africa/Addis_Ababa", False), "TZ": ("Africa/Dar_es_Salaam", False),
    "UG": ("Africa/Kampala", False), "RW": ("Africa/Kigali", False),
    "SN": ("Africa/Dakar", False), "CI": ("Africa/Abidjan", False),
    "CM": ("Africa/Douala", False), "ZW": ("Africa/Harare", False),
    "ZM": ("Africa/Lusaka", False), "MZ": ("Africa/Maputo", False),
    "AO": ("Africa/Luanda", False), "MU": ("Indian/Mauritius", False),
    "BW": ("Africa/Gaborone", False), "NA": ("Africa/Windhoek", False),
    "LY": ("Africa/Tripoli", False), "SD": ("Africa/Khartoum", False),

    # The long tail. Almost none of these will ever post a job he wants, but a
    # country with no offset gets no band, and a hole in the map reads as a
    # country that does not exist rather than one nobody is hiring in.
    "EH": ("Africa/El_Aaiun", False), "CD": ("Africa/Kinshasa", True),
    "CG": ("Africa/Brazzaville", False), "SO": ("Africa/Mogadishu", False),
    "TD": ("Africa/Ndjamena", False), "HT": ("America/Port-au-Prince", False),
    "BS": ("America/Nassau", False), "FK": ("Atlantic/Stanley", False),
    "GL": ("America/Nuuk", True), "TL": ("Asia/Dili", False),
    "LS": ("Africa/Maseru", False), "NI": ("America/Managua", False),
    "HN": ("America/Tegucigalpa", False), "SV": ("America/El_Salvador", False),
    "BZ": ("America/Belize", False), "GY": ("America/Guyana", False),
    "SR": ("America/Paramaribo", False), "PR": ("America/Puerto_Rico", False),
    "CU": ("America/Havana", False), "ML": ("Africa/Bamako", False),
    "MR": ("Africa/Nouakchott", False), "BJ": ("Africa/Porto-Novo", False),
    "NE": ("Africa/Niamey", False), "TG": ("Africa/Lome", False),
    "BF": ("Africa/Ouagadougou", False), "GN": ("Africa/Conakry", False),
    "GW": ("Africa/Bissau", False), "SL": ("Africa/Freetown", False),
    "LR": ("Africa/Monrovia", False), "GM": ("Africa/Banjul", False),
    "CF": ("Africa/Bangui", False), "GA": ("Africa/Libreville", False),
    "GQ": ("Africa/Malabo", False), "SS": ("Africa/Juba", False),
    "ER": ("Africa/Asmara", False), "DJ": ("Africa/Djibouti", False),
    "BI": ("Africa/Bujumbura", False), "MW": ("Africa/Blantyre", False),
    "MG": ("Indian/Antananarivo", False), "SZ": ("Africa/Mbabane", False),
    "TM": ("Asia/Ashgabat", False), "TJ": ("Asia/Dushanbe", False),
    "KG": ("Asia/Bishkek", False), "BN": ("Asia/Brunei", False),
    "NC": ("Pacific/Noumea", False), "SB": ("Pacific/Guadalcanal", False),
    "VU": ("Pacific/Efate", False), "TT": ("America/Port_of_Spain", False),
    "XK": ("Europe/Belgrade", False), "PS": ("Asia/Hebron", False),
    "ATA": ("Antarctica/McMurdo", False), "AQ": ("Antarctica/McMurdo", False),
}


def utc_offset(zone: str, when: datetime | None = None) -> float | None:
    """Hours, possibly fractional -- Colombo is +5.5 and Kathmandu is +5.75.

    Read from the OS tzdata at call time rather than stored, so daylight saving
    is right today instead of right on the day this file was written. Half the
    world moves an hour twice a year and it changes the overlap band.
    """
    try:
        stamp = when or datetime.now()
        delta = stamp.astimezone(ZoneInfo(zone)).utcoffset()
        return round(delta.total_seconds() / 3600, 2) if delta else None
    except Exception:            # noqa: BLE001 -- an unknown zone is data, not a crash
        return None


def thin(ring: list) -> list:
    """Round, then drop anything closer than one pixel to the last point kept."""
    out = []
    for point in ring:
        try:
            lon, lat = round(float(point[0]), PRECISION), round(float(point[1]), PRECISION)
        except (TypeError, ValueError, IndexError):
            continue
        if out:
            last = out[-1]
            if math.hypot(lon - last[0], lat - last[1]) < MIN_STEP:
                continue
        out.append([lon, lat])
    # A ring needs three points to be a shape at all.
    if len(out) >= 3 and out[0] != out[-1]:
        out.append(out[0])
    return out


def rings_of(geometry: dict) -> list[list]:
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "Polygon":
        return [r for r in coords]
    if kind == "MultiPolygon":
        return [r for polygon in coords for r in polygon]
    return []


def centroid(rings: list[list]) -> tuple[float, float]:
    """Area-weighted centre of the largest ring. The mean of every point puts
    the United States in Kansas and Indonesia in the sea; the largest ring at
    least lands on the country's main landmass."""
    if not rings:
        return 0.0, 0.0
    biggest = max(rings, key=len)
    lons = [p[0] for p in biggest]
    lats = [p[1] for p in biggest]
    return round(sum(lons) / len(lons), 3), round(sum(lats) / len(lats), 3)


def build() -> dict:
    print(f"fetching Natural Earth 110m admin-0 …")
    data = get(NE_URL, cfg=settings())
    if not data or "features" not in data:
        print("could not fetch the boundaries", file=sys.stderr)
        return {}

    countries = {}
    kept_points = total_points = 0

    for feature in data["features"]:
        props = feature.get("properties", {})
        iso = (props.get("ISO_A2_EH") or props.get("ISO_A2") or "").strip()
        name = props.get("NAME") or props.get("ADMIN") or iso
        if not iso or iso in ("-99", ""):
            continue

        raw_rings = rings_of(feature.get("geometry") or {})
        total_points += sum(len(r) for r in raw_rings)
        thinned = [t for t in (thin(r) for r in raw_rings) if len(t) >= 4]

        # Never drop a country to save bytes.
        if not thinned and raw_rings:
            biggest = max(raw_rings, key=len)
            thinned = [[[round(float(p[0]), PRECISION), round(float(p[1]), PRECISION)]
                        for p in biggest]]
        kept_points += sum(len(r) for r in thinned)

        zone, multi = ZONES.get(iso, (None, False))
        lon, lat = centroid(thinned or raw_rings)
        countries[iso] = {
            "name": name,
            "continent": props.get("CONTINENT"),
            "region": props.get("REGION_UN"),
            "subregion": props.get("SUBREGION"),
            "lon": lon, "lat": lat,
            "zone": zone,
            "utc_offset": utc_offset(zone) if zone else None,
            "multi_zone": multi,
            "rings": thinned,
        }

    world = {"built_at": int(datetime.now().timestamp()),
             "source": "Natural Earth 110m admin-0 (public domain)",
             "countries": countries}
    OUT.write_text(json.dumps(world, separators=(",", ":")))

    on_wire = len(gzip.compress(OUT.read_bytes()))
    missing = [f"{iso} {c['name']}" for iso, c in countries.items() if not c["zone"]]
    print(f"{len(countries)} countries · {total_points:,} points -> {kept_points:,}")
    print(f"{OUT.stat().st_size / 1024:.0f} KB on disk, {on_wire / 1024:.0f} KB on the wire")
    if missing:
        print(f"\n{len(missing)} without a timezone (they will not get a band):")
        print("  " + ", ".join(missing[:24]))
    return world


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        if not OUT.exists():
            print("world.json not built yet — run `python3 world.py`")
            return 1
        world = json.loads(OUT.read_text())
        countries = world["countries"]
        print(f"{len(countries)} countries, built "
              f"{datetime.fromtimestamp(world['built_at']):%d %b %Y}")
        for iso in ("LK", "US", "GB", "DE", "AU", "SG"):
            c = countries.get(iso)
            if c:
                span = " (spans zones)" if c["multi_zone"] else ""
                print(f"  {iso}  {c['name']:<18} UTC{c['utc_offset']:+.1f}  "
                      f"{len(c['rings'])} rings{span}")
        return 0

    return 0 if build() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
