#!/usr/bin/env python3
"""The loop. Not a pipeline -- every stage underneath is idempotent and
resumable, so a run picks up whatever is due, does it, and stops. Run it twice
and the second run does almost nothing.

That property is what makes scheduling it safe. A cron job that half-fails at
07:31 leaves the database consistent, and the 07:45 run finishes the work
rather than starting again from the top.

    python3 run.py                # the daily pass
    python3 run.py --full         # re-decide everything (after a rule change)
    python3 run.py --status       # what state everything is in
    python3 run.py --install      # launchd, every morning at 07:30
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import connect, maintain, now, session      # noqa: E402

PLIST = Path.home() / "Library/LaunchAgents/com.jobscout.daily.plist"

# Ceilings, not budgets: a stage that is still running this long has hung on a
# feed, and killing it is what lets the rest of the run happen. Ingest gets the
# most because Himalayas at 250 pages is a genuine three-hour crawl some days.
TIMEOUTS = {"ingest.py": 4 * 3600, "discover.py": 3600, "companies.py": 3600,
            "cities.py": 1800, "forms.py": 1800}
DEFAULT_TIMEOUT = 1800


def stage(script: str, *args: str) -> int:
    """One stage as a child process, with a timestamp and elapsed time either
    side of it, flushed immediately.

    Every print here and in the children used to be block-buffered when stdout
    was run.log, so the stage banners landed at the *end* of the log after the
    report, and a run killed mid-flight lost every line. `-u` on the child and
    flush=True here make the log a chronology again."""
    t0 = time.monotonic()
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n── {stamp} {script} {' '.join(args)}", flush=True)
    timeout = TIMEOUTS.get(script, DEFAULT_TIMEOUT)
    try:
        rc = subprocess.run([sys.executable, "-u", str(HERE / script), *args],
                            timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"   {script} killed after {timeout // 60} min -- hung, not slow",
              flush=True)
        rc = 124
    print(f"   {script}: exit {rc} in {time.monotonic() - t0:.0f}s", flush=True)
    return rc


def once(full: bool) -> int:
    failed = 0
    # Ingest is the only stage that touches the network. It isolates each
    # source internally, so a non-zero exit here is a real crash and counts.
    failed += 1 if stage("ingest.py") else 0
    # GeoNames is a monthly-ish dump, not a daily feed. Loaded only when asked
    # for a full pass, or when the table is empty -- geo.py silently falls back
    # to country precision without it, which is a quiet accuracy loss rather
    # than a failure, so it is worth checking here rather than hoping.
    with session() as conn:
        need_cities = full or not conn.execute(
            "SELECT COUNT(*) c FROM city").fetchone()[0]
    if need_cities:
        failed += 1 if stage("cities.py", "--load") else 0
    # Board discovery: a bounded batch per run, so the 1,157-employer backlog
    # clears over a few days without any single run taking an hour. Misses are
    # remembered, so the batch is always fresh candidates.
    failed += 1 if stage("discover.py", "--daily") else 0
    for script, args in (("normalise.py", ()),
                         # Ghost/re-stamp grading. Independent of everything
                         # below it -- it only reads job.posted_at/first_seen
                         # and closed-job history -- but sits ahead of score.py
                         # since score.py's own freshness component now reads
                         # the same basis() this stage uses.
                         ("freshness.py", ("--all",) if full else ()),
                         ("salary.py", ("--all",) if full else ()),
                         ("eligibility.py", ("--all",) if full else ()),
                         ("geo.py", ("--all",) if full else ()),
                         # fit reads eligibility and geo, and score reads fit.
                         # This order is load-bearing.
                         ("fit.py", ("--all",) if full else ()),
                         # Scoring reads all five of the above, so it always
                         # runs over everything -- a job whose eligibility or
                         # salary changed today needs its rank changed today.
                         ("score.py", ("--top", "0")),
                         # Company blurbs for whatever is now viable. Advert-only
                         # in the daily run: it is instant and needs no network,
                         # where the website fallback is a slow long shot.
                         ("companies.py", ("--fetch", "300", "--advert-only")),
                         # The advert's own application form for whatever is
                         # now near the top. Bounded per run and cached, so the
                         # backlog clears over a few mornings and a form is
                         # never fetched twice.
                         ("forms.py", ("--fetch", "40"))):
        failed += 1 if stage(script, *args) else 0
    # J2: is a shortlisted job's own ATS posting still live, per open-jobs'
    # crawler. Reads the score table, so it runs after score.py above. Off by
    # default (settings.yml); the stage itself prints "disabled" and exits 0
    # rather than run.py needing to know the setting.
    failed += 1 if stage("status_check.py") else 0
    return failed


def status() -> None:
    conn = connect()
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]   # noqa: E731

    open_jobs = q("SELECT COUNT(*) FROM job WHERE status = 'open'")
    print(f"\njobscout — {datetime.now(timezone.utc).astimezone():%a %d %b, %H:%M}\n")
    print(f"  postings   {q('SELECT COUNT(*) FROM posting'):>7,}")
    print(f"  jobs       {q('SELECT COUNT(*) FROM job'):>7,}   ({open_jobs:,} open)")
    print(f"  priced     {q('SELECT COUNT(*) FROM salary WHERE known=1'):>7,}")
    print(f"  decided    {q('SELECT COUNT(*) FROM eligibility'):>7,}")

    print("\n  by source")
    for r in conn.execute(
            "SELECT source, COUNT(*) n, MAX(last_seen) seen FROM posting "
            "GROUP BY source ORDER BY n DESC"):
        stale = (now() - (r["seen"] or 0)) // 3600
        flag = "  <- not seen in a day" if stale > 24 else ""
        print(f"    {r['source']:<16} {r['n']:>6,}   {stale:>3}h ago{flag}")

    print("\n  by eligibility")
    for r in conn.execute(
            "SELECT state, COUNT(*) n FROM eligibility GROUP BY state ORDER BY n DESC"):
        print(f"    {r['state']:<20} {r['n']:>6,}")

    print("\n  last run")
    for r in conn.execute(
            "SELECT stage, ok, detail, ended_at FROM run_log "
            "ORDER BY id DESC LIMIT 8"):
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"    {mark} {r['stage']:<22} {(r['detail'] or '')[:60]}")


def install() -> None:
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobscout.daily</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>{HERE / 'run.py'}</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>StandardOutPath</key><string>{HERE / 'run.log'}</string>
  <key>StandardErrorPath</key><string>{HERE / 'run.log'}</string>
</dict></plist>
"""
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist)
    subprocess.call(["launchctl", "unload", str(PLIST)],
                    stderr=subprocess.DEVNULL)
    subprocess.call(["launchctl", "load", str(PLIST)])
    print(f"installed {PLIST}\nruns daily at 07:30, logging to {HERE / 'run.log'}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--install", action="store_true")
    # J3: evidence-quoted fit scoring. Never part of the plain daily run --
    # it spends your Claude subscription usage on the top of the day's list, so it
    # only runs when explicitly asked for, here or by flipping judge.enabled.
    ap.add_argument("--judge", action="store_true",
                    help="also run judge.py after the daily pass")
    args = ap.parse_args(argv)

    if args.status:
        status()
        return 0
    if args.install:
        install()
        return 0

    started = now()
    print(f"\n{'═' * 62}\njobscout run — "
          f"{datetime.now(timezone.utc).astimezone():%a %d %b %Y, %H:%M:%S}", flush=True)
    failed = once(args.full)
    print(f"\n{'─' * 62}", flush=True)
    stage("report.py", "--limit", "15")
    # Anything sent and unanswered for a week is the first thing worth doing
    # tomorrow, ahead of any new job on the list.
    with session() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) c FROM application WHERE status = 'sent' "
            "AND outcome IS NULL AND sent_at <= ?", (now() - 7 * 86400,)).fetchone()["c"]
    if pending:
        stage("applied.py")
    if args.judge:
        stage("judge.py", "--force")
    # Fold the WAL back into the file now that no stage holds it open, and
    # VACUUM when a migration has asked for it. Without this the file only
    # ever grew.
    conn = connect()
    try:
        print(f"\nmaintenance: {maintain(conn)}", flush=True)
    finally:
        conn.close()
    print(f"\nrun finished in {now() - started}s"
          f"{f', {failed} stage(s) failed' if failed else ''}", flush=True)
    heartbeat()
    return 1 if failed else 0


def heartbeat() -> None:
    """Tell mz doctor's dead man's switch the run happened. It pings even when a
    stage failed: a failed stage already shows as a non-zero exit, and the
    switch exists to catch the other thing, a run that never started."""
    beat = HERE.parent / "metrics" / "heartbeat.py"
    if not beat.exists():
        return
    try:
        subprocess.run([sys.executable, str(beat), "ok", "jobscout"],
                       timeout=30, check=True)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"heartbeat call failed: {e}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
