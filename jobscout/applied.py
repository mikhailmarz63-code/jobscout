#!/usr/bin/env python3
"""What he actually sent, and what happened next.

Until this file existed the board could rank jobs forever and never notice he
had applied to any of them. Tomorrow's list showed the same five at the top,
and the only record of an application was his memory. That is the failure mode
a pipeline is supposed to remove.

Three things live here:

  * **marking one applied** -- which drops it out of the daily queue, so the
    list is the work that is left rather than the work that exists;
  * **the follow-up clock** -- his own convention, taken from
    `job-applications/chaser.py` rather than reinvented: due at 7 days, stale at
    14, and a status left on "sent" after a fortnight means he forgot to update
    it, not that they are still thinking;
  * **the export** -- rows written into `resume/applications/CALLBACK-TRACKER.md`,
    the table he already keeps by hand, so `chaser.py`, `interview_kit.py` and
    `prep_packs.py` keep working against the file they already read.

Nothing here sends anything. Marking a job applied is a claim *he* makes after
doing it.

    python3 applied.py                     # the pipeline, and what is due
    python3 applied.py --mark 753 portal   # record one
    python3 applied.py --outcome 753 callback
    python3 applied.py --export            # into CALLBACK-TRACKER.md
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from db import connect, now                  # noqa: E402

TRACKER = ROOT / "resume" / "applications" / "CALLBACK-TRACKER.md"

# His thresholds, from chaser.py. Not re-derived, because two systems disagreeing
# about when a follow-up is due is worse than either answer.
FOLLOWUP_DAYS = 7
STALE_DAYS = 14

LANES = ("email", "portal", "manual")
OUTCOMES = ("callback", "rejected", "silent")


def mark(conn, job_id: int, lane: str = "manual", note: str = "",
         sent_at: int | None = None) -> dict:
    """Record that he applied. Idempotent: applying twice is a mistake worth
    refusing rather than a row worth duplicating -- the tracker already carries
    one "⚠ SENT TWICE" note, and once was enough.

    `sent_at` exists for migrate_tracker.py, which is importing applications
    that were really sent in August: stamping them "now" would restart every
    follow-up clock and report an 18-day-old silence as same-day. It defaults
    to now(), so every live caller is unchanged."""
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}")
    job = conn.execute("SELECT id, company, title FROM job WHERE id = ?",
                       (job_id,)).fetchone()
    if not job:
        raise ValueError(f"no job {job_id}")

    existing = conn.execute(
        "SELECT id, sent_at FROM application WHERE job_id = ?", (job_id,)).fetchone()
    if existing:
        return {"job_id": job_id, "already": True, "sent_at": existing["sent_at"],
                "company": job["company"], "title": job["title"]}

    stamp = sent_at if sent_at is not None else now()
    conn.execute(
        "INSERT INTO application (job_id, lane, mode, status, sent_at, created_at, note) "
        "VALUES (?, ?, 'manual', 'sent', ?, ?, ?)",
        (job_id, lane, stamp, now(), note or None))
    conn.execute(
        "INSERT INTO touch (job_id, kind, channel, body, occurred_at) "
        "VALUES (?, 'applied', ?, ?, ?)",
        (job_id, lane if lane in ("email", "portal") else "other",
         note or f"applied via {lane}", stamp))
    conn.commit()
    return {"job_id": job_id, "already": False, "sent_at": stamp,
            "company": job["company"], "title": job["title"]}


def outcome(conn, job_id: int, result: str, note: str = "") -> bool:
    if result not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    row = conn.execute("SELECT id FROM application WHERE job_id = ?",
                       (job_id,)).fetchone()
    if not row:
        return False
    stamp = now()
    conn.execute("UPDATE application SET outcome = ?, outcome_at = ? WHERE job_id = ?",
                 (result, stamp, job_id))
    conn.execute(
        "INSERT INTO touch (job_id, kind, channel, body, occurred_at) "
        "VALUES (?, 'note', 'other', ?, ?)",
        (job_id, note or f"outcome: {result}", stamp))
    conn.commit()
    return True


def followup_state(sent_at: int | None, outcome_value: str | None) -> tuple[str, int]:
    """(state, days). The clock only runs while nothing has come back."""
    if not sent_at:
        return "unsent", 0
    days = max(0, (now() - sent_at) // 86400)
    if outcome_value:
        return outcome_value, days
    if days >= STALE_DAYS:
        return "stale", days
    if days >= FOLLOWUP_DAYS:
        return "due", days
    return "waiting", days


def pipeline(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT a.job_id, a.lane, a.sent_at, a.outcome, a.note,
               j.company, j.title, j.canonical_url
        FROM application a JOIN job j ON j.id = a.job_id
        WHERE a.status = 'sent'
        ORDER BY a.sent_at DESC""").fetchall()
    out = []
    for r in rows:
        state, days = followup_state(r["sent_at"], r["outcome"])
        out.append(dict(r) | {"state": state, "days": days})
    return out


