#!/usr/bin/env python3
"""One-shot import of the sent rows in CALLBACK-TRACKER.md into jobscout.db.

Why this is not a plain migration: the tracker is Sri Lankan companies emailed
directly, jobscout is remote/global discovery, and the overlap between them is
exactly zero -- every tracker company was checked against all 73,155 jobs and
none is there. `application.job_id` is a NOT NULL foreign key, so an application
cannot exist without a job row. So each imported application gets a synthesised
job: a real record of a real application, marked as one that jobscout never
discovered.

Only rows whose status is 'sent' are imported. The 36 'not sent' rows are a
target list, not history -- writing application rows for them would be
fabricating records, which is the one thing this estate exists to not do. They
stay in the markdown, which remains the target list and the company list that
lib/gmail.py's reply matcher reads.

Synthesised jobs are status='closed' and get no eligibility/fit/score row, so
they cannot surface in the queue by either route. Idempotent on dedupe_key:
running it twice changes nothing.

    python3 migrate_tracker.py            # dry run, prints what it would do
    python3 migrate_tracker.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# Same sys.path.insert precedent lifehub-web/server.py:31 already uses for this
# hyphenated sibling -- and parse_rows is reused rather than re-written, because
# two parsers for one table is how the "**sent**" bug happened the first time.
sys.path.insert(0, str(ROOT / "job-applications"))

import applied                               # noqa: E402
import db                                    # noqa: E402
from chaser import TRACKER, parse_rows       # noqa: E402
from normalise import dedupe_key, slug_company  # noqa: E402

# "Covered by row 50 -- one email only". A row that names another row as the
# one that was actually sent is not a second application; importing both would
# invent an application that never happened.
COVERED_RE = re.compile(r"covered by row\s+(\d+)", re.I)
# The tracker records the real role in the notes, after the address it went to:
# "jobs@example.com - Data Engineer Intern". The Role focus column is a
# CV-variant name ("tech", "business-analyst"), not a job title.
TITLE_RE = re.compile(r"[—-]\s*([^—\-⚠(.]{3,80})")


def title_for(row: dict) -> tuple[str, bool]:
    """(title, derived). Never invents one: an unreadable note falls back to the
    CV variant, and then to saying so."""
    m = TITLE_RE.search(row["notes"])
    if m:
        t = m.group(1).replace("**", "").strip(" .,")
        if t and not t.lower().startswith(("one email", "do not", "row says")):
            return t, True
    if row["role"] and row["role"] not in ("—", "-", ""):
        return row["role"], False
    return "(role not recorded)", False


def plan(conn) -> list[dict]:
    rows = parse_rows(TRACKER.read_text())
    covered = {int(m.group(1)) for r in rows
               if r["status"] == "sent" and (m := COVERED_RE.search(r["notes"]))}
    out = []
    for r in rows:
        if r["status"] != "sent":
            continue
        skip = None
        if COVERED_RE.search(r["notes"]):
            skip = f"note says it is covered by row {COVERED_RE.search(r['notes']).group(1)}"
        title, derived = title_for(r)
        key = f"manual:{dedupe_key(r['company'], title)}"
        existing = conn.execute("SELECT id FROM job WHERE dedupe_key = ?", (key,)).fetchone()
        try:
            sent_at = int(time.mktime(time.strptime(r["sent_date"], "%Y-%m-%d")))
        except (ValueError, OverflowError):
            sent_at, skip = None, skip or f"unparseable sent date {r['sent_date']!r}"
        out.append({**r, "title": title, "derived": derived, "key": key,
                    "sent_at": sent_at, "job_id": existing["id"] if existing else None,
                    "skip": skip})
    return out, covered


def apply(conn, items: list[dict]) -> dict:
    made = skipped = already = 0
    for it in items:
        if it["skip"]:
            skipped += 1
            continue
        job_id = it["job_id"]
        if job_id is None:
            cur = conn.execute(
                "INSERT INTO job (dedupe_key, title, company, company_slug, "
                "canonical_url, first_seen, last_seen, source_count, status) "
                "VALUES (?,?,?,?,NULL,?,?,1,'closed')",
                (it["key"], it["title"], it["company"], slug_company(it["company"]),
                 it["sent_at"], it["sent_at"]))
            job_id = cur.lastrowid
        # The whole note travels into the application, warnings included -- row 11's
        # "the email went to John Keells IT, not Properties" is the kind of thing a
        # tidied-up import would lose, and it is the most useful cell in the table.
        res = applied.mark(conn, job_id, lane="email",
                           note=f"imported from CALLBACK-TRACKER.md row {it['row']}. {it['notes']}"[:500],
                           sent_at=it["sent_at"])
        already += 1 if res["already"] else 0
        made += 0 if res["already"] else 1
    return {"created": made, "already": already, "skipped": skipped}


def sync_row(conn, row_n: int) -> dict:
    """Import one tracker row. LifeHub's mark_sent() calls this after it edits
    the markdown, so the two stores cannot drift apart one row at a time."""
    items, _ = plan(conn)
    for it in items:
        if it["row"] != row_n:
            continue
        if it["skip"]:
            return {"ok": False, "skipped": it["skip"]}
        return {"ok": True, **apply(conn, [it]), "title": it["title"],
                "company": it["company"]}
    return {"ok": False, "skipped": f"row {row_n} is not marked sent in the tracker"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; without it, dry run")
    args = ap.parse_args(argv)

    with db.session() as conn:
        items, covered = plan(conn)
        print(f"{TRACKER}\n{len(items)} rows marked sent\n")
        for it in items:
            mark = "SKIP" if it["skip"] else ("have" if it["job_id"] else "new ")
            star = "*" if it["derived"] else " "
            print(f"  {mark} row {it['row']:>3}  {it['company'][:28]:28} {star}{it['title'][:38]:38} {it['sent_date']}")
            if it["skip"]:
                print(f"         -> skipped: {it['skip']}")
        print("\n  * title read from the notes; unstarred is the CV variant or unrecorded")
        real = sum(1 for i in items if not i["skip"])
        print(f"\n{real} applications to import, {len(items) - real} skipped as duplicates of another row")
        if not args.apply:
            print("\ndry run -- nothing written. Re-run with --apply.")
            return 0
        res = apply(conn, items)
        print(f"\ncreated {res['created']}, already present {res['already']}, skipped {res['skipped']}")
        total = conn.execute("SELECT COUNT(*) c FROM application WHERE status='sent'").fetchone()["c"]
        print(f"applications sent, total: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
