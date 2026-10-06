#!/usr/bin/env python3
"""Tests. Run before trusting anything the system says.

Weighted towards the two places a bug is expensive rather than annoying:

  * **the eligibility gate** -- a false permissive is an application to a job the
    candidate cannot take, sent by a machine, to an employer who now has his name;
  * **salary parsing** -- a figure out by 12x lands at the top of the list and
    is acted on first.

Almost every case below is a real defect this code had, kept as a test so it
cannot come back. Where that is so, the docstring says which.

    python3 test_jobscout.py
    python3 test_jobscout.py -v
    python3 test_jobscout.py TestEligibility
"""
from __future__ import annotations

import warnings

# Set before the local imports, because the connections that raise it are made
# during import. Each test wants a database nobody else has touched and the
# process is over in a second, so the temporary connections are deliberately
# left open -- one ResourceWarning per test and nothing else. Silenced here so
# a warning that matters stays visible in the output.
warnings.filterwarnings("ignore", category=ResourceWarning)

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import eligibility as elig          # noqa: E402
import applied                      # noqa: E402
import companies as companies_mod   # noqa: E402
import cities                       # noqa: E402
import fit                          # noqa: E402
import forms                        # noqa: E402
import geo                          # noqa: E402
import kit                          # noqa: E402
import normalise as norm            # noqa: E402
import salary as sal                # noqa: E402
import sources                      # noqa: E402
import score                        # noqa: E402
from db import connect, now         # noqa: E402
from sources import Posting, epoch, strip_html   # noqa: E402

RATES = {"USD": 1.0, "EUR": 1.1, "GBP": 1.27, "INR": 0.012, "PLN": 0.25}


# One directory for every per-test database, removed when the process ends.
# NamedTemporaryFile(delete=False) used to leave 200+ files behind per run.
_TMPDIR = tempfile.TemporaryDirectory(prefix="jobscout-tests-")
_TMP_N = 0


_CONNS: list = []


def temp_db():
    global _TMP_N
    _TMP_N += 1
    conn = connect(Path(_TMPDIR.name) / f"t{_TMP_N}.db")
    _CONNS.append(conn)
    return conn


def tearDownModule():
    for conn in _CONNS:
        conn.close()


# ------------------------------------------------------------- the schema --

class TestSchemaGate(unittest.TestCase):
    """The CHECK constraints are the system's actual guarantees. If these pass
    but the Python is wrong, the database still refuses the bad row."""

    def setUp(self):
        self.conn = temp_db()
        self.conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "first_seen, last_seen) VALUES (1, 'k|t', 'T', 'C', 'c', 0, 0)")

    def _elig(self, state, quote, url, rule):
        self.conn.execute(
            "INSERT INTO eligibility (job_id, state, evidence_quote, evidence_url,"
            " rule, decided_by, confidence, decided_at) VALUES (1,?,?,?,?,'rule',1,0)",
            (state, quote, url, rule))

    def test_sendable_state_requires_evidence(self):
        for state in elig.SENDABLE:
            with self.subTest(state=state):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._elig(state, None, None, None)
                self.conn.rollback()

    def test_sendable_state_requires_url_not_just_quote(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._elig("OPEN_WORLDWIDE", "says anywhere", None, "r")

    def test_sendable_state_rejects_whitespace_evidence(self):
        """'   ' is not evidence. length(trim(...)) > 0, not just NOT NULL."""
        with self.assertRaises(sqlite3.IntegrityError):
            self._elig("OPEN_WORLDWIDE", "   ", "  ", "r")

    def test_sendable_state_accepts_real_evidence(self):
        self._elig("OPEN_WORLDWIDE", "location field: Anywhere in the World",
                   "https://x/1", "location:worldwide")
        self.assertEqual(
            self.conn.execute("SELECT state FROM eligibility").fetchone()["state"],
            "OPEN_WORLDWIDE")

    def test_unknown_needs_no_evidence(self):
        """UNKNOWN is the review queue, not a claim. It must be writable with
        nothing attached, or the queue cannot exist."""
        self._elig("UNKNOWN", None, None, None)

    def test_blocked_needs_no_evidence(self):
        """BLOCKED removes a job. That is a safe direction to be wrong in."""
        self._elig("BLOCKED", None, None, None)

    def test_invalid_state_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._elig("PROBABLY_FINE", "q", "u", "r")

    def _salary(self, known, lo, hi):
        self.conn.execute(
            "INSERT INTO salary (job_id, known, min_usd_month, max_usd_month, "
            "decided_at) VALUES (1, ?, ?, ?, 0)", (known, lo, hi))

    def test_known_salary_requires_a_number(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._salary(1, None, None)

    def test_unknown_salary_cannot_carry_a_number(self):
        """The other direction matters too: known=0 with a figure attached means
        something downstream will read the figure."""
        with self.assertRaises(sqlite3.IntegrityError):
            self._salary(0, 5000, 6000)

    def test_absurd_salary_rejected(self):
        """This constraint caught 52 real rows on the first live run."""
        with self.assertRaises(sqlite3.IntegrityError):
            self._salary(1, 1000, 30_000_000)

    def test_negative_salary_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._salary(1, -100, 500)


# ------------------------------------------------------------- the gate --

class TestEligibility(unittest.TestCase):

    def d(self, loc="", desc="", title="Engineer", url="https://x/1",
          tags=None, trusted=True):
        return elig.decide(loc, desc, title, url, tags or [],
                           location_trusted=trusted)

    # --- the structured path -------------------------------------------------

    def test_worldwide_field(self):
        self.assertEqual(self.d(loc="Anywhere in the World").state,
                         "OPEN_WORLDWIDE")

    def test_sri_lanka_beats_everything(self):
        """He is in the country: no visa, no timezone compromise, best case."""
        self.assertEqual(self.d(loc="Colombo, Sri Lanka").state, "ONSITE_LK")

    def test_asia_region_is_open(self):
        self.assertEqual(self.d(loc="APAC").state, "OPEN_REGION")
        self.assertEqual(self.d(loc="South Asia").state, "OPEN_REGION")

    def test_malaysia_is_not_asia_inclusive(self):
        """'Malaysia' contains the substring 'asia'. Plain `in` matching made a
        Kuala Lumpur role open to South Asia. _find is word-boundary matched."""
        self.assertEqual(self.d(loc="Malaysia").state, "BLOCKED")

    def test_us_only_is_blocked(self):
        self.assertEqual(self.d(loc="United States").state, "BLOCKED")

    def test_bare_us_code_is_blocked(self):
        """'Remote (US)' reduces to two letters after the generic strip, so the
        named-place catch-all cannot see it."""
        self.assertEqual(self.d(loc="Remote (US)").state, "BLOCKED")

    def test_emea_goes_to_review_not_blocked(self):
        """Sri Lanka is not in EMEA, but EMEA postings hire there in practice.
        Both confident readings are wrong often enough to be dangerous."""
        self.assertEqual(self.d(loc="EMEA").state, "UNKNOWN")

    def test_named_place_blocks(self):
        self.assertEqual(self.d(loc="Dresden, Sachsen, Deutschland").state,
                         "BLOCKED")
        self.assertEqual(self.d(loc="New York, NY (HQ); Remote").state, "BLOCKED")

    def test_bare_remote_is_not_a_place(self):
        """'Remote' names nowhere, so it must not trigger the catch-all."""
        self.assertNotEqual(self.d(loc="Remote").rule, "location:named_place")

    def test_currency_in_location_is_not_a_place(self):
        """A Hacker News header put '$100-140K USD' in the location slot and
        'USD' was read as a three-letter place name."""
        self.assertNotEqual(self.d(loc="Full-time | $100-140K USD").state,
                            "BLOCKED")

    def test_untrusted_location_skips_the_catch_all(self):
        """HN's 'location' is whatever landed third in a pipe-separated line."""
        self.assertNotEqual(
            self.d(loc="San Francisco, CA", trusted=False).rule,
            "location:named_place")

    # --- the prose path ------------------------------------------------------

    def test_worldwide_text(self):
        d = self.d(desc="We hire from anywhere in the world; work where you like.")
        self.assertEqual(d.state, "OPEN_WORLDWIDE")
        self.assertTrue(d.quote and d.url and d.rule)

    def test_product_copy_is_not_a_hiring_policy(self):
        """Replit: 'the Apps and Agents they ship can transact reliably anywhere
        in the world' was promoted to a sendable state."""
        d = self.d(desc="Our platform lets builders transact reliably anywhere "
                        "in the world with one API call.")
        self.assertNotEqual(d.state, "OPEN_WORLDWIDE")

    def test_negated_sponsorship_is_not_sponsorship(self):
        """'We do not offer visa sponsorship' contains 'visa sponsorship'."""
        d = self.d(desc="Please note we do not offer visa sponsorship for this role.")
        self.assertNotEqual(d.state, "ONSITE_SPONSORED")

    def test_real_sponsorship_is_found(self):
        d = self.d(desc="Competitive relocation packages and visa sponsorship "
                        "where necessary for the right candidate.")
        self.assertEqual(d.state, "ONSITE_SPONSORED")

    def test_generic_benefits_boilerplate_is_not_sponsorship(self):
        """Mistral: 'Benefits vary by country and may include healthcare
        coverage, parental leave, relocation support' promises nobody a visa."""
        d = self.d(desc="Benefits vary by country and may include healthcare "
                        "coverage, parental leave and relocation support.")
        self.assertNotEqual(d.state, "ONSITE_SPONSORED")

    def test_contractor_lane(self):
        d = self.d(desc="You will be engaged through an employer of record, so "
                        "we can hire team members in most countries.")
        self.assertEqual(d.state, "OPEN_CONTRACTOR")

    # --- contradictions ------------------------------------------------------

    def test_worldwide_tag_contradicted_by_body_goes_to_review(self):
        """We Work Remotely lets a poster tag 'Anywhere in the World' and then
        require US work authorisation three paragraphs down."""
        d = self.d(loc="Anywhere in the World",
                   desc="Great role. You must be legally authorized to work in "
                        "the US. We offer equity.")
        self.assertEqual(d.state, "UNKNOWN")
        self.assertIn("CONTRADICTION", d.quote)

    def test_clean_worldwide_survives_the_contradiction_check(self):
        d = self.d(loc="Anywhere in the World",
                   desc="A normal advert. Remote work, flexible hours.")
        self.assertEqual(d.state, "OPEN_WORLDWIDE")

    # --- the invariant -------------------------------------------------------

    def test_every_sendable_decision_carries_evidence(self):
        """The property the whole gate rests on, asserted over every rule."""
        cases = [
            {"loc": "Anywhere in the World"}, {"loc": "Colombo, Sri Lanka"},
            {"loc": "APAC"}, {"desc": "We hire from anywhere in the world."},
            {"desc": "You will work through an employer of record."},
            {"desc": "Visa sponsorship is available for this position."},
        ]
        for case in cases:
            with self.subTest(**case):
                d = self.d(**case)
                if d.state in elig.SENDABLE:
                    self.assertTrue(d.quote.strip(), "no evidence quote")
                    self.assertTrue(d.url.strip(), "no evidence url")
                    self.assertTrue(d.rule.strip(), "no rule name")

    def test_nothing_known_is_unknown(self):
        self.assertEqual(self.d(loc="", desc="").state, "UNKNOWN")


class TestEligibilityWritePath(unittest.TestCase):
    """apply() must never write a sendable state it cannot evidence."""

    def test_evidenceless_sendable_downgrades_to_unknown(self):
        conn = temp_db()
        conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "description, canonical_url, first_seen, last_seen) "
            "VALUES (1, 'k|t', 'Engineer', 'C', 'c', "
            "'We hire from anywhere in the world.', '', 0, 0)")
        conn.commit()
        elig.apply(conn)
        row = conn.execute("SELECT state FROM eligibility").fetchone()
        # The rule fires, but there is no URL to cite, so it cannot be sendable.
        self.assertEqual(row["state"], "UNKNOWN")

    def test_human_decisions_are_not_overwritten(self):
        conn = temp_db()
        conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "description, canonical_url, first_seen, last_seen) "
            "VALUES (1,'k|t','E','C','c','Remote, United States','https://x/1',0,0)")
        conn.execute(
            "INSERT INTO eligibility (job_id, state, evidence_quote, evidence_url,"
            " rule, decided_by, confidence, decided_at) "
            "VALUES (1,'OPEN_WORLDWIDE','I emailed and asked','https://x/1',"
            "'human','human',1.0,0)")
        conn.commit()
        elig.apply(conn, redo=True)
        self.assertEqual(
            conn.execute("SELECT state FROM eligibility").fetchone()["state"],
            "OPEN_WORLDWIDE")


# ----------------------------------------------------------------- salary --

class TestSalary(unittest.TestCase):

    def s(self, raw=None, desc="", ):
        return sal.parse(raw or {}, desc, RATES)

    # --- rule 1: missing is not zero ----------------------------------------

    def test_remoteok_zero_means_unknown(self):
        """RemoteOK sends salary_min: 0 for 'not stated'. Read literally it
        buries a third of the feed at the bottom of the list."""
        r = self.s({"min": 0, "max": 0, "currency": "USD", "period": "year"})
        self.assertFalse(r.known)
        self.assertIsNone(r.min_usd_month)

    def test_null_means_unknown(self):
        self.assertFalse(self.s({"min": None, "max": None}).known)

    def test_missing_field_means_unknown(self):
        self.assertFalse(self.s({}).known)

    # --- conversion ----------------------------------------------------------

    def test_annual_usd_to_monthly(self):
        r = self.s({"min": 120000, "max": 180000, "currency": "USD", "period": "year"})
        self.assertTrue(r.known)
        self.assertAlmostEqual(r.min_usd_month, 10000, places=0)
        self.assertAlmostEqual(r.max_usd_month, 15000, places=0)

    def test_currency_conversion(self):
        r = self.s({"min": 60000, "max": 60000, "currency": "GBP", "period": "year"})
        self.assertAlmostEqual(r.min_usd_month, 60000 * 1.27 / 12, places=0)

    def test_hourly_contract_rate(self):
        r = self.s({"min": 150, "max": 250, "currency": "USD", "period": "hour"})
        self.assertAlmostEqual(r.min_usd_month, 150 * (40 * 52 / 12), places=0)

    # --- rule 3: ambiguity is unknown ---------------------------------------

    def test_ambiguous_magnitude_refused(self):
        """5000 with no period is $5k/month or $5k/year -- a 12x difference on
        the one number the whole filter turns on."""
        self.assertFalse(self.s({"min": 5000, "max": 5000, "currency": "USD"}).known)

    def test_unambiguous_magnitude_accepted(self):
        r = self.s({"min": 120000, "max": 140000, "currency": "USD"})
        self.assertTrue(r.known)
        self.assertEqual(r.src_period, "year")

    # --- the text ladder -----------------------------------------------------

    def test_range_with_k_suffix(self):
        r = self.s({"text": "$172K - $440K"})
        self.assertAlmostEqual(r.min_usd_month, 172000 / 12, places=0)

    def test_shared_k_suffix_repaired(self):
        """'$300-450K' means 300K to 450K. Taken literally the bottom of the
        band is understated a thousandfold, and 450,000/yr is plausible enough
        that nothing downstream would catch it."""
        r = self.s({"text": "$300–450K"})
        self.assertAlmostEqual(r.min_usd_month, 300000 / 12, places=0)

    def test_european_decimal_comma(self):
        """"$31,2k" is 31.2 thousand, not 312 thousand. Stripping the comma as
        a thousands separator turned a Remote Office Assistant into a
        $26,000-a-month job sitting near the top of the list."""
        self.assertEqual(sal._clean_number("31,2k"), 31200.0)
        self.assertEqual(sal._clean_number("45,5"), 45.5)

    def test_comma_is_still_a_thousands_separator_when_three_digits_follow(self):
        self.assertEqual(sal._clean_number("120,000"), 120000.0)
        self.assertEqual(sal._clean_number("1,250,000"), 1250000.0)

    def test_m_is_not_millions(self):
        """'6-12 M' is months. Supporting an m suffix produced $12m/month
        salaries on the first live run and nothing else."""
        r = self.s({"text": "contract length 6-12 M"})
        self.assertFalse(r.known)

    def test_absurd_range_width_refused(self):
        """'1083161-2682' is an id. It parsed to $224-$90,263/month."""
        self.assertFalse(self.s({"text": "ref 1083161-2682"}).known)

    def test_distant_period_word_ignored(self):
        """'per day' two paragraphs away turned a GBP 70-80k annual band into a
        daily rate. Sub-monthly periods are read from the tight window only."""
        r = self.s(desc="Salary £70,000 - £80,000. " + "x" * 200 +
                        " We stand up per day at 10am.")
        self.assertEqual(r.src_period, "year")

    def test_adjacent_hourly_marker_believed(self):
        r = self.s(desc="Pay is $45 per hour for this contract.")
        self.assertEqual(r.src_period, "hour")

    def test_annual_period_with_tiny_number_refused(self):
        """'$1,750' beside the word 'annual' is a stipend, not a salary."""
        self.assertFalse(self.s(desc="An annual allowance of $1,750 applies.").known)

    def test_lone_small_figure_refused(self):
        self.assertFalse(self.s(desc="Base pay starts at $1 plus equity.").known)

    def test_description_needs_a_salary_cue(self):
        """Most numbers in an advert are not salaries."""
        self.assertFalse(self.s(desc="We serve 120,000 - 180,000 customers.").known)

    def test_description_with_cue_is_read(self):
        r = self.s(desc="The salary for this role is $120,000 - $180,000 per year.")
        self.assertTrue(r.known)
        self.assertAlmostEqual(r.max_usd_month, 15000, places=0)

    def test_structured_beats_text(self):
        r = self.s({"min": 96000, "max": 96000, "currency": "USD",
                    "period": "year", "text": "$500,000"})
        self.assertEqual(r.derived_by, "structured")
        self.assertAlmostEqual(r.min_usd_month, 8000, places=0)

    def test_dollar_is_lower_confidence_than_a_code(self):
        """'$' is USD, CAD, AUD, SGD and more. It resolves to USD, but the
        confidence has to say so."""
        symbol = self.s(desc="Salary: $120,000 per year")
        code = self.s({"min": 120000, "max": 120000, "currency": "USD",
                       "period": "year"})
        self.assertLess(symbol.confidence, code.confidence)