def summary(conn) -> dict:
    items = pipeline(conn)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {"total": len(items), "by_state": counts,
            "due": [i for i in items if i["state"] in ("due", "stale")]}


# ------------------------------------------------------------------ export --

ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")


def export_tracker(conn, dry_run: bool = False) -> list[str]:
    """Append anything not already in CALLBACK-TRACKER.md.

    He maintains that table by hand and three other tools read it. Writing to it
    rather than replacing it is the whole point -- `chaser.py`,
    `interview_kit.py` and `prep_packs.py` keep working untouched.

    Matching is on company name, because the table predates this database and
    has no job ids in it.
    """
    if not TRACKER.exists():
        return []
    text = TRACKER.read_text()
    lines = text.splitlines(keepends=True)
    known = {line.split("|")[2].strip().lower()
             for line in lines if ROW_RE.match(line) and line.count("|") > 3}

    row_idxs = [i for i, line in enumerate(lines) if ROW_RE.match(line)]
    if not row_idxs:
        return []
    next_n = int(ROW_RE.match(lines[row_idxs[-1]]).group(1)) + 1

    added = []
    for item in pipeline(conn):
        if item["company"].lower() in known:
            continue
        sent = datetime.fromtimestamp(item["sent_at"]).strftime("%Y-%m-%d")
        status = {"callback": "**callback**", "rejected": "rejected",
                  "silent": "silent (14d+)"}.get(item["outcome"], "**sent**")
        note = f"{item['title']} — via {item['lane']}, from jobscout"
        added.append(f"| {next_n} | {item['company']} | — | {sent} | {status} | | {note} |\n")
        known.add(item["company"].lower())
        next_n += 1

    if added and not dry_run:
        lines[row_idxs[-1] + 1:row_idxs[-1] + 1] = added
        TRACKER.write_text("".join(lines))
    return added


def next_up(conn, count: int = 1) -> list[dict]:
    """The top viable jobs he has not applied to, best first.

    Everything already in `application` drops out, which is the whole reason
    that table exists: tomorrow's queue should be the work that is left.
    """
    return [dict(r) for r in conn.execute("""
        SELECT j.id, j.title, j.company, j.canonical_url, sc.total score,
               (SELECT p.apply_url FROM posting p WHERE p.job_id = j.id
                 AND p.apply_url IS NOT NULL LIMIT 1) apply_url
        FROM job j
        JOIN fit f ON f.job_id = j.id
        JOIN score sc ON sc.job_id = j.id
        LEFT JOIN application a ON a.job_id = j.id
        WHERE f.viable = 1 AND a.job_id IS NULL
        ORDER BY sc.total DESC LIMIT ?""", (count,))]


