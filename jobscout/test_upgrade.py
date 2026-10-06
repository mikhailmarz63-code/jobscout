#!/usr/bin/env python3
"""Tests for the 2026-09 upgrade pass: J1 ghost/re-stamp grading, J2 the
open-jobs feed and its /status sweep, J3 evidence-quoted fit scoring, J4 the
guard list over self-identification/work-authorisation/salary questions.

Same style as test_jobscout.py: a fresh temp database per test class, network
calls mocked by reassigning the module's own `get`/`subprocess.run` reference
rather than a mocking framework.

    python3 test_upgrade.py
    python3 test_upgrade.py -v
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=ResourceWarning)

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import forms                        # noqa: E402
import freshness                    # noqa: E402
import judge                        # noqa: E402
import kit                          # noqa: E402
import score                        # noqa: E402
import status_check                 # noqa: E402
import views                        # noqa: E402
from db import connect, now         # noqa: E402
from sources import Posting         # noqa: E402
import sources.openjobs as openjobs # noqa: E402

_TMPDIR = tempfile.TemporaryDirectory(prefix="jobscout-upgrade-tests-")
_TMP_N = 0
_CONNS: list = []


def temp_db():
    global _TMP_N
    _TMP_N += 1
    conn = connect(Path(_TMPDIR.name) / f"u{_TMP_N}.db")
    _CONNS.append(conn)
    return conn


def tearDownModule():
    for conn in _CONNS:
        conn.close()


def make_job(conn, job_id: int, *, posted_at=None, first_seen=0, last_seen=0,
             title="Backend Engineer", status="open") -> None:
    conn.execute(
        "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
        "posted_at, first_seen, last_seen, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, f"k{job_id}|t", title, "Acme", "acme", posted_at, first_seen,
         last_seen, status))


# ---------------------------------------------------------------------- J1 --

class TestFreshnessBasis(unittest.TestCase):
    """The bug this replaces: `posted or first_seen` trusted a source's own
    date outright. `basis()` takes the older of the two instead."""

    def test_no_posted_date_falls_back_to_first_seen(self):
        self.assertEqual(freshness.basis(None, 1000), 1000)

    def test_posted_older_than_first_seen_is_the_normal_case(self):
        """A job posted long before we found it -- the ordinary shape."""
        self.assertEqual(freshness.basis(100, 1000), 100)

    def test_posted_newer_than_first_seen_is_a_restamp_and_is_not_trusted(self):
        """This is the regression: a bumped posted_at must not win."""
        self.assertEqual(freshness.basis(9000, 1000), 1000)


class TestFreshnessClassify(unittest.TestCase):
    def test_within_par_is_fresh(self):
        self.assertEqual(freshness.classify(5, 30, None), "fresh")

    def test_older_than_par_is_stale(self):
        self.assertEqual(freshness.classify(45, 30, None), "stale")

    def test_over_absolute_ceiling_is_ghost_even_under_multiple(self):
        self.assertEqual(freshness.classify(150, 1000, None), "ghost")

    def test_over_double_the_par_is_ghost(self):
        self.assertEqual(freshness.classify(70, 30, None), "ghost")

    def test_restamp_flag_beats_a_merely_stale_age(self):
        self.assertEqual(freshness.classify(45, 30, 5), "restamped")

    def test_ghost_outranks_a_restamp_flag(self):
        """Open far too long is worse than merely re-dated -- ghost wins."""
        self.assertEqual(freshness.classify(200, 30, 5), "ghost")

    def test_restamp_under_the_two_day_threshold_is_not_flagged(self):
        self.assertEqual(freshness.classify(5, 30, 1), "fresh")


class TestFreshnessPar(unittest.TestCase):
    def test_no_closed_jobs_uses_the_global_default(self):
        conn = temp_db()
        par, source = freshness.par_for(conn, "Backend Engineer")
        self.assertEqual(source, "global")
        self.assertEqual(par, freshness.GLOBAL_DEFAULT_PAR)

    def test_family_with_enough_history_wins_over_global(self):
        conn = temp_db()
        # 20 backend-engineer jobs, closed after a long, consistent run --
        # enough to clear FAMILY_MIN_N and produce a par far from the default.
        for i in range(20):
            make_job(conn, i + 1, first_seen=0, last_seen=200 * 86400,
                     title="Backend Engineer", status="closed")
        conn.commit()
        par, source = freshness.par_for(conn, "Backend Engineer II")
        self.assertEqual(source, "family")
        self.assertGreater(par, 100)

    def test_too_few_closed_jobs_in_a_family_falls_back_to_global(self):
        conn = temp_db()
        for i in range(5):
            make_job(conn, i + 1, first_seen=0, last_seen=200 * 86400,
                     title="Backend Engineer", status="closed")
        conn.commit()
        _, source = freshness.par_for(conn, "Backend Engineer")
        self.assertEqual(source, "global")


class TestFreshnessApply(unittest.TestCase):
    def test_writes_one_row_per_open_job_and_skips_closed_ones(self):
        conn = temp_db()
        make_job(conn, 1, first_seen=now() - 5 * 86400, last_seen=now())
        make_job(conn, 2, first_seen=now() - 200 * 86400, last_seen=now(),
                 title="Ghost Role")
        make_job(conn, 3, first_seen=0, last_seen=0, status="closed")
        conn.commit()
        stats = freshness.apply(conn)
        self.assertEqual(stats["fresh"] + stats["stale"] + stats["ghost"], 2)
        rows = {r["job_id"]: r["verdict"] for r in
                conn.execute("SELECT job_id, verdict FROM freshness")}
        self.assertEqual(rows[1], "fresh")
        self.assertEqual(rows[2], "ghost")
        self.assertNotIn(3, rows)

    def test_restamped_job_is_flagged_from_its_own_dates(self):
        conn = temp_db()
        seen = now() - 30 * 86400
        make_job(conn, 1, posted_at=now() - 1 * 86400, first_seen=seen,
                 last_seen=now())
        conn.commit()
        freshness.apply(conn)
        row = conn.execute("SELECT verdict FROM freshness WHERE job_id = 1").fetchone()
        self.assertEqual(row["verdict"], "restamped")


class TestScoreFreshnessRegression(unittest.TestCase):
    """score.py:167 used to read `posted or first_seen`, so a re-dated repost
    scored as brand new however long it had really been open."""

    def test_a_bumped_posted_at_does_not_score_as_fresh(self):
        old_first_seen = now() - 90 * 86400
        bumped_posted_at = now() - 1 * 86400
        value, why = score.freshness(bumped_posted_at, old_first_seen)
        self.assertEqual(value, 0.0)
        self.assertIn("90", why)

    def test_a_genuinely_new_job_still_scores_fresh(self):
        seen = now() - 1 * 86400
        value, _ = score.freshness(seen, seen)
        self.assertEqual(value, 1.0)


class TestBoardGhostToggle(unittest.TestCase):
    """views.jobs() hides ghosts by default; show_ghost surfaces them."""

    def _seed(self, conn):
        make_job(conn, 1, first_seen=now() - 200 * 86400, last_seen=now(),
                 title="Ghost Role")
        conn.execute(
            "INSERT INTO eligibility (job_id, state, evidence_quote, "
            "evidence_url, rule, decided_by, decided_at) "
            "VALUES (1, 'OPEN_WORLDWIDE', 'anywhere', 'https://x', 'rule', 'rule', ?)",
            (now(),))
        conn.execute(
            "INSERT INTO fit (job_id, work_mode, reach, viable, viable_why, "
            "decided_at) VALUES (1, 'remote', 'likely', 1, 'ok', ?)", (now(),))
        conn.commit()
        freshness.apply(conn)

    def test_ghost_hidden_by_default(self):
        conn = temp_db()
        self._seed(conn)
        items, total = views.jobs(conn)
        self.assertEqual(total, 0)

    def test_ghost_shown_when_asked(self):
        conn = temp_db()
        self._seed(conn)
        items, total = views.jobs(conn, show_ghost=True)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["verdict"], "ghost")


# ---------------------------------------------------------------------- J2 --

class TestOpenJobsIdealText(unittest.TestCase):
    def test_no_personal_data_leaves_in_the_ideal_jd(self):
        """Only settings.yml's openjobs section may shape this text -- never
        his name, email or phone, and never CV text."""
        conf = {"title": "AI Solutions Engineer",
               "must_haves": ["Python, TypeScript, SQL"],
               "nice_to_haves": [".NET"]}
        text, title, location = openjobs._ideal_text(conf)
        self.assertNotIn("alex@example.com", text.lower())
        self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.\w+")
        self.assertNotRegex(text, r"\+94")
        self.assertIn("Python", text)
        self.assertEqual(title, "AI Solutions Engineer")


class TestHalfFloatDecode(unittest.TestCase):
    def test_round_trips_known_values(self):
        raw = struct.pack("<3e", 1.0, -2.5, 0.0)
        self.assertEqual(openjobs._decode_half_floats(raw), [1.0, -2.5, 0.0])


class TestNearestLeaves(unittest.TestCase):
    def test_picks_the_closer_centroid_first(self):
        # Two 2-dim leaves: one aligned with the ideal vector, one orthogonal.
        manifest = {"dims": 2, "tree": [
            {"id": 0, "children": None, "label": "a"},
            {"id": 1, "children": None, "label": "b"},
            {"id": 2, "children": [0, 1], "label": "root"},   # not a leaf
        ]}
        centroids = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        leaves = openjobs.nearest_leaves(manifest, centroids, [1.0, 0.0], k=1)
        self.assertEqual(leaves[0]["id"], 0)


class TestOpenJobsEmbedCache(unittest.TestCase):
    def test_second_call_uses_the_cache_not_the_network(self):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return {"vector": [1.0, 0.0], "recipe": "r1"}

        real_get, real_cache, real_ideal = openjobs.get, openjobs.CACHE, openjobs.IDEAL_CACHE
        tmp = Path(tempfile.mkdtemp(prefix="openjobs-cache-"))
        openjobs.get = fake_get
        openjobs.CACHE = tmp
        openjobs.IDEAL_CACHE = tmp / "ideal.json"
        try:
            conf = {"title": "X", "must_haves": ["a"], "nice_to_haves": []}
            cfg = {"sources": {"openjobs": conf}}
            first = openjobs.embed(cfg)
            second = openjobs.embed(cfg)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["vector"], second["vector"])
        finally:
            openjobs.get, openjobs.CACHE, openjobs.IDEAL_CACHE = real_get, real_cache, real_ideal


class TestOpenJobsFetch(unittest.TestCase):
    def test_fetch_returns_postings_keyed_in_the_status_shape(self):
        manifest = {"dims": 1, "groups": "g/", "tree": [
            {"id": 0, "children": None, "label": "a"}]}
        group = {"jobs": [
            {"ats": "greenhouse", "slug": "acme", "id": "123",
             "title": "Backend Engineer", "company": "Acme",
             "url": "https://boards.greenhouse.io/acme/jobs/123",
             "location": "Remote", "jd": "Full description here.",
             "pub": 1700000000000},
        ]}
        responses = {
            "https://backend.dehnbostele.workers.dev/embed": {"vector": [1.0], "recipe": "r"},
            "https://backend.dehnbostele.workers.dev/data/manifest.json": manifest,
            "https://backend.dehnbostele.workers.dev/data/g/0.json": group,
        }

        def fake_get(url, *, cfg=None, json_body=None, binary=False, **kw):
            if binary:
                return struct.pack("<1e", 1.0)
            return responses.get(url)

        real_get = openjobs.get
        tmp = Path(tempfile.mkdtemp(prefix="openjobs-fetch-"))
        openjobs.get, openjobs.CACHE, openjobs.IDEAL_CACHE = fake_get, tmp, tmp / "ideal.json"
        try:
            cfg = {"sources": {"openjobs": {"title": "X", "top_groups": 1,
                                            "must_haves": [], "nice_to_haves": []}}}
            out = openjobs.fetch(cfg)
        finally:
            openjobs.get = real_get
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source_id, "greenhouse/acme#123")
        self.assertEqual(out[0].source, "openjobs")
        self.assertEqual(out[0].description, "Full description here.")


class TestStatusKey(unittest.TestCase):
    def test_openjobs_sourced_posting_uses_its_own_key_verbatim(self):
        self.assertEqual(
            status_check.status_key("openjobs", "greenhouse/acme#123", "https://x"),
            "greenhouse/acme#123")

    def test_greenhouse_key_from_url(self):
        key = status_check.status_key(
            "ats", "greenhouse:456",
            "https://boards.greenhouse.io/acme/jobs/456")
        self.assertEqual(key, "greenhouse/acme#456")

    def test_lever_key_from_url(self):
        key = status_check.status_key(
            "ats", "lever:abc-123",
            "https://jobs.lever.co/acme/abc-123")
        self.assertEqual(key, "lever/acme#abc-123")

    def test_unsupported_vendor_returns_none_rather_than_guess(self):
        self.assertIsNone(status_check.status_key(
            "ats", "workable:xyz", "https://apply.workable.com/acme/j/xyz"))

    def test_no_colon_in_source_id_returns_none(self):
        self.assertIsNone(status_check.status_key("himalayas", "999", "https://x"))


class TestStatusSweep(unittest.TestCase):
    def _seed(self, conn):
        make_job(conn, 1, first_seen=0, last_seen=0)
        make_job(conn, 2, first_seen=0, last_seen=0)
        conn.execute(
            "INSERT INTO posting (source, source_id, url, title, company, "
            "first_seen, last_seen, job_id, raw) VALUES "
            "('ats','greenhouse:1','https://boards.greenhouse.io/acme/jobs/1',"
            "'T','Acme',0,0,1,'{}')")
        conn.execute(
            "INSERT INTO posting (source, source_id, url, title, company, "
            "first_seen, last_seen, job_id, raw) VALUES "
            "('ats','greenhouse:2','https://boards.greenhouse.io/acme/jobs/2',"
            "'T','Acme',0,0,2,'{}')")
        conn.execute("INSERT INTO score (job_id, total, breakdown, scored_at) "
                     "VALUES (1, 90, '{}', ?)", (now(),))
        conn.execute("INSERT INTO score (job_id, total, breakdown, scored_at) "
                     "VALUES (2, 80, '{}', ?)", (now(),))
        conn.commit()

    def test_removed_status_closes_the_job(self):
        conn = temp_db()
        self._seed(conn)
        real_get = status_check.get

        def fake_get(url, *, cfg=None, json_body=None, **kw):
            return {"statuses": {"greenhouse/acme#1": {"status": "removed"},
                                 "greenhouse/acme#2": {"status": "open"}}}
        status_check.get = fake_get
        try:
            result = status_check.sweep(conn, {"sources": {"openjobs": {"status_sweep": {}}}})
        finally:
            status_check.get = real_get
        self.assertEqual(result["closed"], 1)
        row = conn.execute("SELECT status FROM job WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "closed")
        row2 = conn.execute("SELECT status FROM job WHERE id = 2").fetchone()
        self.assertEqual(row2["status"], "open")

    def test_dry_run_writes_nothing(self):
        conn = temp_db()
        self._seed(conn)
        real_get = status_check.get
        status_check.get = lambda *a, **k: {
            "statuses": {"greenhouse/acme#1": {"status": "removed"}}}
        try:
            status_check.sweep(conn, {"sources": {"openjobs": {"status_sweep": {}}}},
                               dry_run=True)
        finally:
            status_check.get = real_get
        row = conn.execute("SELECT status FROM job WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "open")

    def test_network_failure_is_raised_not_swallowed(self):
        """The stage must log loudly -- see main()'s except block -- not
        silently do nothing the way a single ingest source is allowed to."""
        conn = temp_db()
        self._seed(conn)
        real_get = status_check.get
        status_check.get = lambda *a, **k: None
        try:
            with self.assertRaises(RuntimeError):
                status_check.sweep(conn, {"sources": {"openjobs": {"status_sweep": {}}}})
        finally:
            status_check.get = real_get

    def test_disabled_by_default_does_nothing(self):
        rc = status_check.main([])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------- J3 --

class TestQuoteVerified(unittest.TestCase):
    def test_exact_substring_passes(self):
        self.assertTrue(judge.quote_verified("led the migration",
                                             "In 2025 I led the migration to microservices."))

    def test_whitespace_differences_are_ignored(self):
        self.assertTrue(judge.quote_verified("led   the\nmigration",
                                             "I led the migration."))

    def test_fabricated_quote_fails(self):
        self.assertFalse(judge.quote_verified("built the entire platform solo",
                                              "I helped maintain a Django app."))

    def test_empty_quote_fails(self):
        self.assertFalse(judge.quote_verified("", "anything"))


class TestParseAndVerify(unittest.TestCase):
    def test_verified_rating_kept_fabricated_one_discarded(self):
        cv = "I built automations in Python using n8n for two years."
        advert = "Must have Python experience. Must have led a team of 10."
        reply = json.dumps({
            "criteria": [
                {"name": "Python", "rating": 4, "cv_quote": "built automations in Python",
                 "advert_quote": "Must have Python experience", "note": "solid"},
                {"name": "Team leadership", "rating": 3,
                 "cv_quote": "led a team of ten engineers to victory",
                 "advert_quote": "Must have led a team of 10", "note": "made up"},
            ],
            "summary": "Reasonable technical fit.",
        })
        result = judge.parse_and_verify(reply, cv, advert)
        by_name = {c["name"]: c for c in result["criteria"]}
        self.assertTrue(by_name["Python"]["verified"])
        self.assertEqual(by_name["Python"]["rating"], 4)
        self.assertFalse(by_name["Team leadership"]["verified"])
        self.assertIsNone(by_name["Team leadership"]["rating"])
        self.assertIn("[unverified]", by_name["Team leadership"]["note"])

    def test_reply_with_no_json_object_is_a_parse_error(self):
        result = judge.parse_and_verify("sorry, I can't do that", "cv", "advert")
        self.assertIn("parse_error", result)
        self.assertEqual(result["criteria"], [])


class TestCallClaude(unittest.TestCase):
    def test_parses_the_cli_json_result_field(self):
        import subprocess

        class FakeProc:
            stdout = json.dumps({"result": '{"criteria": []}', "is_error": False})

        real_run = subprocess.run
        subprocess.run = lambda *a, **k: FakeProc()
        judge.subprocess.run = subprocess.run
        try:
            reply, error = judge.call_claude("prompt", "sonnet")
        finally:
            subprocess.run = real_run
            judge.subprocess.run = real_run
        self.assertEqual(error, "")
        self.assertEqual(reply, '{"criteria": []}')

    def test_is_error_response_returns_an_error_not_a_reply(self):
        class FakeProc:
            stdout = json.dumps({"result": "Not logged in", "is_error": True})
        real_run = judge.subprocess.run
        judge.subprocess.run = lambda *a, **k: FakeProc()
        try:
            reply, error = judge.call_claude("prompt", "sonnet")
        finally:
            judge.subprocess.run = real_run
        self.assertIsNone(reply)
        self.assertIn("Not logged in", error)

    def test_missing_cli_binary_is_reported_not_raised(self):
        def boom(*a, **k):
            raise FileNotFoundError()
        real_run = judge.subprocess.run
        judge.subprocess.run = boom
        try:
            reply, error = judge.call_claude("prompt", "sonnet")
        finally:
            judge.subprocess.run = real_run
        self.assertIsNone(reply)
        self.assertIn("PATH", error)


class TestJudgeCaching(unittest.TestCase):
    def test_a_cached_result_never_calls_claude_again(self):
        conn = temp_db()
        make_job(conn, 1, first_seen=0, last_seen=0)
        conn.commit()
        variant, cv, cv_hash = judge.cv_text_and_hash("Backend Engineer", "")
        cached = {"criteria": [], "summary": "cached"}
        conn.execute(
            "INSERT INTO agent_call (job_id, purpose, backend, prompt, reply, "
            "choice, confidence, reasoning, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (1, f"fit_judge:{cv_hash}", "claude-cli", "p", "r", "n/a", None,
             json.dumps(cached), now()))
        conn.commit()

        def boom(*a, **k):
            raise AssertionError("should not call claude for a cached pair")
        real_call = judge.call_claude
        judge.call_claude = boom
        try:
            result = judge.judge_one(conn, 1, "Backend Engineer", "Acme", "sonnet")
        finally:
            judge.call_claude = real_call
        self.assertEqual(result["summary"], "cached")


# ---------------------------------------------------------------------- J4 --

class TestSelfIdRouting(unittest.TestCase):
    def test_gender_race_veteran_disability_all_route_to_self_id(self):
        for label in ("Gender", "Race/Ethnicity", "Veteran Status",
                     "Do you have a disability?", "I identify my gender as"):
            with self.subTest(label=label):
                self.assertEqual(kit.route(label), "self_id")

    def test_self_id_answer_is_never_invented(self):
        """Empty, like any other demographic question with no route at all
        (test_jobscout.py's TestKit already covers that one) -- GUARD_KINDS in
        answers_for_labels() is what forces needs_you/no-choice downstream."""
        text = kit.answer_for(None, {"id": None}, {"label": "Gender", "values": []},
                              kit.context_generic())
        self.assertEqual(text, "")


class TestGuardKindsNeverAutoFilled(unittest.TestCase):
    """The bug this closes: `answers_for_labels()` handed the extension a
    `choice` for sponsorship/salary whenever the computed text happened to
    equal one of the field's own options, and fillSafe() would one-click it."""

    def test_sponsorship_dropdown_never_gets_a_choice_or_is_marked_answered(self):
        fields = [{"label": "Do you require visa sponsorship?",
                  "type": "select",
                  "options": ["I already have the right to work",
                             "I would need visa sponsorship support"]}]
        out = kit.answers_for_labels_generic(fields)
        item = out["answers"][0]
        self.assertIsNone(item["choice"])
        self.assertTrue(item["needs_you"])

    def test_self_id_dropdown_never_gets_a_choice(self):
        fields = [{"label": "Gender", "type": "select",
                  "options": ["Male", "Female", "Prefer not to say"]}]
        out = kit.answers_for_labels_generic(fields)
        item = out["answers"][0]
        self.assertIsNone(item["choice"])
        self.assertTrue(item["needs_you"])

    def test_salary_expectation_is_guarded(self):
        fields = [{"label": "What are your salary expectations?", "type": "input_text"}]
        out = kit.answers_for_labels_generic(fields)
        item = out["answers"][0]
        self.assertTrue(item["needs_you"])
        self.assertIsNone(item["choice"])


class TestFormsFlagsGuardList(unittest.TestCase):
    def test_self_identification_is_flagged(self):
        rows = [{"label": "Gender", "required": False, "field_type": "select",
                "values": []}]
        _, flags = forms.classify(rows)
        self.assertTrue(any("self-identification" in f for f in flags))

    def test_salary_expectation_is_flagged(self):
        rows = [{"label": "What are your salary expectations?", "required": False,
                "field_type": "input_text", "values": []}]
        _, flags = forms.classify(rows)
        self.assertTrue(any("salary expectation" in f for f in flags))


class TestRepeatedQuestionsPanel(unittest.TestCase):
    def test_grouped_counted_and_unanswered_sorts_first(self):
        conn = temp_db()
        make_job(conn, 1, first_seen=0, last_seen=0)
        make_job(conn, 2, first_seen=0, last_seen=0)
        rows = [
            (1, 0, "Gender", 0, "select", "[]", "greenhouse", now()),
            (2, 0, "  gender  ", 1, "select", "[]", "greenhouse", now()),   # same q, different case/space
            (1, 1, "Why do you want to work here?", 0, "text", "[]", "greenhouse", now()),
        ]
        conn.executemany(
            "INSERT INTO form_question (job_id, position, label, required, "
            "field_type, values_json, vendor, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        conn.commit()
        items = views.repeated_questions(conn)
        gender = next(i for i in items if i["label"].strip().lower() == "gender")
        self.assertEqual(gender["count"], 2)
        self.assertEqual(gender["required"], 1)
        self.assertTrue(gender["guarded"])
        self.assertFalse(gender["answered"])
        # Unanswered items sort ahead of answered ones.
        self.assertFalse(items[0]["answered"])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