class TestFx(unittest.TestCase):

    def test_unknown_currency_falls_back_rather_than_failing(self):
        """A stale or missing rate must still rank the job. Refusing to convert
        would silently drop every non-USD posting."""
        self.assertIsNotNone(sal.to_usd_month(1000, "SEK", "month", {}))

    def test_unconvertible_currency_returns_none(self):
        self.assertIsNone(sal.to_usd_month(1000, "XXX", "month", {}))

    def test_a_currency_with_no_rate_is_unknown_not_a_crash(self):
        """Frankfurter publishes about thirty currencies; the sources quote
        more. One Jobicy posting in Costa Rican colon (CRC) made `max()` run
        over an empty sequence, and because the whole stage commits once at the
        end, that ValueError rolled back every row parsed that day -- the stage
        exited 1 on three consecutive daily runs and priced nothing."""
        raw = {"min": 14360800, "max": 18846200,
               "currency": "CRC", "period": "yearly"}
        rates = {"USD": 1.0}
        self.assertIsNone(sal.from_structured(raw, rates))
        result = sal.parse(raw, "", rates)
        self.assertFalse(result.known)
        # known=0 with a figure attached is what the table's CHECK rejects.
        self.assertIsNone(result.min_usd_month)
        self.assertIsNone(result.max_usd_month)

    def test_biggest_of_nothing_is_none_not_an_exception(self):
        """The helper both call sites now go through. `detect_currency` only
        ever returns codes that have a fallback rate, so the text path cannot
        reach this today -- the guard there is deliberate belt-and-braces, and
        this pins the behaviour it relies on."""
        self.assertIsNone(sal._biggest(None, None))
        self.assertEqual(sal._biggest(None, 3.0), 3.0)
        self.assertEqual(sal._biggest(5.0, 3.0), 5.0)


# -------------------------------------------------------------- normalise --

class TestDedupe(unittest.TestCase):

    def test_legal_suffixes_collapse(self):
        self.assertEqual(norm.slug_company("Acme Inc."), norm.slug_company("Acme"))
        self.assertEqual(norm.slug_company("Acme GmbH"), norm.slug_company("acme"))

    def test_different_companies_stay_apart(self):
        self.assertNotEqual(norm.slug_company("Acme Labs"), norm.slug_company("Acme"))

    def test_remote_markers_stripped_from_titles(self):
        self.assertEqual(norm.slug_title("Engineer (Remote)"),
                         norm.slug_title("Engineer"))
        self.assertEqual(norm.slug_title("Engineer - Remote"),
                         norm.slug_title("Engineer"))

    def test_gender_markers_stripped(self):
        self.assertEqual(norm.slug_title("Entwickler (m/w/d)"),
                         norm.slug_title("Entwickler"))

    def test_specialisation_is_not_stripped(self):
        """'Senior Engineer - Backend' and '- Frontend' are two jobs. Stripping
        everything after a dash would hide one of them."""
        self.assertNotEqual(norm.slug_title("Senior Engineer - Backend"),
                            norm.slug_title("Senior Engineer - Frontend"))

    def test_same_job_on_five_boards_is_one_job(self):
        conn = temp_db()
        variants = [
            ("himalayas", "1", "Senior Engineer", "Acme Inc"),
            ("remoteok", "2", "Senior Engineer (Remote)", "Acme"),
            ("jobicy", "3", "Senior Engineer", "acme inc."),
            ("remotive", "4", "Senior Engineer - Remote", "ACME"),
            ("weworkremotely", "5", "Senior Engineer", "Acme, Inc"),
        ]
        for source, sid, title, company in variants:
            conn.execute(
                "INSERT INTO posting (source, source_id, url, title, company, "
                "description, first_seen, last_seen, raw) "
                "VALUES (?,?,?,?,?,'',0,0,'{}')",
                (source, sid, f"https://x/{sid}", title, company))
        conn.commit()
        stats = norm.build(conn)
        self.assertEqual(stats["jobs"], 1)
        self.assertEqual(
            conn.execute("SELECT source_count FROM job").fetchone()["source_count"], 5)

    def test_first_seen_never_moves_forward(self):
        """How long a job has been open is a real signal; rewriting it on every
        run would destroy it."""
        conn = temp_db()
        conn.execute("INSERT INTO posting (source, source_id, url, title, company,"
                     " description, first_seen, last_seen, raw) "
                     "VALUES ('a','1','https://x/1','E','C','',100,100,'{}')")
        conn.execute("INSERT INTO posting (source, source_id, url, title, company,"
                     " description, first_seen, last_seen, raw) "
                     "VALUES ('b','2','https://x/2','E','C','',500,900,'{}')")
        conn.commit()
        norm.build(conn)
        row = conn.execute("SELECT first_seen, last_seen FROM job").fetchone()
        self.assertEqual(row["first_seen"], 100)
        self.assertEqual(row["last_seen"], 900)

    def test_longest_description_wins(self):
        """The eligibility rules read the description. A truncated one loses the
        sentence that decides the gate."""
        conn = temp_db()
        conn.execute("INSERT INTO posting (source, source_id, url, title, company,"
                     " description, first_seen, last_seen, raw) "
                     "VALUES ('a','1','https://x/1','E','C','short',0,0,'{}')")
        conn.execute("INSERT INTO posting (source, source_id, url, title, company,"
                     " description, first_seen, last_seen, raw) "
                     "VALUES ('b','2','https://x/2','E','C',?,0,0,'{}')",
                     ("a much longer description " * 10,))
        conn.commit()
        norm.build(conn)
        self.assertGreater(
            len(conn.execute("SELECT description FROM job_text").fetchone()["description"]),
            50)

    def test_idempotent(self):
        conn = temp_db()
        conn.execute("INSERT INTO posting (source, source_id, url, title, company,"
                     " description, first_seen, last_seen, raw) "
                     "VALUES ('a','1','https://x/1','E','C','',0,0,'{}')")
        conn.commit()
        first = norm.build(conn)
        second = norm.build(conn)
        self.assertEqual(first["jobs"], second["jobs"])


# ---------------------------------------------------------------- sources --

class TestSourceHelpers(unittest.TestCase):

    def test_epoch_accepts_every_shape(self):
        self.assertEqual(epoch(1787553930), 1787553930)
        self.assertEqual(epoch("1787553930"), 1787553930)
        self.assertEqual(epoch(1787553930000), 1787553930)   # milliseconds
        self.assertIsNotNone(epoch("2026-08-21T05:54:39"))
        self.assertIsNotNone(epoch("Mon, 24 Aug 2026 07:30:47 +0000"))

    def test_epoch_refuses_to_guess(self):
        """A plausible wrong date is worse than no date -- it drives staleness."""
        self.assertIsNone(epoch("last Tuesday"))
        self.assertIsNone(epoch(None))
        self.assertIsNone(epoch(""))

    def test_strip_html_keeps_sentence_boundaries(self):
        """The gate quotes sentences. Losing the boundaries makes a rule quote
        across two of them and produce nonsense evidence."""
        out = strip_html("<p>Remote worldwide.</p><p>Must be in the US.</p>")
        self.assertIn("\n", out)
        self.assertNotIn("<p>", out)

    def test_posting_row_shape_matches_the_insert(self):
        p = Posting(source="s", source_id="1", url="https://x/1", title="T",
                    company="C", tags=["a"], timezones=[5.5],
                    salary_raw={"min": 1}, raw={"k": "v"})
        row = p.as_row(123)
        self.assertEqual(len(row), 17)
        self.assertEqual(json.loads(row[12]), ["a"])
        self.assertEqual(json.loads(row[13]), [5.5])
        self.assertEqual(row[15], 123)

    def test_posting_titles_and_companies_are_trimmed(self):
        """Ashby ships titles with a leading space."""
        row = Posting(source="s", source_id="1", url="u",
                      title="  Security Engineer ", company=" Ramp ").as_row(0)
        self.assertEqual(row[3], "Security Engineer")
        self.assertEqual(row[4], "Ramp")


class TestRemoteOkFeed(unittest.TestCase):
    def test_legal_notice_is_not_a_job(self):
        """The first element of RemoteOK's array is its terms of service.
        Iterating naively ingests a posting called None at company None."""
        import sources.remoteok as rok
        rok_get = rok.get
        rok.get = lambda *a, **k: [
            {"legal": "API Terms of Service: please link back"},
            {"id": 9, "position": "Engineer", "company": "Acme",
             "url": "https://x/9", "salary_min": 0, "salary_max": 0},
        ]
        try:
            out = rok.fetch({})
        finally:
            rok.get = rok_get
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Engineer")


class TestWeWorkRemotelyFeed(unittest.TestCase):
    def test_company_role_split(self):
        import sources.weworkremotely as wwr
        xml = ("<rss><channel><item><title>Faire: Senior Manager</title>"
               "<link>https://x/1</link><region>Anywhere in the World</region>"
               "<description>&lt;p&gt;hi&lt;/p&gt;</description>"
               "</item></channel></rss>")
        wwr_get = wwr.get
        wwr.get = lambda *a, **k: xml
        try:
            out = wwr.fetch({})
        finally:
            wwr.get = wwr_get
        self.assertEqual(out[0].company, "Faire")
        self.assertEqual(out[0].title, "Senior Manager")
        self.assertEqual(out[0].location_raw, "Anywhere in the World")

    def test_title_without_a_colon_keeps_the_whole_role(self):
        """No colon means no company prefix. Splitting anyway would file the job
        under a truncated company name."""
        import sources.weworkremotely as wwr
        xml = ("<rss><channel><item><title>Senior Engineer</title>"
               "<link>https://x/2</link></item></channel></rss>")
        wwr_get = wwr.get
        wwr.get = lambda *a, **k: xml
        try:
            out = wwr.fetch({})
        finally:
            wwr.get = wwr_get
        self.assertEqual(out[0].title, "Senior Engineer")
        self.assertEqual(out[0].company, "")


# ------------------------------------------------------------------- geo --

class TestCountryResolution(unittest.TestCase):

    def test_names_and_cities(self):
        for text, want in [("Colombo, Sri Lanka", "LK"), ("South Korea", "KR"),
                           ("Berlin", "DE"), ("New York, NY (HQ); Remote", "US"),
                           ("Remote, Italy; Italy", "IT"), ("Remote (US)", "US")]:
            with self.subTest(text=text):
                self.assertEqual(geo.resolve_country(text)[0], want)

    def test_anywhere_is_not_india(self):
        """`IN` is India's ISO code and "Anywhere in the World" contains the
        word "in". Accepting bare two-letter codes pinned eighty-two
        work-from-anywhere jobs to India, gave them a +5:30 offset and a green
        band none of them had earned, and put them at the top of the list."""
        self.assertEqual(geo.resolve_country("Anywhere in the World"),
                         (None, "nowhere"))
        self.assertEqual(geo.resolve_country("Worldwide")[0], None)

    def test_other_word_shaped_codes_are_refused(self):
        """IT, IS, AT, NO, SO, ME, AN and DE all fail the same way as IN."""
        for text in ("Remote at your convenience", "There is no office",
                     "Work so you can travel", "Send me a note"):
            with self.subTest(text=text):
                iso, how = geo.resolve_country(text)
                self.assertNotEqual(how[:4], "code", f"{text} -> {iso}")

    def test_us_state_needs_the_comma(self):
        """'Austin, TX' is Texas. 'or' in a sentence is not Oregon."""
        self.assertEqual(geo.resolve_country("Somewhere, tx")[0], "US")
        self.assertIsNone(geo.resolve_country("remote or hybrid")[0])

    def test_longest_name_wins(self):
        self.assertEqual(geo.resolve_country("South Korea")[0], "KR")


class TestOverlap(unittest.TestCase):
    """Colombo is UTC+5:30. Every one of these is checked by hand."""

    def hours(self, theirs, mine=5.5):
        return geo.overlap_hours(theirs, mine, 9.0, 18.0)

    def test_same_zone_is_a_whole_day(self):
        self.assertEqual(self.hours(5.5), 9.0)

    def test_us_east_coast_is_zero(self):
        """The finding that justifies the whole timezone component: a New York
        nine-to-six is 18:30-03:30 in Colombo. No overlap at all."""
        self.assertEqual(self.hours(-4.0), 0.0)

    def test_uk_is_workable(self):
        self.assertEqual(self.hours(1.0), 4.5)

    def test_germany_is_comfortable(self):
        self.assertEqual(self.hours(2.0), 5.5)

    def test_fractional_offsets_survive(self):
        """Kathmandu is +5:45 and Chatham Island is +12:45. Rounding offsets to
        whole hours would mislabel every South Asian job by half an hour --
        exactly the margin between a green band and an amber one."""
        self.assertAlmostEqual(self.hours(5.75), 8.75, places=2)

    def test_overlap_is_symmetric(self):
        self.assertEqual(geo.overlap_hours(2.0, 5.5, 9.0, 18.0),
                         geo.overlap_hours(5.5, 2.0, 9.0, 18.0))

    def test_date_line_does_not_break_it(self):
        """A window crossing midnight UTC is split into two, not wrapped
        negative -- Auckland at +12 is the case that catches it."""
        for offset in (12.0, -11.0, 13.0, -12.0):
            with self.subTest(offset=offset):
                hours = self.hours(offset)
                self.assertGreaterEqual(hours, 0.0)
                self.assertLessEqual(hours, 9.0)

    def test_bands(self):
        self.assertEqual(geo.band(9.0, 4, 2), "green")
        self.assertEqual(geo.band(3.0, 4, 2), "amber")
        self.assertEqual(geo.band(0.0, 4, 2), "red")