def prepare_next(conn, count: int = 1, write: bool = True) -> list[str]:
    """Fetch the form, build the pack, save it, and hand back what to open.

    Deliberately stops here. It does not open a browser and it does not submit
    anything -- `settings.yml` has said since the beginning that automating an
    ATS portal breaks its terms and gets accounts flagged, and that is still
    the right call. What it removes is the retyping, which was the actual cost.
    """
    import forms
    import kit
    told = []
    for job in next_up(conn, count):
        forms.fetch_one(conn, job["id"])
        pack = kit.build(conn, job["id"])
        if not pack:
            continue
        blockers = pack.get("blockers") or []
        path = ""
        if write and not blockers:
            path = str(kit.write_pack(job["id"], pack))
        told.append({
            "job_id": job["id"], "title": job["title"], "company": job["company"],
            "score": job["score"], "blockers": blockers,
            "url": job["apply_url"] or job["canonical_url"] or "",
            "path": path, "form": pack.get("form", False),
            "gaps": sum(1 for _, body in pack["sections"]
                        if body.startswith("[NEEDS YOU]")),
        })
    return told


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mark", nargs="+", metavar=("JOB_ID", "LANE"))
    ap.add_argument("--outcome", nargs="+", metavar=("JOB_ID", "RESULT"))
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--next", nargs="?", type=int, const=1, metavar="N",
                    help="prepare packs for the next N unapplied jobs")
    args = ap.parse_args(argv)

    conn = connect()

    if args.next:
        ready = prepare_next(conn, args.next)
        if not ready:
            print("nothing left in the viable queue that has not been applied to")
        for item in ready:
            print(f"\n  {item['job_id']}  {item['title'][:56]}")
            print(f"        {item['company'][:30]}   score {item['score']:.0f}"
                  f"   {'real form' if item['form'] else 'generic pack'}")
            if item["blockers"]:
                print("        SKIP — the form rules him out:")
                for b in item["blockers"]:
                    print(f"          x {b}")
                continue
            if item["gaps"]:
                print(f"        {item['gaps']} field(s) need his own words")
            print(f"        pack:  {item['path']}")
            print(f"        apply: {item['url']}")
        print()
        return 0

    if args.mark:
        job_id = int(args.mark[0])
        lane = args.mark[1] if len(args.mark) > 1 else "manual"
        result = mark(conn, job_id, lane)
        if result["already"]:
            sent = datetime.fromtimestamp(result["sent_at"]).strftime("%d %b")
            print(f"already applied to {result['company']} on {sent} — not recording twice")
        else:
            print(f"recorded: {result['company']} — {result['title'][:50]} ({lane})")
        return 0

    if args.outcome:
        job_id, result = int(args.outcome[0]), args.outcome[1]
        print("recorded" if outcome(conn, job_id, result)
              else f"no application on record for job {job_id}")
        return 0

    if args.export:
        added = export_tracker(conn, dry_run=args.dry_run)
        for row in added:
            print("  " + row.strip())
        print(f"\n{len(added)} row(s)"
              f"{' would be' if args.dry_run else ''} added to "
              f"{TRACKER.relative_to(ROOT)}")
        return 0

    s = summary(conn)
    if not s["total"]:
        print("\nNothing applied to yet.\n"
              "Mark one from the board, or: python3 applied.py --mark <id> portal\n")
        return 0

    print(f"\n{s['total']} application(s) in flight\n")
    for state in ("stale", "due", "waiting", "callback", "rejected", "silent"):
        items = [i for i in pipeline(conn) if i["state"] == state]
        if not items:
            continue
        label = {"due": f"FOLLOW UP — {FOLLOWUP_DAYS}+ days, no reply",
                 "stale": f"STALE — {STALE_DAYS}+ days; update the status",
                 "waiting": "waiting", "callback": "callback",
                 "rejected": "rejected", "silent": "silent"}[state]
        print(f"  {label}")
        for i in items:
            print(f"    #{i['job_id']:<5} {i['days']:>3}d  {i['company'][:26]:<26} "
                  f"{i['title'][:40]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
