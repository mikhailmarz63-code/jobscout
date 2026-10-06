#!/usr/bin/env python3
"""What the employer says about itself, so the cover letter can say something.

Every letter `kit.py` produces still ships with this line in it:

    [ONE SENTENCE ON WHY THIS COMPANY — name something specific from their
    posting or product.]

That bracket is the only part of the letter that could not have been sent to
anyone else, which makes it the only part that matters, and it is the part that
was left blank. Filling it is the cheapest available improvement to whether an
application gets read.

**Nothing here is invented.** The summary is sentences lifted verbatim from the
company's own page, stored with the URL they came from, and shown to the candidate as
a *draft* beside its source. `kit.py`'s whole doctrine is that no claim appears
in a pack unless it is evidenced -- and a fabricated compliment about a company
is exactly the failure that loses an application, because the one person
guaranteed to read it is someone who works there.

One fetch per company, cached forever. Failures are recorded too, so a company
with no reachable site is not retried every morning.

    python3 companies.py --fetch 40     # fill in the ones jobs point at
    python3 companies.py --status
    python3 companies.py stripe         # what do we know about one
"""
from __future__ import annotations

import argparse
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, log_stage, now          # noqa: E402
from sources import get, settings, strip_html   # noqa: E402

# Pages that describe a company in its own words, in the order worth trying.
# Two paths, not six.
#
# The first version tried eight candidate domains against six paths with the
# shared 30-second timeout: forty-eight requests per company, most of them to
# domains that do not resolve. Thirty companies did not finish in ten minutes.
# The advert path below costs nothing and covers most of it, so the web
# fallback is now a bounded long shot rather than an exhaustive search.
ABOUT_PATHS = ("/about", "/")
MAX_HOSTS = 3
WEB_TIMEOUT = 6

# Hosts that are a job board rather than the employer. Their "about" page
# describes the board, and a letter praising Greenhouse's mission to an
# applicant-tracking customer would be worse than leaving the bracket empty.
BOARD_HOSTS = (
    "greenhouse.io", "ashbyhq.com", "lever.co", "workable.com",
    "smartrecruiters.com", "weworkremotely.com", "remoteok.com",
    "remotive.com", "jobicy.com", "arbeitnow.com", "himalayas.app",
    "ycombinator.com", "linkedin.com", "indeed.com", "glassdoor.com",
)

# A sentence worth quoting: it says what they do, not that they value teamwork.
SUBSTANCE = re.compile(
    r"\b(we (?:build|make|help|power|provide|enable|run|design|develop)|"
    r"our (?:platform|product|software|mission|customers|technology)|"
    r"is (?:a|an|the) (?:platform|company|marketplace|tool|service)|"
    r"used by|trusted by|founded in|customers include|powers)\b", re.I)

# Boilerplate that says nothing. A letter quoting any of these reads worse than
# one that quotes nothing.
FILLER = re.compile(
    r"\b(cookie|privacy policy|terms of service|all rights reserved|"
    r"equal opportunity|sign up|log ?in|subscribe|newsletter|"
    r"we are committed to (?:diversity|excellence)|javascript)\b", re.I)


# The advert's own "About us" block.
#
# This turned out to be the better source and is now the primary one. Scraping
# a company's website yielded 2 usable summaries in 15 attempts and one of them
# was a values statement -- "Excellence Surpass expectations to delight our
# customers" -- which is worse than an empty bracket. The advert's About
# section hits 24% and reads properly, because it is the company describing
# itself *to an applicant*, which is exactly the audience the cover letter is
# answering.
ABOUT_BLOCK = re.compile(
    # "About the role" is not about the company, and matching it put the job
    # description into the cover letter's why-this-company sentence.
    r"(?:^|\n)\s*(?:about\s+(?!the\s+role|this\s+role|the\s+position|the\s+job"
    r"|the\s+opportunity|the\s+team\b)(?:us|the\s+company|[\w.\-]+)"
    r"|who we are|our (?:company|mission|story))"
    r"\b[:\s\-—]*(.{80,900}?)(?:\n\n|\Z)",
    re.I | re.S)

# Values boilerplate. Every company has these and none of them distinguish one.
VALUES_NOISE = re.compile(
    r"\b(excellence|integrity|passionate|world[- ]class|synergy|"
    r"delight our customers|surpass expectations|think big|move fast|"
    r"best[- ]in[- ]class|cutting[- ]edge|rock ?star|ninja)\b", re.I)


URL_IN_TEXT = re.compile(r"https?://([\w.-]+\.[a-z]{2,})", re.I)
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")