class TestGeoWritePath(unittest.TestCase):

    def _job(self, conn, jid, loc, quote=""):
        conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "description, canonical_url, first_seen, last_seen) "
            "VALUES (?,?,'Engineer','C','c','','https://x/1',0,0)", (jid, f"k{jid}|t"))
        conn.execute(
            "INSERT INTO posting (source, source_id, url, title, company, "
            "description, location_raw, first_seen, last_seen, raw, job_id) "
            "VALUES ('himalayas',?,'https://x/1','Engineer','C','',?,0,0,'{}',?)",
            (str(jid), loc, jid))
        conn.execute(
            "INSERT INTO eligibility (job_id, state, evidence_quote, evidence_url,"
            " rule, decided_by, confidence, decided_at) "
            "VALUES (?,'OPEN_WORLDWIDE',?, 'https://x/1','r','rule',1,0)",
            (jid, quote or "location field: " + loc))
        conn.commit()

    def test_rerun_clears_a_stale_placement(self):
        """`--all` has to be able to *erase*.

        Both branches used to write different upserts and the unplaced one
        touched only `decided_at`, so 143 jobs kept a country, an offset and a
        green band through every re-run after the resolver that produced them
        was fixed. A re-decide that cannot un-decide is not a re-decide.
        """
        conn = temp_db()
        self._job(conn, 1, "Anywhere in the World")
        # Plant exactly the wrong row the old code would have left behind.
        conn.execute(
            "INSERT INTO geo (job_id, country, country_name, lat, lon, precision,"
            " utc_offset, overlap_hours, band, decided_at) "
            "VALUES (1,'IN','India',20.0,78.0,'country',5.5,9.0,'green',0)")
        conn.commit()

        geo.apply(conn, redo=True)
        row = conn.execute("SELECT * FROM geo WHERE job_id = 1").fetchone()
        self.assertIsNone(row["country"])
        self.assertIsNone(row["utc_offset"])
        self.assertIsNone(row["band"])

    def test_a_real_country_is_placed(self):
        conn = temp_db()
        self._job(conn, 1, "Berlin, Germany")
        geo.apply(conn, redo=True)
        row = conn.execute("SELECT * FROM geo WHERE job_id = 1").fetchone()
        self.assertEqual(row["country"], "DE")
        self.assertIsNotNone(row["band"])


# ------------------------------------------------------------------- fit --

class TestReach(unittest.TestCase):
    """Would they take him? Every case here is a job from the top 40 that four
    independent judges assessed against his real CV. The audit found 49 of 69
    verdicts were "well above him" and only 6 jobs would plausibly get him an
    interview -- the ranking had been sorting jobs he could legally take, not
    jobs he could get."""

    def r(self, title, desc=""):
        return fit.reach(title, desc)[0]

    def test_title_beats_years_in_the_body(self):
        """The defect that started this. `seniority_fit` read years-of-experience
        BEFORE the title, so "Senior AI Engineer" whose advert mentioned "2
        years" scored a near-maximum ENTRY-LEVEL bonus. 31 of 306 jobs had a
        senior marker in the title and escaped the penalty entirely."""
        self.assertEqual(self.r("Senior AI Engineer", "2 years experience"), "stretch")
        self.assertEqual(self.r("Senior Data Engineer", "at least 2 years"), "stretch")

    def test_hard_seniority_markers(self):
        for title in ("Engineering Manager, Experimentation", "Staff Product Manager",
                      "Benefits Operations Lead", "Backend Developer Level III",
                      "DevOps Engineer IV (Obs)", "Director of Engineering",
                      "Principal Architect", "VP of Product"):
            with self.subTest(title=title):
                self.assertEqual(self.r(title), "no_chance")

    def test_bare_manager_is_senior(self):
        """"Revenue Strategy & Operations Manager" (Mixpanel) scored zero
        seniority penalty and ranked second on the whole board."""
        self.assertEqual(self.r("Revenue Strategy & Operations Manager"), "no_chance")
        self.assertEqual(self.r("Product Owner, Tools & Systems"), "no_chance")

    def test_a_junior_qualifier_beats_a_soft_marker(self):
        """"Associate Product Manager" and "Junior Product Manager" are graduate
        jobs that happen to contain the word Manager. The first fix used
        lookbehinds and stripped the entry word before testing, so the lookbehind
        had nothing to see and dropped both genuine graduate roles."""
        self.assertEqual(self.r("Associate Product Manager (Remote)"), "likely")
        self.assertEqual(self.r("Junior Product Manager*in"), "likely")

    def test_a_junior_qualifier_does_not_beat_a_hard_marker(self):
        """"Associate Director" is a director."""
        self.assertEqual(self.r("Associate Director, Data"), "no_chance")

    def test_entry_titles(self):
        for title in ("Graduate Software Engineer", "Junior Automation Specialist",
                      "Data Analyst Intern", "Trainee Developer"):
            with self.subTest(title=title):
                self.assertEqual(self.r(title), "likely")

    def test_years_thresholds_are_set_against_his_eighteen_months(self):
        self.assertEqual(self.r("Business Analyst", "3+ years experience"), "stretch")
        self.assertEqual(self.r("Business Analyst", "5+ years experience"), "no_chance")
        self.assertEqual(self.r("Business Analyst", "1 year of experience"), "likely")

    def test_entry_title_with_a_heavy_ask_is_only_a_stretch(self):
        self.assertEqual(self.r("Junior Engineer", "6+ years experience"), "stretch")

    def test_unmarked_titles_are_plausible_not_likely(self):
        self.assertEqual(self.r("Backend Software Engineer"), "plausible")


class TestYearsRequired(unittest.TestCase):
    """The years regex was rewritten after the audit found it silently missing
    most real phrasings — an invisible requirement is worse than none, because
    the job then presents itself as a graduate role."""

    def test_en_dash_ranges(self):
        """"8–10+ years of professional software engineering experience" — an
        en-dash, and four arbitrary words the old whitelist did not allow."""
        self.assertEqual(fit.years_required(
            "8–10+ years of professional software engineering experience"), 10)

    def test_arbitrary_words_between_years_and_experience(self):
        self.assertEqual(fit.years_required(
            "at least 3 years of hands-on Python development experience"), 3)

    def test_plain_phrasing(self):
        self.assertEqual(fit.years_required("2+ years experience"), 2)
        self.assertEqual(fit.years_required("5-7 years of relevant experience"), 7)

    def test_takes_the_largest_not_the_first(self):
        """Lemon.io publishes several role tracks in one description. Reading
        the leftmost match let a senior posting look like a graduate one."""
        self.assertEqual(fit.years_required(
            "Track 1: 2+ years experience. Track 2: 7+ years experience."), 7)

    def test_no_number_is_none(self):
        self.assertIsNone(fit.years_required("We want a great engineer."))

    def test_an_alternative_route_is_not_a_requirement(self):
        """"2 years relevant experience, or 4 years in lieu of a degree" — the
        four is the route for candidates without a degree. He has one, so taking
        the maximum invents a bar the advert does not set for him. Flagged by an
        adversarial reviewer against the max-not-first change."""
        self.assertEqual(fit.years_required(
            "2 years relevant experience, or 4 years experience in lieu of a degree"), 2)

    def test_a_ceiling_is_not_a_floor(self):
        """"Bachelor's degree and less than 2 years of experience" is an
        entry-level signal read as a two-year requirement."""
        self.assertIsNone(fit.years_required(
            "Bachelor's degree and less than 2 years of experience"))

    def test_absurd_figures_ignored(self):
        """A 40-year requirement is a parse error, not a job."""
        self.assertIsNone(fit.years_required("Founded 1998 years experience"))


class TestPlainEnglishGates(unittest.TestCase):

    def test_beginners_rejected(self):
        """Elite Software Automation's Business Analyst — third on his list —
        says outright that beginners will be rejected."""
        self.assertEqual(fit.reach("Business Analyst",
            "We are only accepting experienced candidates at this time. "
            "If you are a beginner, your application will be rejected."),
            ("no_chance", "the advert says 'only accepting experienced candidates'"))

    def test_manages_a_team(self):
        """A title can hide a people-management job the body states plainly."""
        self.assertEqual(fit.reach("Software Engineer",
                                   "You will be managing a team of five.")[0],
                         "no_chance")
        self.assertEqual(fit.reach("Engineer", "You will have direct reports.")[0],
                         "no_chance")

    def test_a_senior_title_can_only_be_made_worse_by_the_body(self):
        self.assertEqual(fit.reach("Senior AI Engineer",
            "Track A: 2+ years experience. Track B: 7+ years experience.")[0],
            "no_chance")

    def test_years_without_the_word_experience(self):
        """"5+ years in a similar role" and "3+ years building distributed
        systems" state a requirement without ever saying "experience"."""
        self.assertEqual(fit.years_required("5+ years in a similar role"), 5)
        self.assertEqual(fit.years_required(
            "3+ years building distributed systems"), 3)


class TestWorkMode(unittest.TestCase):

    def m(self, loc="", desc="", tags=None):
        return fit.work_mode(loc, desc, tags or [])[0]

    def test_location_field_wins(self):
        self.assertEqual(self.m(loc="Remote"), "remote")
        self.assertEqual(self.m(loc="Berlin — Hybrid"), "hybrid")
        self.assertEqual(self.m(loc="London, On-site"), "onsite")

    def test_hybrid_beats_remote_in_prose(self):
        """"A remote-friendly company that meets in the office three days a
        week" is a hybrid job, and the three days are the binding half."""
        self.assertEqual(
            self.m(desc="We are a remote-friendly company. You will be in the "
                        "office 3 days a week."), "hybrid")

    def test_explicit_remote(self):
        for text in ("This is a 100% remote role.", "We are remote-first.",
                     "You can work from home.", "A fully distributed team."):
            with self.subTest(text=text):
                self.assertEqual(self.m(desc=text), "remote")

    def test_explicit_onsite(self):
        self.assertEqual(self.m(desc="This is not a remote role."), "onsite")
        self.assertEqual(self.m(desc="You will relocate to Munich."), "onsite")

    def test_silence_is_unknown_not_remote(self):
        self.assertEqual(self.m(desc="A great job at a great company."), "unknown")


class TestViability(unittest.TestCase):
    """The rule the candidate asked for on 2026-08-24: it has to be remote unless it
    is in Sri Lanka."""

    def v(self, state="OPEN_WORLDWIDE", country=None, mode="remote",
          reach="plausible", role_value=1.0, role_why="'Engineer'"):
        return fit.viability(state, country, mode, reach, "LK",
                             role_value, role_why)

    def test_onsite_abroad_is_refused(self):
        self.assertFalse(self.v(state="OPEN_REGION", country="SG", mode="onsite")[0])

    def test_hybrid_abroad_is_refused(self):
        """Hybrid means an office he cannot reach, which is on-site with extra
        steps."""
        self.assertFalse(self.v(state="OPEN_REGION", country="DE", mode="hybrid")[0])

    def test_onsite_in_sri_lanka_is_fine(self):
        self.assertTrue(self.v(state="ONSITE_LK", country="LK", mode="onsite")[0])
        self.assertTrue(self.v(state="OPEN_REGION", country="LK", mode="onsite")[0])

    def test_onsite_abroad_with_sponsorship_is_fine(self):
        """He asked for these explicitly: relocation is on the table when the
        visa comes with it."""
        self.assertTrue(
            self.v(state="ONSITE_SPONSORED", country="DE", mode="onsite")[0])

    def test_worldwide_with_no_stated_mode_is_remote_by_construction(self):
        """Nobody offers a desk in every country at once."""
        self.assertTrue(self.v(state="OPEN_WORLDWIDE", mode="unknown")[0])

    def test_region_with_no_stated_mode_is_refused(self):
        """"Open to APAC" could easily be an office in Singapore."""
        self.assertFalse(self.v(state="OPEN_REGION", country="SG", mode="unknown")[0])

    def test_blocked_is_never_viable(self):
        """A US-only role that is remote and pitched at his level is still a
        role he cannot have. Checking only the work mode let 337 of them
        through."""
        self.assertFalse(self.v(state="BLOCKED", mode="remote")[0])
        self.assertFalse(self.v(state="ONSITE_NO_SPONSOR", mode="remote")[0])

    def test_no_chance_is_never_viable(self):
        self.assertFalse(self.v(reach="no_chance")[0])

    def test_a_different_profession_is_never_viable(self):
        """Profession is a gate, not a penalty.

        Eligibility, timezone, pay and freshness are 80 of the 130 scoring
        points and none of them say whether he is qualified, so a job in the
        wrong profession lost 20 points and kept its place. Nineteen of the 98
        viable jobs were somebody else's career — an Online English Teacher, a
        Medical Licensing Specialist, a Contract Legal Assistant. Flagged by an
        adversarial reviewer; scoring cannot fix it, only removal can."""
        ok, why = self.v(role_value=-1.0, role_why="'Teacher' is a different profession")
        self.assertFalse(ok)
        self.assertIn("different profession", why)

    def test_adjacent_and_own_professions_stay_viable(self):
        for value in (1.0, 0.4, 0.1, -0.2):
            with self.subTest(role_value=value):
                self.assertTrue(self.v(role_value=value)[0])

    def test_every_refusal_gives_a_reason(self):
        for kwargs in ({"state": "BLOCKED"}, {"reach": "no_chance"},
                       {"state": "OPEN_REGION", "country": "SG", "mode": "onsite"},
                       {"state": "OPEN_REGION", "country": "DE", "mode": "hybrid"},
                       {"state": "OPEN_REGION", "country": "SG", "mode": "unknown"}):
            with self.subTest(**kwargs):
                ok, why = self.v(**kwargs)
                self.assertFalse(ok)
                self.assertTrue(why.strip(), "a refusal with no reason is unreviewable")


# ----------------------------------------------------------------- score --

class TestScore(unittest.TestCase):

    def test_director_titles_are_pushed_down(self):
        """An early-career CV reads as "capable graduate". A Director role
        ranking highly is the system wasting his week."""
        for title in ("Director of Sales", "VP of Engineering", "Head of Product",
                      "Chief Technology Officer", "Principal Engineer"):
            with self.subTest(title=title):
                self.assertLess(score.seniority_fit(title, "")[0], 0)

    def test_entry_titles_are_lifted(self):
        for title in ("Junior Developer", "Graduate Software Engineer",
                      "Associate Product Manager", "Data Analyst Intern"):
            with self.subTest(title=title):
                self.assertGreater(score.seniority_fit(title, "")[0], 0)

    def test_account_executive_is_rejected_on_the_role_axis(self):
        """'executive' was in the mid-level pattern and matched "Enterprise
        Account Executive" -- a senior sales role -- as a level he could reach.

        The rejection now lives on the role axis rather than the seniority one,
        which is the more honest place for it: the title alone genuinely does
        not say what level an Account Executive is, but it says perfectly
        clearly that it is not his profession."""
        self.assertEqual(score.role_fit("Enterprise Account Executive")[0], -1.0)
        self.assertNotEqual(fit.reach("Enterprise Account Executive", "")[0], "likely")

    def test_years_of_experience_is_read(self):
        self.assertLess(score.seniority_fit("Engineer", "8+ years experience")[0], 0)
        self.assertGreater(score.seniority_fit("Engineer", "1 years experience")[0], 0)

    def test_seniority_fit_delegates_to_fit_reach(self):
        """One classification, in one place, tested once. score.py only converts
        it into points now."""
        self.assertEqual(score.seniority_fit("Senior AI Engineer", "2 years")[0],
                         score.REACH_POINTS["stretch"])

    def test_other_professions_are_rejected_on_the_title(self):
        """The skills list is read against the whole advert, and a technology
        company's advert describes technology whichever job it is. Anthropic's
        Enterprise Account Executive posting scored full marks on skills."""
        for title in ("Enterprise Account Executive", "Registered Nurse",
                      "Teleradiologist", "Paralegal", "Recruiter",
                      "Insurance Agent"):
            with self.subTest(title=title):
                self.assertEqual(score.role_fit(title)[0], -1.0)

    def test_his_professions_are_accepted(self):
        for title in ("Backend Engineer", "Business Analyst", "Data Analyst",
                      "Automation Specialist", "DevOps Engineer"):
            with self.subTest(title=title):
                self.assertEqual(score.role_fit(title)[0], 1.0)

    def test_adjacent_professions_get_partial_credit(self):
        """A flat family list gave "Product Manager" the same full credit as
        "Backend Engineer". He has never held a PM role, and the independent
        audit judged every product-management posting in the top 40 as not his
        field -- but it is a move he could plausibly make, so it is scored
        between his own work and somebody else's profession."""
        for title in ("Junior Product Manager", "Associate Product Manager",
                      "Technical Web Specialist", "Implementation Consultant"):
            with self.subTest(title=title):
                points = score.role_fit(title)[0]
                self.assertGreater(points, 0.0)
                self.assertLess(points, 1.0)

    def test_straddling_titles_are_neither(self):
        self.assertGreater(score.role_fit("Sales Engineer")[0], -1.0)
        self.assertLess(score.role_fit("Sales Engineer")[0], 1.0)

    def test_unknown_pay_is_neutral_not_zero(self):
        """Scoring an unpublished salary as zero would bury a third of the
        market, and much of the best of it."""
        unknown = score.pay_fit(0, None, None, 2000)[0]
        below = score.pay_fit(1, 500, 900, 2000)[0]
        self.assertGreater(unknown, below)
        self.assertLess(unknown, score.pay_fit(1, 8000, 12000, 2000)[0])

    def test_below_floor_scores_zero(self):
        self.assertEqual(score.pay_fit(1, 900, 1200, 2000)[0], 0.0)

    def test_score_is_capped_at_100(self):
        conn = temp_db()
        conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "description, first_seen, last_seen, posted_at) VALUES "
            "(1,'k|t','Junior Automation Engineer','C','c','python sql api "
            "automation typescript docker react node excel reporting dashboard "
            "1 years experience',?,?,?)", (now(), now(), now()))
        conn.execute(
            "INSERT INTO eligibility (job_id, state, evidence_quote, evidence_url,"
            " rule, decided_by, confidence, decided_at) VALUES "
            "(1,'OPEN_WORLDWIDE','q','https://x/1','r','rule',1,0)")
        conn.execute("INSERT INTO salary (job_id, known, min_usd_month, "
                     "max_usd_month, decided_at) VALUES (1,1,20000,30000,0)")
        conn.execute("INSERT INTO geo (job_id, band, overlap_hours, decided_at) "
                     "VALUES (1,'green',9.0,0)")
        conn.execute("INSERT INTO fit (job_id, work_mode, reach, viable, "
                     "viable_why, decided_at) VALUES (1,'remote','likely',1,'remote',0)")
        conn.commit()
        score.apply(conn)
        total = conn.execute("SELECT total FROM score").fetchone()["total"]
        self.assertLessEqual(total, 100.0)
        self.assertGreater(total, 85.0)

    def test_blocked_jobs_lose_their_score(self):
        """A job that becomes blocked must not keep its place in a sorted list."""
        conn = temp_db()
        conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "description, first_seen, last_seen) VALUES (1,'k|t','Engineer',"
            "'C','c','',0,0)")
        conn.execute(
            "INSERT INTO eligibility (job_id, state, decided_by, confidence, "
            "decided_at) VALUES (1,'BLOCKED','rule',1,0)")
        conn.execute("INSERT INTO fit (job_id, work_mode, reach, viable, "
                     "viable_why, decided_at) VALUES (1,'remote','likely',0,'blocked',0)")
        conn.execute("INSERT INTO score (job_id, total, breakdown, scored_at) "
                     "VALUES (1, 90.0, '{}', 0)")
        conn.commit()
        score.apply(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM score").fetchone()["c"], 0)


# ----------------------------------------------------------------- cities --

class TestCityMatching(unittest.TestCase):
    """City precision is what makes the timezone band trustworthy: the country
    table put Austin on New York's clock (one hour out) and San Francisco on it
    too (three hours out), and three hours is the width of a whole band."""

    def setUp(self):
        self.conn = temp_db()
        rows = [
            (None, "Austin", "austin", "US", "TX", 30.27, -97.74, "America/Chicago", 974447),
            (None, "San Francisco", "san francisco", "US", "CA", 37.77, -122.42,
             "America/Los_Angeles", 827526),
            (None, "New York City", "new york city", "US", "NY", 40.71, -74.01,
             "America/New_York", 8804190),
            (None, "Colombo", "colombo", "LK", "36", 6.93, 79.86, "Asia/Colombo", 648034),
            (None, "Munchen", "munchen", "DE", "02", 48.14, 11.58, "Europe/Berlin", 1505005),
            # The trap: a village in Indiana called Ireland.
            (None, "Ireland", "ireland", "US", "IN", 38.41, -87.00, "America/Indiana/Vincennes", 800),
        ]
        self.conn.executemany("INSERT INTO city VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def r(self, text, **kw):
        return cities.resolve(self.conn, text, **kw)

    def test_state_abbreviation_disambiguates(self):
        self.assertEqual(self.r("Austin, TX")["timezone"], "America/Chicago")

    def test_noise_words_are_stripped(self):
        self.assertEqual(self.r("Remote — Austin, TX (HQ)")["name"], "Austin")

    def test_prefix_match_for_new_york(self):
        """GeoNames calls it "New York City"; every job advert says "New York"."""
        self.assertEqual(self.r("New York, NY")["timezone"], "America/New_York")

    def test_accents_fold(self):
        """GeoNames names Munich "Munich"; the German spelling is an alias, and
        an earlier version dropped every non-ASCII alias before folding it."""
        self.assertEqual(self.r("München")["country"], "DE")

    def test_a_country_beats_a_village_of_the_same_name(self):
        """There is an Ireland in Indiana, population 800. Population
        tie-breaking picked it over the country and put Irish jobs on UTC-5."""
        self.assertIsNone(self.r("Ireland", avoid={"ireland"}))
        self.assertIsNotNone(self.r("Ireland"))   # without the veto, it matches

    def test_short_fragments_do_not_match(self):
        self.assertIsNone(self.r("NY"))

    def test_empty_is_none(self):
        self.assertIsNone(self.r(""))
        self.assertIsNone(self.r("   "))

    def test_normalise_folds_case_accents_and_punctuation(self):
        self.assertEqual(cities.normalise("Zürich"), "zurich")
        self.assertEqual(cities.normalise("SÃO PAULO"), "sao paulo")
        self.assertEqual(cities.normalise("St. John's"), "st john s")


# -------------------------------------------------------------------- kit --

@unittest.skipUnless((HERE.parent / "resume" / "STAR-STORIES.md").exists(),
                     "needs your own resume/STAR-STORIES.md and INTERVIEW-LINES.md")
class TestKit(unittest.TestCase):
    """The pack must never invent a claim about him. Everything it says comes
    out of resume/, which is written and checked; the only things it composes
    are the ones computed from this job's own data."""

    def test_it_reads_his_real_story_bank(self):
        stories = kit.load_stories()
        self.assertGreaterEqual(len(stories), 4)
        for story in stories:
            self.assertTrue(story["s"] and story["t"] and story["a"] and story["r"])
            self.assertTrue(story["tags"], "a story with no use-for tag cannot be matched")

    def test_it_reads_his_prepared_lines(self):
        lines = kit.load_lines()
        self.assertIn("Why you (20 sec)", lines)
        self.assertIn("Tell me about yourself (90 sec)", lines)

    def test_story_selection_follows_the_advert(self):
        stories = kit.load_stories()
        spec = kit.pick_stories(
            "You will own requirements gathering with non-technical stakeholders "
            "and run UAT.", stories)
        self.assertTrue(any("stock dashboard" in s["title"].lower() for s in spec))

    def test_selection_never_returns_nothing(self):
        """An empty pack helps nobody; the two strongest stories are the floor."""
        self.assertTrue(kit.pick_stories("", kit.load_stories()))

    def test_story_pool_and_project_pool_cover_the_real_files(self):
        """The two sources answer_for() falls back to for open-ended "describe
        a project" questions -- built from the real files, not fixtures, so a
        broken parse here would show up as every essay question going blank."""
        pool = kit.load_stories()
        stories = kit._story_pool(pool)
        self.assertEqual(len(stories), len(pool))
        for heading in stories:
            self.assertTrue(heading.strip())
        projects = kit._project_pool()
        self.assertGreaterEqual(len(projects), 3)
        for heading, body in projects.items():
            self.assertTrue(heading.strip() and body.strip())

    def test_open_ended_project_questions_answer_from_the_cv(self):
        """INTERVIEW-LINES.md is a narrow FAQ for hard interview questions; it
        was never written to cover a form's "describe your best project" box.
        The CV's own Selected Projects section is the real source, and the
        route has to recognise the question by its shape (a generic prompt
        shares almost no vocabulary with a specific project's name, so plain
        word-overlap matching can never find it on its own)."""
        job = {"id": 1, "title": "Software Engineer", "company": "Acme",
              "description": "Build things."}
        ctx = {"lines": {}, "story_pool": {}, "projects": kit._project_pool(),
              "why_company": "", "hours": "", "start_date": ""}
        for label in ("Describe your best personal software project, outside of curriculum or work",
                     "Tell us about a project you're proud of",
                     "What is your favourite project you have built?"):
            answer = kit.answer_for(None, job, {"label": label, "values": []}, ctx)
            self.assertTrue(answer, f"no answer for: {label}")
            self.assertNotIn(kit.NEEDS_YOU, answer)
            # every word of the answer traces back to a real project heading
            self.assertTrue(any(h.split(" — ")[0] in answer for h in ctx["projects"]))

    def test_current_ctc_is_a_different_question_from_salary_expectations(self):
        """Conflating "what do you currently make" with "what do you want" is
        the kind of thing that reads as evasive, not honest -- and this file
        must never invent his current CTC, unlike his expected pay, which is
        a real, stated floor."""
        for label in ("What is your current annual CTC (Cost to Company)? Please "
                     "provide full amount in your local currency.",
                     "Present salary", "Last drawn salary", "Current compensation"):
            self.assertEqual(kit.route(label), "current_ctc")
        self.assertEqual(kit.route("What are your salary expectations?"), "salary")
        answer = kit.current_ctc_answer()
        self.assertNotRegex(answer, r"\$[\d,]+|\bLKR\b|\d{4,}")   # no invented figure
        self.assertIn("internship", answer.lower())                 # the one true thing on file

    def test_untracked_jobs_still_get_identity_and_ctc_and_project_answers(self):
        """The gap this closes: a job jobscout has never seen used to leave
        every field blank, including his own name, because the browser
        helper's local-only fallback had no working identity source at all.
        The server must answer what it honestly can from a job-less
        context -- identity, CTC, story/project matching -- and only leave
        genuinely job-specific or unsourced fields blank."""
        fields = [
            {"label": "First Name", "type": "text", "required": True},
            {"label": "Last Name", "type": "text", "required": True},
            {"label": "Email", "type": "text", "required": True},
            {"label": "What is your current annual CTC?", "type": "text", "required": True},
            {"label": "Describe your best personal project", "type": "textarea", "required": False},
            {"label": "Which gender do you identify as?", "type": "select", "required": False,
             "options": ["Man", "Woman", "Non-binary"]},
        ]
        out = kit.answers_for_labels_generic(fields)
        by = {a["label"]: a for a in out["answers"]}
        self.assertEqual(by["First Name"]["answer"], "Alex")
        self.assertEqual(by["Last Name"]["answer"], "Example")
        self.assertIn("@", by["Email"]["answer"])
        self.assertIn("internship", by["What is your current annual CTC?"]["answer"].lower())
        self.assertFalse(by["Which gender do you identify as?"]["answer"])
        self.assertTrue(by["Which gender do you identify as?"]["needs_you"])
        self.assertIsNone(out["job_id"])

    def test_it_never_answers_a_demographic_or_opinion_question(self):
        """The broadened fallback (interview lines -> stories -> projects)
        must never fire on a question it was not built for. Guessing gender or
        ethnicity risks misrepresenting him; a "favourite food" has no source
        at all. Both must stay [NEEDS YOU], however rich the story/project
        pools get."""
        job = {"id": 1, "title": "Software Engineer", "company": "Acme", "description": ""}
        ctx = {"lines": kit.load_lines(), "story_pool": kit._story_pool(kit.load_stories()),
              "projects": kit._project_pool(), "why_company": "", "hours": "", "start_date": ""}
        for label in ("Which gender do you identify as?",
                     "Please indicate your race or ethnicity",
                     "Are you Hispanic/Latino?",
                     "Do you identify as transgender?",
                     "What's your favorite junk food?"):
            answer = kit.answer_for(None, job, {"label": label, "values": []}, ctx)
            self.assertFalse(answer, f"invented an answer for a demographic/opinion question: {label!r} -> {answer!r}")

    def test_clock_answer_states_both_clocks(self):
        conn = temp_db()
        answer = kit.clock_answer(conn, {"utc_offset": 1.0, "overlap_hours": 4.5,
                                         "country_name": "London, United Kingdom"})
        self.assertIn("Colombo", answer)
        self.assertIn("4.5", answer)
        self.assertRegex(answer, r"\d{2}:\d{2}")

    def test_clock_answer_is_honest_about_zero_overlap(self):
        conn = temp_db()
        answer = kit.clock_answer(conn, {"utc_offset": -4.0, "overlap_hours": 0.0,
                                         "country_name": "United States"})
        self.assertIn("no natural overlap", answer)

    def test_salary_answer_anchors_on_their_published_range(self):
        answer = kit.salary_answer({"pay_known": 1, "lo": 8000, "hi": 12000})
        self.assertIn("96,000", answer)     # 8000 x 12
        self.assertIn("144,000", answer)

    def test_salary_answer_without_a_published_range_asks_first(self):
        answer = kit.salary_answer({"pay_known": 0})
        self.assertIn("your band first", answer)

    def test_sponsorship_answer_matches_the_eligibility_state(self):
        self.assertIn("would need sponsorship".split()[-1],
                      kit.sponsorship_answer({"state": "ONSITE_SPONSORED"}))
        self.assertIn("No sponsorship needed",
                      kit.sponsorship_answer({"state": "ONSITE_LK"}))
        self.assertIn("contractor", kit.sponsorship_answer({"state": "OPEN_WORLDWIDE"}))

    def test_cover_letter_leaves_the_unknowable_in_brackets(self):
        """A generated specific that turns out to be wrong is worse than a
        blank he fills in ten seconds."""
        letter = kit.cover_letter({"company": "Acme", "title": "Analyst"},
                                  kit.load_stories()[:1])
        self.assertIn("[ONE SENTENCE ON WHY THIS COMPANY", letter)
        self.assertIn("Acme", letter)


# ----------------------------------------------------------------- applied --

class TestApplied(unittest.TestCase):
    """Until this existed the board could rank jobs forever and never notice he
    had applied to any. Tomorrow's list showed the same five at the top."""

    def setUp(self):
        self.conn = temp_db()
        self.conn.execute(
            "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
            "first_seen, last_seen) VALUES (1, 'k|t', 'Analyst', 'Acme', 'acme', 0, 0)")
        self.conn.commit()

    def test_marking_records_an_application_and_a_touch(self):
        result = applied.mark(self.conn, 1, "portal")
        self.assertFalse(result["already"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM application").fetchone()["c"], 1)
        self.assertEqual(
            self.conn.execute("SELECT kind FROM touch").fetchone()["kind"], "applied")

    def test_marking_twice_is_refused_not_duplicated(self):
        """The tracker already carries one "SENT TWICE" note. Once was enough."""
        applied.mark(self.conn, 1, "portal")
        second = applied.mark(self.conn, 1, "email")
        self.assertTrue(second["already"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM application").fetchone()["c"], 1)

    def test_an_unknown_job_is_refused(self):
        with self.assertRaises(ValueError):
            applied.mark(self.conn, 999, "portal")

    def test_a_bad_lane_is_refused(self):
        with self.assertRaises(ValueError):
            applied.mark(self.conn, 1, "telepathy")

    def test_followup_clock_matches_his_own_convention(self):
        """7 and 14 days, taken from job-applications/chaser.py rather than
        reinvented — two systems disagreeing about when a follow-up is due is
        worse than either answer."""
        day = 86400
        self.assertEqual(applied.followup_state(now() - 1 * day, None)[0], "waiting")
        self.assertEqual(applied.followup_state(now() - 8 * day, None)[0], "due")
        self.assertEqual(applied.followup_state(now() - 20 * day, None)[0], "stale")

    def test_an_outcome_stops_the_clock(self):
        """A rejection at 30 days is not overdue for a chase."""
        self.assertEqual(
            applied.followup_state(now() - 30 * 86400, "rejected")[0], "rejected")

    def test_outcome_needs_an_application_on_record(self):
        self.assertFalse(applied.outcome(self.conn, 1, "callback"))
        applied.mark(self.conn, 1, "portal")
        self.assertTrue(applied.outcome(self.conn, 1, "callback"))

    def test_a_bad_outcome_is_refused(self):
        applied.mark(self.conn, 1, "portal")
        with self.assertRaises(ValueError):
            applied.outcome(self.conn, 1, "vibes")

    def test_pipeline_reports_state_and_age(self):
        applied.mark(self.conn, 1, "email")
        item = applied.pipeline(self.conn)[0]
        self.assertEqual(item["company"], "Acme")
        self.assertEqual(item["state"], "waiting")
        self.assertEqual(item["days"], 0)

    def test_applied_jobs_leave_the_queue(self):
        """The query the board runs. A job he has finished with must not be at
        the top of tomorrow's list."""
        applied.mark(self.conn, 1, "portal")
        still_open = self.conn.execute(
            "SELECT COUNT(*) c FROM job j WHERE j.id NOT IN "
            "(SELECT job_id FROM application WHERE status = 'sent')").fetchone()["c"]
        self.assertEqual(still_open, 0)

    def test_migration_added_the_outcome_columns(self):
        """CREATE TABLE IF NOT EXISTS cannot widen a table, and dropping this
        one would take the application history with it."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(application)")}
        self.assertIn("outcome", cols)
        self.assertIn("outcome_at", cols)


# ------------------------------------------------------------- supply x10 --

class TestCursorResumption(unittest.TestCase):
    """Himalayas serves 20 jobs a page against a 103,000-job feed. Re-walking
    the newest 30 pages every morning collected the same 600 jobs forever; the
    other 102,400 were unreachable because nothing ever asked for page 31."""

    def setUp(self):
        self.conn = temp_db()

    def test_a_fresh_source_starts_at_the_beginning(self):
        self.assertEqual(sources.cursor_get(self.conn, "himalayas"), (None, 0, False))

    def test_a_cursor_survives(self):
        sources.cursor_set(self.conn, "himalayas", "abc123", 30)
        self.assertEqual(sources.cursor_get(self.conn, "himalayas"),
                         ("abc123", 30, False))

    def test_exhaustion_is_recorded(self):
        sources.cursor_set(self.conn, "himalayas", None, 0, exhausted=True)
        self.assertTrue(sources.cursor_get(self.conn, "himalayas")[2])

    def test_raw_is_trimmed_to_what_the_pipeline_reads(self):
        """14 KB per posting was the entire growth curve. Only the salary block
        is ever read out of it; everything else has its own column."""
        big = {"description": "x" * 9000, "title": "y" * 3000,
               "min": 50, "max": 90, "currency": "USD",
               "locationRestrictions": ["Anywhere"]}
        trimmed = sources.trim_raw(big)
        self.assertNotIn("description", trimmed)
        self.assertEqual(trimmed["min"], 50)
        self.assertEqual(trimmed["locationRestrictions"], ["Anywhere"])
        self.assertLess(len(json.dumps(trimmed)), 200)

    def test_politeness_is_per_host(self):
        """One shared gap made five providers on five different servers wait a
        second each, as though they were one overloaded machine. That made
        board discovery across 1,157 employers unaffordable."""
        self.assertIsInstance(sources._last_request_at, dict)


class TestBoardRotation(unittest.TestCase):
    """248 boards with full content cost 456 seconds, and the count only grows
    with every sweep. A company's board barely changes overnight."""

    def setUp(self):
        self.conn = temp_db()
        for i, (ats, slug, checked) in enumerate([
                ("greenhouse", "old", 100), ("ashby", "older", 50),
                ("lever", "newest", 900), ("greenhouse", "mid", 400)]):
            self.conn.execute(
                "INSERT INTO board (company, slug, ats, live, found_at, checked_at) "
                "VALUES (?, ?, ?, 1, 0, ?)", (f"C{i}", slug, ats, checked))
        self.conn.commit()

    def test_oldest_checked_go_first(self):
        from sources import ats
        boards = [{"name": "C1", "ats": "greenhouse", "slug": "old"},
                  {"name": "C2", "ats": "ashby", "slug": "older"},
                  {"name": "C3", "ats": "lever", "slug": "newest"}]
        picked = ats._rotate(self.conn, list(boards), 2)
        self.assertEqual([p["slug"] for p in picked], ["older", "old"])

    def test_picking_stamps_them_so_they_go_last_next_time(self):
        from sources import ats
        boards = [{"name": "C2", "ats": "ashby", "slug": "older"},
                  {"name": "C1", "ats": "greenhouse", "slug": "old"}]
        ats._rotate(self.conn, list(boards), 1)
        again = ats._rotate(self.conn, list(boards), 1)
        self.assertEqual(again[0]["slug"], "old")

    def test_a_hand_curated_entry_with_no_row_is_always_checked(self):
        """companies.yml is the list somebody chose; it sorts oldest-first
        because it has no checked_at at all."""
        from sources import ats
        boards = [{"name": "C3", "ats": "lever", "slug": "newest"},
                  {"name": "Curated", "ats": "greenhouse", "slug": "curated"}]
        picked = ats._rotate(self.conn, list(boards), 1)
        self.assertEqual(picked[0]["slug"], "curated")


class TestGermanEligibility(unittest.TestCase):
    """A third of the undecided Arbeitnow jobs are written in German, where
    every English rule is blind."""

    def d(self, desc):
        return elig.decide("", desc, "Engineer", "https://x/1", [])

    def test_weltweit_is_worldwide(self):
        self.assertEqual(
            self.d("Wir arbeiten weltweit und ortsunabhängig.").state,
            "OPEN_WORLDWIDE")

    def test_visum_is_sponsorship(self):
        self.assertEqual(
            self.d("Wir bieten Visumsponsoring für neue Mitarbeiter.").state,
            "ONSITE_SPONSORED")

    def test_german_residency_requirement_blocks(self):
        self.assertEqual(self.d("Wohnsitz in Deutschland erforderlich.").state,
                         "BLOCKED")

    def test_german_negation_is_honoured(self):
        """Every permissive phrase has a negated form that contains it, in
        German as in English."""
        self.assertNotEqual(self.d("Leider keine Arbeitserlaubnis möglich.").state,
                            "ONSITE_SPONSORED")

    def test_the_work_cue_speaks_german(self):
        """The guard that stops product copy being read as hiring policy was
        English-only, so it rejected every German sentence before the German
        rules could be believed — the whole rule set was dead on its first run
        with the patterns matching and the guard throwing the result away."""
        self.assertEqual(
            self.d("Unsere Software funktioniert weltweit.").state, "UNKNOWN")
        self.assertEqual(
            self.d("Sie arbeiten weltweit in unserem Team.").state,
            "OPEN_WORLDWIDE")


class TestCompanyInferredGeography(unittest.TestCase):

    def setUp(self):
        self.conn = temp_db()
        for i in range(1, 5):
            self.conn.execute(
                "INSERT INTO job (id, dedupe_key, title, company, company_slug, "
                "first_seen, last_seen) VALUES (?, ?, 'T', 'Acme', 'acme', 0, 0)",
                (i, f"k{i}"))
        self.conn.commit()

    def _geo(self, job_id, country):
        self.conn.execute(
            "INSERT INTO geo (job_id, country, precision, decided_at) "
            "VALUES (?, ?, 'country', 0)", (job_id, country))
        self.conn.commit()

    def test_agreeing_siblings_place_the_orphan(self):
        self._geo(1, "DE"), self._geo(2, "DE"), self._geo(3, "DE")
        self.assertEqual(geo.company_country(self.conn, "acme", 4), ("DE", 3))

    def test_one_sibling_is_not_evidence(self):
        self._geo(1, "DE")
        self.assertEqual(geo.company_country(self.conn, "acme", 4), (None, 0))

    def test_a_company_spread_across_countries_tells_us_nothing(self):
        self._geo(1, "DE"), self._geo(2, "US"), self._geo(3, "IN")
        self.assertEqual(geo.company_country(self.conn, "acme", 4), (None, 0))

    def test_the_job_itself_is_excluded(self):
        self._geo(1, "DE"), self._geo(2, "DE")
        self.assertEqual(geo.company_country(self.conn, "acme", 1), (None, 0))


class TestCompanySummary(unittest.TestCase):
    """The one sentence in a cover letter that could not have been sent to
    anyone else — and the one that was left blank."""

    def test_about_the_role_is_not_about_the_company(self):
        """Matching "About the role" put the job description into the
        why-this-company sentence."""
        self.assertIsNone(companies_mod.ABOUT_BLOCK.search(
            "About the role\n\nYou will write content for our clients and own "
            "the editorial calendar end to end, working with our team daily."))

    def test_about_us_is(self):
        match = companies_mod.ABOUT_BLOCK.search(
            "About us\n\nAcme is a platform that helps small businesses run "
            "payroll and benefits without hiring an accountant to do it.")
        self.assertIsNotNone(match)
        self.assertIn("platform", match.group(1))

    def test_values_boilerplate_is_refused(self):
        """"Excellence Surpass expectations to delight our customers" is worse
        in a letter than an empty bracket."""
        self.assertEqual(companies_mod.summarise(
            "<p>Excellence. Surpass expectations to delight our customers at "
            "every step of the journey and beyond.</p>"), "")

    def test_a_guessed_domain_must_prove_itself(self):
        """A guessed domain is often a parking page or an unrelated business
        with the same short name."""
        self.assertTrue(companies_mod._mentions("welcome to Acme Corp", "Acme"))
        self.assertFalse(companies_mod._mentions("domain for sale", "Acme"))


# -------------------------------------------------------------- the board --

class TestBoardSecurity(unittest.TestCase):
    """The findings a security review confirmed, kept as tests. Each docstring
    says what an attacker would have done."""

    def test_secrets_are_gitignored(self):
        """settings.yml carries a phone and email; .board-token is the whole
        database; kits/ names every company applied to. This repo has a remote."""
        ignored = (HERE / ".gitignore").read_text()
        for path in (".board-token", "settings.yml", "kits/", "jobscout.db",
                     ".board-key.pem", ".board-cert.pem", ".board-extension"):
            with self.subTest(path=path):
                self.assertIn(path, ignored)

    def test_user_agent_carries_no_identity(self):
        """It used to contain an email address, and it is sent to all eight
        job boards on every request. A board could tie the scraping to the
        application."""
        from sources import settings as _settings
        ua = _settings()["http"]["user_agent"]
        self.assertNotRegex(ua, r"[\w.+-]+@[\w-]+\.\w+")
        self.assertNotIn("example.com", ua.lower())

    def test_token_comparison_is_constant_time(self):
        """Plain == short-circuits on the first differing byte, so response
        time leaks a prefix and the token is guessable one character at a
        time. Over a LAN that is practical."""
        source = (HERE / "board.py").read_text()
        auth = source[source.index("def _authorised"):source.index("def _host_ok")]
        self.assertIn("compare_digest", auth)
        self.assertNotIn("== want", auth)

    def test_csrf_needs_a_header_a_simple_request_cannot_set(self):
        """SameSite=Strict is scheme + registrable domain and ignores the port,
        so anything else on localhost — a dev server on :3000 — is same-site
        and its POST carries the cookie."""
        source = (HERE / "board.py").read_text()
        self.assertIn("X-Jobscout-Token", source)
        self.assertIn("Sec-Fetch-Site", source)
        post = source[source.index("def do_POST"):]
        self.assertIn("_csrf_ok", post[:400])

    def test_host_is_checked_in_every_mode(self):
        """It used to return True immediately on localhost, leaving DNS
        rebinding open."""
        source = (HERE / "board.py").read_text()
        host = source[source.index("def _host_ok"):source.index("def _send")]
        self.assertNotIn("if not self.lan:", host)

    def test_apply_links_are_scheme_allow_listed(self):
        """Every URL here came out of a job advert, and anyone can post one. A
        javascript: apply link would run in the page holding the session
        cookie for the whole database."""
        source = (HERE / "static" / "app.js").read_text()
        self.assertIn("safeUrl", source)
        self.assertIn("'http:', 'https:', 'mailto:'", source)
        self.assertNotIn("innerHTML", source)          # data never becomes markup

    def test_lan_mints_a_fresh_token(self):
        """A static token is a permanent skeleton key. TLS stops it being read
        off the wire; it does not help against a screenshot, a phone's history
        or a shoulder. One that dies with the server is not a skeleton key."""
        source = (HERE / "board.py").read_text()
        self.assertIn("TOKEN_FILE.unlink(missing_ok=True)", source)
        self.assertIn("keep_token", source)

    def test_session_cookie_has_no_max_age(self):
        """It lasted a week, so a cookie captured once stayed usable for seven
        days. The printed URL always carries ?t=, so nothing is lost."""
        source = (HERE / "board.py").read_text()
        cookie = source[source.index("Set-Cookie"):source.index("Set-Cookie") + 300]
        self.assertNotIn("Max-Age", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_lan_serves_tls(self):
        """Four independent verifiers confirmed the same finding: --lan bound
        0.0.0.0 and spoke plain HTTP, so the bearer token crossed the network
        in clear text."""
        source = (HERE / "board.py").read_text()
        self.assertIn("ssl.SSLContext", source)
        self.assertIn("wrap_socket", source)
        self.assertIn("ensure_cert", source)

    def test_nothing_in_this_system_can_send(self):
        """The standing guarantee: jobscout finds, ranks and prepares. It never
        writes to an employer. Asserted here rather than checked by hand,
        because the shell version of this check was itself wrong — `if grep ...
        | head` takes head's exit status, which succeeds on no matches, so it
        reported a send path in a codebase that has never had one."""
        offenders = []
        for path in HERE.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            body = path.read_text()
            for needle in ("smtplib", "sendmail", "SMTP(", "send_message("):
                if needle in body:
                    offenders.append(f"{path.name}: {needle}")
        for path in (HERE / "sources").glob("*.py"):
            body = path.read_text()
            for needle in ("smtplib", "sendmail", "SMTP("):
                if needle in body:
                    offenders.append(f"sources/{path.name}: {needle}")
        self.assertEqual(offenders, [])

    def test_no_inline_styles_anywhere_in_the_client(self):
        """The CSP forbids them, so an inline style is not a style — it is a
        silently missing one. This is how the choropleth shading and every
        analytics bar width came to do nothing at all."""
        for name in ("static/app.js", "static/index.html"):
            self.assertNotIn('style="', (HERE / name).read_text(), name)
        self.assertNotIn(".style.", (HERE / "static" / "app.js").read_text())


# ------------------------------------------------------- end to end shape --

class TestPipeline(unittest.TestCase):
    """One posting, all the way through, asserting the invariant that matters:
    nothing reaches a sendable state without evidence."""

    def test_full_pass(self):
        conn = temp_db()
        conn.execute(
            "INSERT INTO posting (source, source_id, url, title, company, "
            "description, location_raw, first_seen, last_seen, raw) "
            "VALUES ('himalayas','1','https://x/1','Backend Engineer','Acme',"
            "'A good job.','Anywhere in the World',0,0,?)",
            (json.dumps({"salary_raw": {"min": 96000, "max": 120000,
                                        "currency": "USD", "period": "year"}}),))
        conn.commit()

        norm.build(conn)
        sal.apply(conn)
        elig.apply(conn)

        row = conn.execute(
            "SELECT j.title, e.state, e.evidence_quote, e.evidence_url, "
            "s.known, s.min_usd_month FROM job j "
            "JOIN eligibility e ON e.job_id = j.id "
            "JOIN salary s ON s.job_id = j.id").fetchone()
        self.assertEqual(row["state"], "OPEN_WORLDWIDE")
        self.assertTrue(row["evidence_quote"])
        self.assertTrue(row["evidence_url"])
        self.assertEqual(row["known"], 1)
        self.assertAlmostEqual(row["min_usd_month"], 8000, places=0)

    def test_no_sendable_row_lacks_evidence_in_the_live_db(self):
        """The invariant, asserted against the real database if one exists.
        Skipped rather than failed on a fresh checkout."""
        db = HERE / "jobscout.db"
        if not db.exists():
            self.skipTest("no live database yet")
        # Read-only: running the suite must never migrate or write production.
        conn = connect(db, readonly=True)
        self.addCleanup(conn.close)
        placeholders = ",".join("?" * len(elig.SENDABLE))
        bad = conn.execute(
            f"SELECT COUNT(*) c FROM eligibility WHERE state IN ({placeholders}) "
            "AND (evidence_quote IS NULL OR trim(evidence_quote) = '' "
            "     OR evidence_url IS NULL OR trim(evidence_url) = '')",
            elig.SENDABLE).fetchone()["c"]
        self.assertEqual(bad, 0)


# ------------------------------------------------------------------ forms --

# The real Hungryroot wording, which reached the board at score 72 wearing
# "your level" and which he cannot take. Every clause here is quoted from the
# live posting, so a future edit to the rules is checked against the thing that
# actually got through rather than against a paraphrase of it.
HUNGRYROOT = (
    "Hungryroot is a distributed team of top talent across 28+ U.S. states. "
    "The expected hours are Mon-Fri 3pm-11pm EST, with every other weekend on "
    "call. You can work remotely anywhere in the US. "
    "The employer will not sponsor applicants for work visas."
)


class TestUsOnlyBoilerplate(unittest.TestCase):
    """The three phrases that leaked. Each one on its own must close the gate."""

    def test_will_not_sponsor_blocks(self):
        d = elig.decide("Remote; HQ", HUNGRYROOT, "Systems Analyst", "http://x", [])
        self.assertEqual(d.state, "BLOCKED")
        self.assertIn("sponsor", d.quote.lower())

    def test_each_phrase_blocks_on_its_own(self):
        for phrase in ("The employer will not sponsor applicants for work visas.",
                       "You can work remotely anywhere in the US.",
                       "a distributed team across 28+ U.S. states"):
            with self.subTest(phrase=phrase):
                d = elig.decide("Remote", phrase, "Analyst", "http://x", [])
                self.assertEqual(d.state, "BLOCKED", phrase)

    def test_or_abroad_is_not_a_geo_lock(self):
        """"anywhere in the United States or abroad" is an *open* job. One real
        advert says exactly this, and the first version of the rule blocked it."""
        d = elig.decide("Remote", "We hire anyone based anywhere in the United "
                        "States or abroad, working US hours.", "Ops", "http://x", [])
        self.assertNotEqual(d.state, "BLOCKED")

    def test_worldwide_still_survives(self):
        d = elig.decide("Anywhere in the World", "We are a fully distributed "
                        "company and hire from anywhere in the world.",
                        "Engineer", "http://x", [])
        self.assertEqual(d.state, "OPEN_WORLDWIDE")


# Greenhouse's real shape, trimmed. Questions nest fields; a field carries the
# type and, for a select, the options.
GREENHOUSE_FORM = {
    "title": "Systems Operations Support Analyst",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file"}]},
        {"label": "Are you legally authorized to work in the United States?",
         "required": True,
         "fields": [{"name": "q1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1},
                                {"label": "No", "value": 0}]}]},
        {"label": "Will you now or in the future require sponsorship for "
                  "employment visa status?", "required": True,
         "fields": [{"name": "q2", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1},
                                {"label": "No", "value": 0}]}]},
        {"label": "How many years of Python experience do you have?",
         "required": True, "fields": [{"name": "q3", "type": "input_text"}]},
    ],
}


class TestForms(unittest.TestCase):

    def test_parse_keeps_order_type_and_options(self):
        rows = forms.parse(GREENHOUSE_FORM)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["label"], "First Name")
        self.assertEqual(rows[1]["field_type"], "input_file")
        self.assertEqual(rows[2]["values"], ["Yes", "No"])
        self.assertTrue(all(r["required"] in (0, 1) for r in rows))

    def test_parse_survives_a_form_with_no_questions(self):
        self.assertEqual(forms.parse({"title": "x"}), [])

    def test_work_authorisation_is_a_blocker(self):
        blockers, _ = forms.classify(forms.parse(GREENHOUSE_FORM))
        self.assertTrue(blockers)
        self.assertIn("authorized to work", blockers[0])

    def test_sponsorship_is_flagged_not_blocked(self):
        """He *can* answer it. It is often the filter, so he should see it --
        but a question he can answer honestly is not a closed door."""
        only = {"questions": [q for q in GREENHOUSE_FORM["questions"]
                              if "sponsorship" in q["label"]]}
        blockers, flags = forms.classify(forms.parse(only))
        self.assertEqual(blockers, [])
        self.assertTrue(flags)

    def test_sponsorship_phrased_as_right_to_work_is_not_a_blocker(self):
        """ClickHouse asks "Will you require sponsorship ... for your right to
        work?". It contains "right to work", and it is a sponsorship question:
        the honest answer is yes and they may still hire. The first version of
        this rule deleted four such jobs silently."""
        form = {"questions": [{
            "label": "Will you now or in future require sponsorship from "
                     "ClickHouse for your right to work in the United States?",
            "required": True,
            "fields": [{"name": "q", "type": "multi_value_single_select",
                        "values": [{"label": "Yes"}, {"label": "No"}]}]}]}
        blockers, flags = forms.classify(forms.parse(form))
        self.assertEqual(blockers, [])
        self.assertTrue(flags)

    def test_a_plain_work_authorisation_question_still_blocks(self):
        form = {"questions": [{
            "label": "Are you legally authorized to work in the United States?",
            "required": True,
            "fields": [{"name": "q", "type": "multi_value_single_select",
                        "values": [{"label": "Yes"}, {"label": "No"}]}]}]}
        blockers, _ = forms.classify(forms.parse(form))
        self.assertTrue(blockers)

    def test_optional_questions_never_block(self):
        loose = {"questions": [dict(q, required=False)
                               for q in GREENHOUSE_FORM["questions"]]}
        blockers, _ = forms.classify(forms.parse(loose))
        self.assertEqual(blockers, [])

    def test_board_slug_and_id_come_from_two_columns(self):
        """source_id has the vendor and the id; only the URL has the slug."""
        self.assertEqual(
            forms._ref("greenhouse:6145795004",
                       "https://job-boards.greenhouse.io/hungryroot/jobs/6145795004"),
            ("greenhouse", "hungryroot", "6145795004"))

    def test_ashby_ref_reads_uuids_not_digits(self):
        self.assertEqual(
            forms._ref("ashby:1adfb275-0882-4b2e-9044-2b735a5d2e2d",
                       "https://jobs.ashbyhq.com/omnea/1adfb275-0882-4b2e-9044-2b735a5d2e2d"),
            ("ashby", "omnea", "1adfb275-0882-4b2e-9044-2b735a5d2e2d"))

    def test_a_board_with_no_public_form_has_no_ref(self):
        """Lever publishes an applyUrl and nothing behind it."""
        self.assertIsNone(forms._ref("lever:abc", "https://jobs.lever.co/x/abc"))

    def test_a_greenhouse_id_must_be_numeric(self):
        self.assertIsNone(forms._ref(
            "greenhouse:not-a-number",
            "https://job-boards.greenhouse.io/x/jobs/not-a-number"))


# Ashby's real shape: `field` is a raw JSON blob and `isRequired` sits on the
# entry outside it, which is the opposite of Greenhouse.
ASHBY_FORM = {"data": {"jobPosting": {"title": "Product Engineer",
    "applicationForm": {"sections": [
        {"fieldEntries": [
            {"isRequired": True,
             "field": {"title": "Name", "type": "String", "path": "_systemfield_name"}},
            {"isRequired": True,
             "field": {"title": "Resume", "type": "File", "path": "_systemfield_resume"}},
            {"isRequired": True,
             "field": {"title": "Do you require a visa or work permit to carry "
                                "out this role in the advertised location?",
                       "type": "ValueSelect",
                       "selectableValues": [
                           {"label": "No, I already have the necessary right to "
                                     "work in this location."},
                           {"label": "I would need Omnea to provide a visa or "
                                     "other work authorisation support."}]}},
            {"isRequired": False,
             "field": {"title": "GitHub Profile URL", "type": "String"}}]}]}}}}


class TestAshbyForms(unittest.TestCase):

    def test_parse_reads_entry_level_required(self):
        rows = forms.parse_ashby(ASHBY_FORM)
        self.assertEqual([r["label"] for r in rows][:2], ["Name", "Resume"])
        self.assertEqual(rows[0]["required"], 1)
        self.assertEqual(rows[3]["required"], 0)
        self.assertEqual(rows[1]["field_type"], "File")

    def test_parse_survives_an_empty_form(self):
        self.assertEqual(forms.parse_ashby({"data": {"jobPosting": None}}), [])
        self.assertEqual(forms.parse_ashby({}), [])

    def test_a_visa_question_without_the_word_sponsor_still_routes(self):
        """Omnea asks "Do you require a visa or work permit...". It never says
        sponsorship, and the first version of the routes missed it entirely."""
        rows = forms.parse_ashby(ASHBY_FORM)
        visa = next(r for r in rows if "visa" in r["label"].lower())
        answer = kit.answer_for(None, {"state": "OPEN_WORLDWIDE"},
                                dict(visa, required=1),
                                {"cover_letter": "", "hours": "", "why_company": "",
                                 "start_date": "", "lines": {}})
        self.assertIn("would need", answer.lower())

    def test_the_sponsorship_choice_is_never_the_reassuring_lie(self):
        """"No, I already have the right to work" is the first option and the
        false one. Picking by first word would have chosen it."""
        chosen = kit._sponsorship_choice([
            "No, I already have the necessary right to work in this location.",
            "I would need Omnea to provide a visa or other work authorisation support."])
        self.assertTrue(chosen.startswith("I would need"))

    def test_a_single_name_field_gets_his_whole_name(self):
        who = sources.settings()["candidate"]
        answer = kit.answer_for(None, {}, {"label": "Name", "field_type": "String",
                                           "required": 1, "values": []},
                                {"cover_letter": "", "hours": "", "why_company": "",
                                 "start_date": "", "lines": {}})
        self.assertEqual(answer, who["name"])

    def test_both_vendors_call_an_upload_an_upload(self):
        self.assertTrue(kit._is_file({"field_type": "File"}))
        self.assertTrue(kit._is_file({"field_type": "input_file"}))
        self.assertFalse(kit._is_file({"field_type": "textarea"}))


class TestWhyCompanyIsAboutThem(unittest.TestCase):
    """companies.py takes the first usable sentence off the advert, and that
    sentence is sometimes addressed to the candidate. Quoting it back as the
    reason he wants the job reads as nonsense, so it must be refused."""

    def test_requirement_sentences_are_refused(self):
        for bad in ("You've spent 2 to 3 years in a chief-of-staff role, product "
                    "operations, or consulting, ideally in B2B SaaS.",
                    "You have 3+ years of experience in visual or product design.",
                    "We are looking for an experienced engineer to join the team."):
            with self.subTest(bad=bad[:40]):
                self.assertTrue(kit.NOT_ABOUT_THEM.search(bad), bad)

    def test_real_company_sentences_survive(self):
        for good in ("Hungryroot is using AI to build the most consumer-centric "
                     "food and wellness company to ever exist.",
                     "PlanetScale is the world's most advanced serverless MySQL "
                     "platform for developers.",
                     "Our mission is to create reliable, interpretable and "
                     "steerable AI systems."):
            with self.subTest(good=good[:40]):
                self.assertIsNone(kit.NOT_ABOUT_THEM.search(good), good)

    def test_cache_round_trips(self):
        conn = temp_db()
        conn.execute("INSERT INTO job (id, dedupe_key, title, company, "
                     "company_slug, first_seen, last_seen) "
                     "VALUES (1,'k','T','C','c',0,0)")
        forms._store(conn, 1, forms.parse(GREENHOUSE_FORM))
        back = forms.cached(conn, 1)
        self.assertEqual([r["label"] for r in back],
                         [r["label"] for r in forms.parse(GREENHOUSE_FORM)])
        self.assertEqual(back[2]["values"], ["Yes", "No"])


class TestFormRouting(unittest.TestCase):
    """Each real question goes to an answer that already exists, or to a marker.
    What it must never do is answer with something nobody wrote."""

    def _ctx(self):
        return {"cover_letter": "LETTER", "hours": "HOURS", "why_company": "WHY",
                "start_date": "START", "lines": kit.load_lines()}

    def _answer(self, label, field_type="input_text", values=None):
        return kit.answer_for(None, {"state": "OPEN_WORLDWIDE"},
                              {"label": label, "field_type": field_type,
                               "required": 1, "values": values or []},
                              self._ctx())

    def test_identity_comes_from_settings(self):
        who = sources.settings()["candidate"]
        self.assertEqual(self._answer("Email"), who["email"])
        self.assertEqual(self._answer("Phone"), who["phone"])
        self.assertIn(self._answer("First Name"), who["name"])

    def test_work_authorisation_answers_no_truthfully(self):
        self.assertEqual(
            self._answer("Are you legally authorized to work in the United States?",
                         "multi_value_single_select", ["Yes", "No"]), "No")

    def test_sponsorship_answers_yes_truthfully(self):
        self.assertEqual(
            self._answer("Will you now or in the future require sponsorship?",
                         "multi_value_single_select", ["Yes", "No"]), "Yes")

    def test_bare_why_company_routes_home(self):
        """'Why Anthropic?' has no verb in it and asks the oldest question there
        is. Thirty of the forms on file phrase it exactly this way."""
        self.assertEqual(self._answer("Why Anthropic?"), "WHY")

    def test_a_question_with_no_source_gets_a_marker_not_a_sentence(self):
        self.assertEqual(self._answer("How many years of Kubernetes do you have?"), "")

    def test_prepared_lines_fill_in_when_they_match(self):
        lines = {"How much of this did AI build?": "PREPARED ANSWER"}
        self.assertEqual(kit.match_line("How much of this did AI build?", lines),
                         "PREPARED ANSWER")

    def test_a_weak_overlap_is_refused(self):
        """Half-relevant is worse than blank -- he would not notice the seam."""
        lines = {"Tell me about yourself (90 sec)": "PREPARED"}
        self.assertEqual(kit.match_line("What is your notice period?", lines), "")

    def test_sections_mark_required_gaps(self):
        fields = forms.parse(GREENHOUSE_FORM)
        out = kit.sections_from_form(None, {"state": "OPEN_WORLDWIDE"},
                                     fields, self._ctx())
        self.assertEqual(len(out), len(fields))
        headings = " ".join(h for h, _ in out)
        self.assertIn("*required*", headings)
        gaps = [b for _, b in out if b.startswith(kit.NEEDS_YOU)]
        self.assertTrue(gaps, "the years-of-Python question has no honest source")



# ------------------------------------------------------------- board API --

class TestBoardApi(unittest.TestCase):
    """The list endpoint's search, sort and paging, and the write helper the
    review buttons share. Three real postings pushed through the real stages,
    so the rows carry every column JOB_SELECT joins."""

    @classmethod
    def setUpClass(cls):
        import board
        cls.board = board
        cls.conn = temp_db()
        rows = [("1", "Backend Engineer", "Acme", 96000, 120000),
                ("2", "Data Engineer", "Globex 100% Remote", 60000, 72000),
                ("3", "Platform Engineer", "Initech", 150000, 180000)]
        for sid, title, company, lo, hi in rows:
            cls.conn.execute(
                "INSERT INTO posting (source, source_id, url, title, company, "
                "description, location_raw, first_seen, last_seen, raw) "
                "VALUES ('himalayas',?,?,?,?,'We hire from anywhere in the world. "
                "Fully remote.','Anywhere in the World',?,?,?)",
                (sid, f"https://x/{sid}", title, company, now(), now(),
                 json.dumps({"salary_raw": {"min": lo, "max": hi,
                                            "currency": "USD", "period": "year"}})))
        cls.conn.commit()
        norm.build(cls.conn)
        sal.apply(cls.conn)
        elig.apply(cls.conn)
        geo.apply(cls.conn)
        fit.apply(cls.conn)
        score.apply(cls.conn)

    def test_every_fixture_row_is_viable_and_listed(self):
        items, total = self.board.jobs(self.conn)
        self.assertEqual(total, 3)
        self.assertEqual(len(items), 3)

    def test_search_matches_title_or_company(self):
        items, total = self.board.jobs(self.conn, q="data")
        self.assertEqual([i["title"] for i in items], ["Data Engineer"])
        items, total = self.board.jobs(self.conn, q="initech")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["company"], "Initech")

    def test_search_escapes_like_wildcards(self):
        """`100%` is the string, not "anything starting with 100"."""
        _, total = self.board.jobs(self.conn, q="100%")
        self.assertEqual(total, 1)
        _, total = self.board.jobs(self.conn, q="1%")
        self.assertEqual(total, 0)

    def test_sort_by_pay_is_descending_and_unknown_sort_falls_back(self):
        items, _ = self.board.jobs(self.conn, sort="pay")
        self.assertEqual([i["company"] for i in items], ["Initech", "Acme", "Globex 100% Remote"])
        by_score, _ = self.board.jobs(self.conn, sort="not-a-sort")
        default, _ = self.board.jobs(self.conn)
        self.assertEqual([i["id"] for i in by_score], [i["id"] for i in default])

    def test_offset_pages_and_total_is_the_unpaged_count(self):
        page1, total = self.board.jobs(self.conn, sort="pay", limit=2, offset=0)
        page2, total2 = self.board.jobs(self.conn, sort="pay", limit=2, offset=2)
        self.assertEqual((total, total2), (3, 3))
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 1)
        self.assertEqual(page2[0]["company"], "Globex 100% Remote")

    def test_settle_writes_a_human_decision_and_refuses_missing_jobs(self):
        job_id = self.conn.execute("SELECT id FROM job WHERE company='Acme'").fetchone()["id"]
        self.assertTrue(self.board.settle(self.conn, job_id, "BLOCKED", "checked by hand"))
        row = self.conn.execute("SELECT state, decided_by, evidence_quote FROM eligibility "
                                "WHERE job_id = ?", (job_id,)).fetchone()
        self.assertEqual((row["state"], row["decided_by"], row["evidence_quote"]),
                         ("BLOCKED", "human", "checked by hand"))
        self.assertFalse(self.board.settle(self.conn, 999999, "BLOCKED"))
        # put it back so the other tests see three viable rows
        self.assertTrue(self.board.settle(self.conn, job_id, "OPEN_WORLDWIDE"))

    def test_static_paths_cannot_escape_the_folder(self):
        b = self.board
        self.assertIsNone(b.static_path("a/../../board.py"))
        self.assertIsNone(b.static_path("../.board-token"))
        inside = b.static_path("app.js")
        self.assertEqual(inside.parent, b.STATIC.resolve())

    def test_route_table_matches_what_it_should_and_nothing_else(self):
        def hit(table, path):
            return next((name for pat, name in table if pat.match(path)), None)
        g = self.board.GET_ROUTES
        self.assertEqual(hit(g, "/"), "get_index")
        self.assertEqual(hit(g, "/index.html"), "get_index")
        self.assertEqual(hit(g, "/api/job/12"), "get_job")
        self.assertEqual(hit(g, "/api/kit/12"), "get_kit")
        self.assertEqual(hit(g, "/static/app.js"), "get_static")
        self.assertEqual(hit(g, "/static/fonts/Inter.woff2"), "get_static")
        self.assertIsNone(hit(g, "/static/../board.py"))
        self.assertIsNone(hit(g, "/board.py"))
        self.assertIsNone(hit(g, "/board.js"))
        self.assertIsNone(hit(g, "/api/job/12/extra"))
        p = self.board.POST_ROUTES
        self.assertEqual(hit(p, "/api/review"), "post_review")
        self.assertIsNone(hit(p, "/api/jobs"))

    def test_cv_routes_are_confined_and_validated(self):
        b = self.board
        self.assertIsNone(b.cv_path("../board"))
        self.assertIsNone(b.cv_path("nope"))
        self.assertIsNone(b.cv_path(""))
        real = b.cv_path(ats.CANONICAL)
        import views
        self.assertEqual(real.parent, views.RESUME_DIR)
        with self.assertRaises(b.HttpError) as cm:
            b.save_cv("nope", "# X")
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(b.HttpError) as cm:
            b.save_cv(ats.CANONICAL, "hello")            # not a CV: refused before any write
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(b.HttpError) as cm:
            b.save_cv(ats.CANONICAL, "x" * (b.CV_MAX_CHARS + 1))
        self.assertEqual(cm.exception.code, 413)

        def hit(table, path):
            return next((name for pat, name in table if pat.match(path)), None)
        self.assertEqual(hit(b.GET_ROUTES, "/api/cv"), "get_cv_list")
        self.assertEqual(hit(b.GET_ROUTES, "/api/cv/tech"), "get_cv")
        self.assertEqual(hit(b.GET_ROUTES, "/api/ats/self"), "get_ats")
        self.assertEqual(hit(b.GET_ROUTES, "/pdf/tech"), "get_pdf")
        self.assertEqual(hit(b.POST_ROUTES, "/api/cv/tech"), "post_cv")
        self.assertEqual(hit(b.POST_ROUTES, "/api/cv/tech/apply"), "post_cv_apply")
        self.assertEqual(hit(b.POST_ROUTES, "/api/cv/tech/render"), "post_cv_render")
        # the PDF is the one response allowed to drop the CSP header
        source = (HERE / "board.py").read_text()
        self.assertEqual(source.count("csp=False"), 1)
        self.assertIn("csp=False", source[source.index("def get_pdf"):source.index("def get_ats")])

    def test_urls_normalise_and_resolve_to_jobs(self):
        import views as b                       # moved out of board.py on 2026-09-08
        self.assertEqual(b.normalise_url("https://www.Example.com/jobs/1/?utm=x#top"), "example.com/jobs/1")
        self.assertEqual(b.normalise_url("https://x/1"), "x/1")
        # the fixture postings are https://x/1..3
        got = b.resolve_url(self.conn, "https://x/2/")
        self.assertEqual(got["company"], "Globex 100% Remote")
        self.assertIsNone(got["applied"])
        self.assertEqual(b.resolve_url(self.conn, "https://nowhere.example/j")["job_id"], None)
        self.assertEqual(b.resolve_url(self.conn, "https://acme.myworkdayjobs.com/en-US/x/job/1")["vendor"], "workday")
        # vendor ids are read out of the URL and matched on posting.source_id
        self.conn.execute("INSERT INTO posting (source, source_id, url, title, company, description, "
                          "first_seen, last_seen, raw, job_id) VALUES ('ats','greenhouse:4242','https://job-boards.greenhouse.io/acme/jobs/4242',"
                          "'Backend Engineer','Acme','x',?,?,'{}',(SELECT id FROM job WHERE company='Acme'))", (now(), now()))
        self.conn.commit()
        for url in ("https://job-boards.greenhouse.io/acme/jobs/4242?gh_src=abc",
                    "https://boards.greenhouse.io/embed/job_app?for=acme&token=4242"):
            with self.subTest(url=url):
                got = b.resolve_url(self.conn, url)
                self.assertEqual((got["company"], got["vendor"]), ("Acme", "greenhouse"))

    def test_answers_for_live_labels_route_flag_and_refuse(self):
        job_id = self.conn.execute("SELECT id FROM job WHERE company='Acme'").fetchone()["id"]
        out = kit.answers_for_labels(self.conn, job_id, [
            {"label": "First Name", "type": "text", "required": True},
            {"label": "Will you require sponsorship?", "type": "select", "required": True, "options": ["Yes", "No"]},
            {"label": "Are you legally authorized to work in the United States?", "type": "select",
             "required": True, "options": ["Yes", "No"]},
            {"label": "Resume/CV", "type": "file", "required": True},
            {"label": "Years of Kubernetes experience", "type": "text", "required": False},
            {"label": "Country", "type": "react-select", "required": False},
        ])
        by = {a["label"]: a for a in out["answers"]}
        self.assertEqual(by["First Name"]["kind"], "first_name")
        self.assertTrue(by["First Name"]["answer"])
        # Sponsorship and work-authorisation are GUARD_KINDS (added with the
        # J4 guard list): kit.py can still work out the honest text, but the
        # extension must never one-click it, so `choice` stays None and
        # `needs_you` stays True however confidently the text was computed.
        self.assertIsNone(by["Will you require sponsorship?"]["choice"])
        self.assertTrue(by["Will you require sponsorship?"]["needs_you"])
        self.assertIsNone(by["Are you legally authorized to work in the United States?"]["choice"])
        self.assertTrue(by["Are you legally authorized to work in the United States?"]["needs_you"])
        self.assertTrue(out["blockers"])                      # the live form rules him out
        self.assertTrue(any("sponsorship" in f for f in out["flags"]))
        self.assertTrue(by["Years of Kubernetes experience"]["needs_you"])
        self.assertEqual(by["Years of Kubernetes experience"]["answer"], "")
        self.assertIn("pdf_url", by["Resume/CV"])
        self.assertEqual(by["Country"]["answer"], "Sri Lanka")
        self.assertEqual(kit.answers_for_labels(self.conn, 999999, []), {})

    def test_extension_origin_rules_are_strict(self):
        source = (HERE / "board.py").read_text()
        self.assertNotIn('Allow-Origin", "*"', source)
        self.assertNotIn("Access-Control-Allow-Origin: *", source)
        auth = source[source.index("def _authorised"):source.index("def _host_ok")]
        self.assertIn("X-Jobscout-Token", auth)
        self.assertIn("compare_digest(header, want)", auth)
        csrf = source[source.index("def _csrf_ok"):source.index("# --- GET")]
        self.assertIn('site == "cross-site" and self._ext_origin() is not None', csrf)
        options = source[source.index("def do_OPTIONS"):source.index("def do_HEAD")]
        self.assertIn("if not ext", options)
        self.assertIn("403", options)
        self.assertEqual(self.board.ext_origins(), set()) if not (HERE / ".board-extension").exists() else None

    def test_asset_cache_key_is_the_file_not_the_request(self):
        css = HERE / "static" / "style.css"
        st = css.stat()
        plain, etag = self.board._asset(str(css), st.st_mtime_ns, st.st_size, False)
        gz, etag2 = self.board._asset(str(css), st.st_mtime_ns, st.st_size, True)
        self.assertEqual(etag, etag2)
        self.assertTrue(etag.startswith('W/"'))
        self.assertLess(len(gz), len(plain))
        import gzip as _gz
        self.assertEqual(_gz.decompress(gz), plain)

    def test_write_routes_sit_behind_csrf_and_a_body_cap(self):
        source = (HERE / "board.py").read_text()
        post = source[source.index("def do_POST"):source.index("def _csrf_ok")]
        self.assertIn("_csrf_ok", post[:400])
        self.assertIn("MAX_BODY", post)
        self.assertEqual(self.board.MAX_BODY, 64 * 1024)