FREE_MAIL = ("gmail.", "yahoo.", "outlook.", "hotmail.", "protonmail.",
             "icloud.", "aol.", "mail.com")


def site_candidates(conn, slug: str, name: str) -> list[str]:
    """Every plausible domain for this employer, best evidence first.

    The apply URL was the obvious source and turned out to be useless: for the
    viable jobs, **every single one** points at a job board -- Hacker News,
    We Work Remotely, Arbeitnow -- and not one at the employer. So the domain
    has to come from the advert's own text, and failing that from the company's
    name, which is a guess and is treated as one: a guessed domain is only
    accepted if the page it serves actually mentions the company.
    """
    hosts: list[str] = []

    def add(host: str) -> None:
        host = (host or "").lower().strip().rstrip(".")
        if (host and "." in host
                and not any(b in host for b in BOARD_HOSTS)
                and not any(f in host for f in FREE_MAIL)
                and host not in hosts):
            hosts.append(host)

    # 1. What the advert itself links to, and who it says to email.
    for r in conn.execute("""
            SELECT p.description, p.apply_url, p.url, p.apply_email FROM posting p
            JOIN job j ON j.id = p.job_id WHERE j.company_slug = ? LIMIT 8""",
            (slug,)):
        if r["apply_email"] and "@" in r["apply_email"]:
            add(r["apply_email"].split("@")[-1])
        for candidate in (r["apply_url"], r["url"]):
            if candidate:
                add(urlparse(candidate).netloc)
        for match in EMAIL_IN_TEXT.finditer((r["description"] or "")[:6000]):
            add(match.group(1))
        for match in URL_IN_TEXT.finditer((r["description"] or "")[:6000]):
            add(match.group(1))

    # 2. The name, as a domain. Only ever accepted after verification.
    stem = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if 2 < len(stem) < 24:
        for tld in (".com", ".io", ".ai", ".co", ".dev"):
            add(stem + tld)

    return hosts[:MAX_HOSTS]


def _mentions(text: str, name: str) -> bool:
    """Does this page belong to the company we think it does?

    A guessed domain is frequently a parking page, a squatter, or an unrelated
    business with the same short name. Requiring the page to say the company's
    name is the cheapest possible proof, and without it the summary in a cover
    letter could describe somebody else entirely.
    """
    stem = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    flat = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    return len(stem) > 2 and stem in flat


def company_site(conn, slug: str) -> str | None:
    row = conn.execute("SELECT site FROM company WHERE slug = ?", (slug,)).fetchone()
    return row["site"] if row and row["site"] else None


def summarise(text: str, limit: int = 3) -> str:
    """A few of the company's own sentences, verbatim.

    Deliberately extractive. Rewriting them would produce something fluent that
    the company never said, which is precisely the thing this file exists to
    avoid.
    """
    clean = re.sub(r"\s+", " ", strip_html(unescape(text or "")))
    picked = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean):
        sentence = sentence.strip()
        if not (40 <= len(sentence) <= 220):
            continue
        if FILLER.search(sentence) or VALUES_NOISE.search(sentence):
            continue
        if not SUBSTANCE.search(sentence):
            continue
        if sentence in picked:
            continue
        picked.append(sentence)
        if len(picked) >= limit:
            break
    return " ".join(picked)


def from_advert(conn, slug: str) -> tuple[str, str] | None:
    """(summary, url) from the job advert's own About section.

    Free, instant, and better than the website: it is what the company chose to
    tell applicants. The longest one wins, because a company posting six roles
    writes the same blurb six times and one of them is usually fuller.
    """
    best, best_url = "", ""
    for r in conn.execute("""
            SELECT p.description, p.url FROM posting p
            JOIN job j ON j.id = p.job_id
            WHERE j.company_slug = ? AND p.description <> '' LIMIT 12""", (slug,)):
        match = ABOUT_BLOCK.search(r["description"] or "")
        if not match:
            continue
        text = re.sub(r"\s+", " ", match.group(1)).strip()
        # Trim to whole sentences and drop anything that is only boilerplate.
        keep = [s for s in re.split(r"(?<=[.!?])\s+", text)
                if 30 <= len(s) <= 260 and not VALUES_NOISE.search(s)
                and not FILLER.search(s)]
        text = " ".join(keep[:3])
        if len(text) > len(best):
            best, best_url = text, r["url"]
    return (best, best_url) if len(best) >= 60 else None