# ------------------------------------------------------------------- ats --

import ats                                            # noqa: E402

ATS_CV_MD = """# Test Person
Colombo · +94 77 000 0000 · test@example.com · [linkedin.com/in/test](https://www.linkedin.com/in/test/)

## Summary

Analyst who gathers requirements and builds the system.

## Technical Skills

**Languages & Runtimes:** Python, SQL, HTML/CSS
**Data & Platforms:** PostgreSQL, Excel (dashboards, pivot tables)

## Work Experience

**Business Technology Associate**, Acme Logistics
Feb 2026 – Present

- Built a Python system that consolidates 98,000+ billing records into one dashboard
- Defined validation logic with warehouse staff and caught a 696% margin error before it reached management
- Responsible for the payment voucher workflow and its migration to PostgreSQL

**Business Analyst Associate**, Startup AI
Mar 2025 – Jul 2025

- Ran UAT with stakeholders across a 3-person Agile team
- Designed an automation pipeline that removed a manual publishing bottleneck
- Wrote functional specifications for the directors

## Education

**Bachelor of Management Information Systems**, Example University *(2023 to August 2026)*
"""

ATS_CV_MD_H3 = """# Test Person
**+94 77 000 0000** · test@example.com

---

## Summary

Analyst who gathers requirements and builds the system.

---

## Technical Skills

- **Languages & runtimes:** Python, SQL, HTML/CSS
- **Data & documents:** PostgreSQL, Excel

---

## Experience

### Business Technology Associate · Acme Logistics *(Logistics)*
**Feb 2026 – Present**

- Built a Python system that consolidates 98,000+ billing records into one dashboard
- Defined validation logic with warehouse staff and caught a 696% margin error before it reached management
- Responsible for the payment voucher workflow and its migration to PostgreSQL

### Business Analyst Associate · Startup AI
**Mar 2025 – Jul 2025**

- Ran UAT with stakeholders across a 3-person Agile team
- Designed an automation pipeline that removed a manual publishing bottleneck
- Wrote functional specifications for the directors

---

## Education

**Bachelor of Management Information Systems**
Example University · Completed August 2026
"""

ATS_CV_MD_DASH = """# Test Person
Colombo · +94 77 000 0000 · test@example.com

## Summary

Analyst who gathers requirements and builds the system.

## Work Experience

**Business Technology Associate** — Acme Logistics
Feb 2026 – Present
- Built a Python system that consolidates 98,000+ billing records into one dashboard
- Defined validation logic with warehouse staff and caught a 696% margin error before it reached management
- Responsible for the payment voucher workflow and its migration to PostgreSQL

**Business Analyst Associate** — Startup AI
Mar 2025 – Jul 2025
- Ran UAT with stakeholders across a 3-person Agile team
- Designed an automation pipeline that removed a manual publishing bottleneck
- Wrote functional specifications for the directors

## Education

**Bachelor of Management Information Systems** — Example University *(Completed August 2026)*

## Languages & Technologies

Excel (dashboards, pivot tables) · Python · SQL (queries, joins) · PostgreSQL
"""

ATS_SIBLING_MD = """# Test Person
Colombo · test@example.com

## Technical Skills

**Data & Platforms:** PostgreSQL, Docker, Excel

## Work Experience

**Business Technology Associate**, Acme Logistics
Feb 2026 – Present

- Owned the payment voucher workflow and its migration to PostgreSQL
- Leveraged the payment voucher workflow and its migration to PostgreSQL
"""