def fetch_one(conn, slug: str, name: str, cfg: dict,
              advert_only: bool = False) -> dict:
    # The advert first: no network call, better prose, and it is the company
    # talking to applicants rather than to customers.
    advert = from_advert(conn, slug)
    if advert:
        summary, url = advert
        conn.execute("""
            INSERT INTO company (slug, name, site, source_url, summary, fetched_at, failed)
            VALUES (?, ?, NULL, ?, ?, ?, 0)
            ON CONFLICT (slug) DO UPDATE SET
              source_url = excluded.source_url, summary = excluded.summary,
              fetched_at = excluded.fetched_at, failed = 0
        """, (slug, name, url, summary, now()))
        conn.commit()
        return {"slug": slug, "ok": True, "url": url, "summary": summary,
                "via": "advert"}

    if advert_only:
        conn.execute(
            "INSERT INTO company (slug, name, failed, fetched_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT (slug) DO UPDATE SET failed = 1, "
            "fetched_at = excluded.fetched_at", (slug, name, now()))
        conn.commit()
        return {"slug": slug, "ok": False, "why": "no About section in the advert"}

    for host in site_candidates(conn, slug, name):
      site = f"https://{host}"
      for path in ABOUT_PATHS:
        url = site.rstrip("/") + path
        html_text = get(url, cfg={**cfg, "http": {**cfg.get("http", {}),
                                                  "timeout": WEB_TIMEOUT}},
                        as_json=False)
        if not html_text:
            continue
        # The page has to be theirs before anything on it is quotable.
        if not _mentions(html_text[:40000], name):
            continue
        summary = summarise(html_text)
        if not summary:
            continue
        conn.execute("""
            INSERT INTO company (slug, name, site, source_url, summary, fetched_at, failed)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT (slug) DO UPDATE SET
              site = excluded.site, source_url = excluded.source_url,
              summary = excluded.summary, fetched_at = excluded.fetched_at, failed = 0
        """, (slug, name, site, url, summary, now()))
        conn.commit()
        return {"slug": slug, "ok": True, "url": url, "summary": summary,
                "via": "website"}

    conn.execute(
        "INSERT INTO company (slug, name, failed, fetched_at) VALUES (?, ?, 1, ?) "
        "ON CONFLICT (slug) DO UPDATE SET failed = 1, "
        "fetched_at = excluded.fetched_at", (slug, name, now()))
    conn.commit()
    return {"slug": slug, "ok": False, "why": "no verifiable company page"}


def pending(conn, limit: int) -> list[tuple[str, str]]:
    """Companies with a viable job and no cached summary, best jobs first."""
    return [(r["company_slug"], r["company"]) for r in conn.execute("""
        SELECT j.company_slug, j.company, MAX(sc.total) best
        FROM job j
        JOIN fit f ON f.job_id = j.id AND f.viable = 1
        LEFT JOIN score sc ON sc.job_id = j.id
        WHERE j.company_slug NOT IN (SELECT slug FROM company)
        GROUP BY j.company_slug
        ORDER BY best DESC NULLS LAST
        LIMIT ?""", (limit,))]


def lookup(conn, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM company WHERE slug = ? AND failed = 0",
                       (slug,)).fetchone()
    return dict(row) if row else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", type=int, metavar="N")
    ap.add_argument("--advert-only", action="store_true",
                    help="skip the web fallback entirely — instant, and covers "
                         "most of what is coverable")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("slug", nargs="?")
    args = ap.parse_args(argv)

    conn = connect()

    if args.slug:
        found = lookup(conn, args.slug)
        if not found:
            print(f"nothing cached for {args.slug}")
            return 1
        print(f"\n{found['name']}  ({found['site']})\n{found['source_url']}\n")
        print(found["summary"])
        return 0

    if args.status or args.fetch is None:
        have = conn.execute("SELECT COUNT(*) c FROM company WHERE failed = 0").fetchone()["c"]
        bad = conn.execute("SELECT COUNT(*) c FROM company WHERE failed = 1").fetchone()["c"]
        left = len(pending(conn, 10_000))
        print(f"\n  summarised   {have:>5}")
        print(f"  no summary   {bad:>5}")
        print(f"  still to try {left:>5}")
        return 0

    started = now()
    ok = bad = 0
    for slug, name in pending(conn, args.fetch):
        result = fetch_one(conn, slug, name, settings(),
                           advert_only=args.advert_only)
        if result["ok"]:
            ok += 1
            print(f"  {name[:22]:<22} [{result.get('via','?'):<7}] "
                  f"{result['summary'][:64]}")
        else:
            bad += 1
    log_stage(conn, "companies", True, f"ok={ok} failed={bad}", started)
    print(f"\n  {ok} summarised, {bad} without a usable page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