ATS_JD = """Data Platform Analyst

We are a fast-growing team building internal tools. You will work with Python and
PostgreSQL every day, run UAT with stakeholders, and own our Docker-based
deployment. Docker experience matters. Kubernetes experience is a strong plus,
and we use Kubernetes for everything. Kubernetes, again.

Requirements:
- 2+ years with Python and SQL
- Hands-on Docker and Kubernetes
- Comfortable running UAT with business stakeholders
- Experience gathering requirements from non-technical teams

Benefits:
- Remote work
- Annual learning budget
"""


class TestAts(unittest.TestCase):
    """The ATS model: parses every shape the five real variants use, scores
    deterministically, and never proposes a fact that is not already written
    down somewhere in resume/."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ats-")
        root = Path(cls.tmp.name)
        cls.mine = root / "resume-a.md"
        cls.sib = root / "resume-sib.md"
        cls.mine.write_text(ATS_CV_MD)
        cls.sib.write_text(ATS_SIBLING_MD)
        cls.cv = ats.load_cv(cls.mine)
        cls.report = ats.score_all(cls.cv, ATS_JD, None)
        cls.keywords = cls.report["_keywords"]
        cls.inj = ats.injectable_set([cls.mine, cls.sib])
        cls.edits = ats.improve(cls.cv, cls.keywords, cls.inj, cls.report, "Workday")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- parsing ------------------------------------------------------------

    def _check_shape(self, cv):
        self.assertEqual(cv.name, "Test Person")
        exp = cv.experience()
        self.assertEqual([e.title for e in exp],
                         ["Business Technology Associate", "Business Analyst Associate"])
        self.assertEqual([e.org for e in exp], ["Acme Logistics", "Startup AI"])
        self.assertEqual([e.dates for e in exp], ["Feb 2026 – Present", "Mar 2025 – Jul 2025"])
        self.assertEqual([len(e.bullets) for e in exp], [3, 3])
        self.assertEqual(len(cv.bullets()), 6)

    def test_parses_canonical_shape(self):
        cv = ats.parse_cv(ATS_CV_MD)
        self._check_shape(cv)
        self.assertEqual(cv.skills()["Data & Platforms"], ["PostgreSQL", "Excel (dashboards, pivot tables)"])
        self.assertEqual(cv.section("education").entries[0].dates, "2023 to August 2026")

    def test_parses_h3_shape(self):
        cv = ats.parse_cv(ATS_CV_MD_H3)
        self._check_shape(cv)
        self.assertEqual(cv.experience()[0].note, "(Logistics)")
        self.assertEqual(list(cv.skills()), ["Languages & runtimes", "Data & documents"])
        edu = cv.section("education").entries[0]
        self.assertEqual((edu.org, edu.dates), ("Example University", "August 2026"))

    def test_parses_em_dash_shape(self):
        cv = ats.parse_cv(ATS_CV_MD_DASH)
        self._check_shape(cv)
        # a competencies paragraph under a skills header is a skills line
        self.assertEqual(cv.skills()["Languages & Technologies"][0], "Excel (dashboards, pivot tables)")

    def test_every_real_variant_parses(self):
        for info in ats.list_variants():
            cv = ats.load_cv(info["path"])
            with self.subTest(variant=info["slug"]):
                self.assertEqual(cv.name, "Alex Example")
                self.assertGreaterEqual(len(cv.sections), 4)
                self.assertGreaterEqual(len(cv.experience()), 4)
                self.assertGreaterEqual(len(cv.bullets()), 8)
                dated = [e for e in cv.experience() if e.dates]
                self.assertGreaterEqual(len(dated), 4)
                self.assertTrue(cv.skills(), "no skills parsed")

    def test_plain_projection_has_no_markdown(self):
        text = ats.plain_text(self.cv)
        self.assertNotIn("**", text)
        self.assertNotIn("](", text)
        self.assertIn("Business Technology Associate, Acme Logistics\nFeb 2026 – Present", text)
        self.assertIn("Data & Platforms: PostgreSQL, Excel (dashboards, pivot tables)", text)

    # -- keywords -----------------------------------------------------------

    def test_jd_keywords_finds_skills_and_the_required_block(self):
        by = {k.term: k for k in self.keywords}
        self.assertIn("kubernetes", by)
        self.assertTrue(by["kubernetes"].required)
        self.assertEqual(by["kubernetes"].kind, "skill")
        self.assertGreater(by["kubernetes"].weight, by["postgresql"].weight)
        self.assertNotIn("annual", by)          # filler is never a keyword
        self.assertNotIn("team", by)
        self.assertIn("gathering requirements", by)
        self.assertNotIn("gathering", by)       # a word inside a kept phrase is dropped

    def test_exact_is_never_more_generous_than_semantic(self):
        s = self.report["keywords"]["score_by_strategy"]
        self.assertLessEqual(s["exact"], s["fuzzy"])
        self.assertLessEqual(s["fuzzy"], s["semantic"])
        self.assertEqual((s["exact"], s["fuzzy"], s["semantic"]), (50, 50, 55))

    def test_synonyms_count_under_fuzzy_but_not_exact(self):
        kws = [ats.Keyword("postgresql", 1, "skill", 3.0, False)]
        self.assertEqual(ats.match_keywords("worked with Postgres daily", kws, "exact").score, 0)
        self.assertEqual(ats.match_keywords("worked with Postgres daily", kws, "fuzzy").score, 80)

    # -- platforms ----------------------------------------------------------

    def test_known_profile_outputs(self):
        """The regression lock: change a weight and this tells you."""
        got = {p["name"]: p["score"] for p in self.report["platforms"]}
        self.assertEqual(got, {"Workday": 78, "Taleo": 76, "iCIMS": 75,
                               "SuccessFactors": 75, "Greenhouse": 72, "Lever": 81})
        by = {p["name"]: p for p in self.report["platforms"]}
        self.assertFalse(by["Greenhouse"]["auto_scores"])
        self.assertFalse(by["Lever"]["auto_scores"])
        self.assertIn("does not score", by["Greenhouse"]["note"])
        self.assertTrue(by["Workday"]["auto_scores"])
        self.assertTrue(all(p["passes"] for p in self.report["platforms"]))

    def test_formatting_penalties_scale_with_strictness(self):
        clean = ats.signals_from_cv(self.cv)
        cols = ats.signals_from_cv(self.cv)
        cols.multi_column = True
        for strictness in (0.9, 0.35):
            base = ats.score_formatting(clean, strictness)["score"]
            hit = ats.score_formatting(cols, strictness)["score"]
            self.assertAlmostEqual(base - hit, 15 * strictness, delta=1)

    def test_date_format_quirk_uses_profile_preferences(self):
        sig = ats.signals_from_cv(self.cv)
        sig.entry_date_formats = ["YYYY", "YYYY"]
        sf = next(p for p in ats.PROFILES if p.name == "SuccessFactors")
        lever = next(p for p in ats.PROFILES if p.name == "Lever")
        sf_q = [q["id"] for q in ats.score_platform(sig, None, sf)["quirks"]]
        lever_q = [q["id"] for q in ats.score_platform(sig, None, lever)["quirks"]]
        self.assertIn("date-format", sf_q)
        self.assertNotIn("date-format", lever_q)

    def test_no_jd_means_no_keyword_component_not_a_fake_hundred(self):
        rep = ats.score_all(self.cv, None, None)
        self.assertIsNone(rep["keywords"])
        for p in rep["platforms"]:
            self.assertIsNone(p["breakdown"]["keywords"])
            self.assertTrue(0 <= p["score"] <= 100)

    # -- the improver -------------------------------------------------------

    def test_every_suggested_term_exists_in_the_injectable_set(self):
        kinds = {e.kind for e in self.edits}
        self.assertIn("add_skill", kinds)
        add = next(e for e in self.edits if e.kind == "add_skill")
        self.assertEqual(add.patch["term"], "docker")
        self.assertEqual(add.evidence[0].file, "resume-sib.md")
        for e in self.edits:
            if e.patch and e.patch["op"] == "append_skill":
                self.assertIn(e.patch["term"].lower(), self.inj.terms)
            if e.patch and e.patch["op"] == "replace_bullet":
                self.assertTrue(self.inj.has_sentence(e.patch["new"]))
        ats.assert_no_fabrication(self.edits, self.inj)     # raises on a lie
        needs = [e for e in self.edits if e.needs_you]
        self.assertTrue(any("kubernetes" in e.summary for e in needs))
        self.assertTrue(all(e.patch is None for e in needs))

    def test_reword_comes_from_a_sibling_and_never_a_banned_verb(self):
        reword = [e for e in self.edits if e.kind == "reword_bullet"]
        self.assertEqual(len(reword), 1)
        self.assertTrue(reword[0].patch["new"].startswith("Owned the payment voucher"))
        self.assertFalse(any(w in ats.BANNED_WORDS for w in ats.tokenize(reword[0].patch["new"])))

    def test_fabrication_is_refused(self):
        bad = ats.Edit("x", "add_skill", "low", [], "", "", [],
                       {"op": "append_skill", "category": "Data & Platforms", "term": "kubernetes"},
                       False, {})
        with self.assertRaises(ValueError):
            ats.assert_no_fabrication([bad], self.inj)

    def test_stuffing_guard(self):
        stuffed = ATS_CV_MD.replace("## Summary\n\n", "## Summary\n\n" + "Python " * 9 + "\n\n")
        cv = ats.parse_cv(stuffed, str(self.mine))
        findings = ats.lint(cv, ["python"])
        self.assertTrue(any(f["rule"] == "keyword-stuffing" and "python" in f["text"] for f in findings))
        rep = ats.score_all(cv, ATS_JD, None)
        edits = ats.improve(cv, rep["_keywords"], self.inj, rep)
        self.assertFalse(any(e.patch and e.patch.get("term") == "python" for e in edits))

    # -- patches ------------------------------------------------------------

    def test_apply_patches_roundtrip(self):
        patches = [e.patch for e in self.edits if e.patch]
        new, applied, rejected = ats.apply_patches(ATS_CV_MD, patches)
        self.assertEqual(rejected, [])
        self.assertEqual(len(applied), len(patches))
        after = ats.parse_cv(new)
        self.assertEqual(len(after.experience()), 2)
        self.assertEqual(len(after.bullets()), 6)
        self.assertIn("docker", [t.lower() for t in after.skills()["Data & Platforms"]])
        self.assertTrue(any(b.startswith("Owned") for _, b in after.bullets()))

    def test_stale_patch_is_rejected_with_a_reason(self):
        add = next(e for e in self.edits if e.kind == "add_skill").patch
        _, applied, rejected = ats.apply_patches(ATS_CV_MD.replace("Data & Platforms", "Platforms"), [add])
        self.assertEqual(applied, [])
        self.assertIn("no single skills line", rejected[0]["reason"])
        stale = {"op": "replace_line", "line": 1, "old": "# Someone Else", "new": "## Skills"}
        _, applied, rejected = ats.apply_patches(ATS_CV_MD, [stale])
        self.assertIn("changed since", rejected[0]["reason"])

    # -- lint ---------------------------------------------------------------

    def test_lint_finds_ing_endings_em_dashes_and_banned_words(self):
        md = ATS_CV_MD.replace("- Wrote functional specifications for the directors",
                               "- Leveraged synergy across teams while improving efficiency and reporting")
        md = md.replace("Analyst who gathers", "Analyst — who — gathers — and — builds")
        findings = ats.lint(ats.parse_cv(md))
        rules = {f["rule"] for f in findings}
        self.assertIn("banned-word", rules)
        self.assertIn("ing-ending", rules)
        self.assertIn("em-dash-count", rules)

    def test_lint_flags_recruiter_hygiene(self):
        md = ("# Someone\nColombo · Age 21 · a@b.co\n\n## Education\n**BSc**, Uni *(2026)*\n\n"
              "## Skills\n**Spoken Languages:** English\n\n## References\n**Mr X**, Director\n")
        rules = {f["rule"] for f in ats.lint(ats.parse_cv(md), pages=2)}
        self.assertTrue({"personal-data", "spoken-languages", "references", "new-grad-length"} <= rules)
        self.assertNotIn("new-grad-length", {f["rule"] for f in ats.lint(ats.parse_cv(md), pages=1)})

    def test_section_headers_flagged_for_exact_parsers(self):
        self.assertIsNone(ats.strict_header_type("Technical Skills & Tools"))
        self.assertEqual(ats.strict_header_type("Work Experience"), "experience")
        self.assertEqual(ats.classify_header("Technical Skills & Tools"), "skills")
        self.assertEqual(ats.classify_header("Key Deliverables"), "projects")



# ---------------------------------------------------------------- helper --

import helper                                          # noqa: E402

# A published extension's public key and the id the browser derived from it
# (read off an installed copy). The derivation must reproduce it exactly.
HELPER_FIXTURE_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAjU1XnLPoasGVmZU42K3h6S+sQhkogfcoLPbIcrWH5Oo8QoInBIugkew/7cWaEFySyQrkaEBe1fjeS/rlAqd3r778dKcTvDZcXmj0VVX0Fi1i8tnkarurceGKGdVxfkL7e30nwfgwoPxj3H8OQbsbxFcBWGVtcFekmdpiyaxwz6o4yXIWColfAxh9K2yToOZkoAS5GvgGvTexiCh1gYy++eFdk6C61mcFsyDdoGQtduhGEaX0zZ9uAW1jX4JTPmHV3kEFrZu/WVBl7Obw+Jk/osoHMdmghVNy6SCB8/6mcgmxkP9buPrNUZgYP6n0x5dqEJ2Ecww/lb1Zd4nQf4XGOwIDAQAB"
HELPER_FIXTURE_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


class TestHelper(unittest.TestCase):
    """The installer and the pairing endpoint: the id is derived, not guessed,
    and the one unauthenticated read is fenced to loopback + the allow-list."""

    def test_extension_id_is_derived_from_the_key(self):
        self.assertEqual(helper.ext_id_from_key(HELPER_FIXTURE_KEY), HELPER_FIXTURE_ID)
        self.assertEqual(len(helper.ext_id_from_key(HELPER_FIXTURE_KEY)), 32)
        self.assertTrue(set(helper.ext_id_from_key(HELPER_FIXTURE_KEY)) <= set("abcdefghijklmnop"))

    def test_pairing_is_loopback_and_allow_list_only(self):
        import board
        allowed = {"chrome-extension://" + HELPER_FIXTURE_ID}
        self.assertTrue(board.pair_allowed("127.0.0.1", "chrome-extension://" + HELPER_FIXTURE_ID, allowed))
        self.assertTrue(board.pair_allowed("::1", "chrome-extension://" + HELPER_FIXTURE_ID, allowed))
        self.assertFalse(board.pair_allowed("192.168.1.20", "chrome-extension://" + HELPER_FIXTURE_ID, allowed))
        self.assertFalse(board.pair_allowed("127.0.0.1", "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", allowed))
        self.assertFalse(board.pair_allowed("127.0.0.1", "", allowed))
        self.assertFalse(board.pair_allowed("127.0.0.1", "https://evil.example", allowed))
        self.assertFalse(board.pair_allowed("127.0.0.1", "chrome-extension://" + HELPER_FIXTURE_ID, set()))
        # Brave sends no Origin from extension contexts: the id rides in ?ext=
        self.assertTrue(board.pair_allowed("127.0.0.1", "", allowed, HELPER_FIXTURE_ID))
        self.assertFalse(board.pair_allowed("127.0.0.1", "", allowed, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
        self.assertFalse(board.pair_allowed("127.0.0.1", "", allowed, "../x"))
        self.assertFalse(board.pair_allowed("10.0.0.5", "", allowed, HELPER_FIXTURE_ID))

    def test_pair_route_sits_before_auth_and_stamps_the_meta_row(self):
        source = (HERE / "board.py").read_text()
        get = source[source.index("def do_GET"):source.index("def do_POST")]
        self.assertLess(get.index('"/api/pair"'), get.index("_authorised"))
        pair = source[source.index("def get_pair"):source.index("def get_resolve")]
        self.assertIn("pair_allowed(", pair)
        self.assertIn("extension_paired_at", pair)
        self.assertIn("403", pair)

    def test_helper_key_and_allowlist_are_gitignored(self):
        ignored = (HERE / ".gitignore").read_text()
        self.assertIn(".helper-key.pem", ignored)
        self.assertIn(".board-extension", ignored)

    def test_manifest_key_when_present_matches_the_allow_list(self):
        manifest = json.loads((HERE / "extension" / "manifest.json").read_text())
        if "key" not in manifest or not (HERE / ".board-extension").exists():
            self.skipTest("helper.py install has not run on this machine")
        ext_id = helper.ext_id_from_key(manifest["key"])
        self.assertIn("chrome-extension://" + ext_id, (HERE / ".board-extension").read_text().split())


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
