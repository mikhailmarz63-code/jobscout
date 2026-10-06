#!/usr/bin/env python3
"""Tests for running jobscout for someone else (person.py, people.py).

The expensive bug here is crossing wires: a run for one person reading the
owner's CV or writing into the owner's database. Most cases below check that
every path moves together, and that with no person set nothing moves at all.
"""
from __future__ import annotations

import importlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import people  # noqa: E402
import person  # noqa: E402

GOOD = """Sure, here it is:
{"name": "Sam Test", "headline": "Data Analyst", "country": "lk",
 "city": "Kandy", "utc_offset": 5.5, "seniority": "Mid", "years_experience": 3,
 "target_titles": ["Data Analyst", "BI Analyst"], "adjacent_titles": ["data engineer"],
 "avoid_titles": ["sales"], "moving_into": [], "skills": ["SQL", "Power BI", "python"]}
"""


class TestParseProfile(unittest.TestCase):

    def test_reads_json_out_of_chatter_and_normalises(self):
        p = people.parse_profile(GOOD)
        self.assertEqual(p["country"], "LK")
        self.assertEqual(p["seniority"], "mid")
        self.assertEqual(p["target_titles"], ["data analyst", "bi analyst"])
        self.assertEqual(p["skills"], ["sql", "power bi", "python"])
        self.assertEqual(p["years_experience"], 3.0)

    def test_a_string_where_a_list_belongs_is_refused(self):
        """It would otherwise match nothing and empty the shortlist silently."""
        with self.assertRaises(ValueError):
            people.parse_profile('{"target_titles": "data analyst"}')

    def test_no_json_is_refused(self):
        with self.assertRaises(ValueError):
            people.parse_profile("I can't read this CV.")

    def test_unknown_seniority_falls_back_to_junior(self):
        self.assertEqual(people.parse_profile('{"seniority": "wizard"}')["seniority"], "junior")


class TestTitleRegex(unittest.TestCase):

    def test_longest_phrase_wins_and_words_are_whole(self):
        rx = person.title_regex(["engineer", "data engineer"])
        self.assertEqual(rx.search("Senior Data Engineer").group(0), "Data Engineer")
        self.assertIsNone(rx.search("Engineering Manager"))

    def test_empty_list_is_none(self):
        self.assertIsNone(person.title_regex([]))
        self.assertIsNone(person.NEVER.search("anything"))


class TestReadCv(unittest.TestCase):

    def test_markdown_and_docx(self):
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / "cv.md"
            md.write_text("# Sam\nhello")
            self.assertIn("hello", people.read_cv(md))
            docx = Path(d) / "cv.docx"
            import zipfile
            with zipfile.ZipFile(docx, "w") as z:
                z.writestr("word/document.xml",
                           "<w:document><w:p><w:t>Sam Test</w:t></w:p>"
                           "<w:p><w:t>SQL &amp; Python</w:t></w:p></w:document>")
            self.assertEqual(people.read_cv(docx).split("\n")[:2], ["Sam Test", "SQL & Python"])

    def test_unknown_format_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cv.pages"
            p.write_text("x")
            with self.assertRaises(SystemExit):
                people.read_cv(p)


class TestPersonSwitch(unittest.TestCase):
    """End to end: `people.py add` with a stand-in `claude`, then every module
    that holds a path must point into that person's folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + GOOD + "\nEOF\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.slug = "zz-test-person"
        self.folder = person.PEOPLE / self.slug
        shutil.rmtree(self.folder, ignore_errors=True)
        cv = self.tmp / "cv.md"
        cv.write_text("# Sam Test\n\n" + "Data analyst with SQL and Power BI. " * 20)
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        env.pop(person.ENV, None)
        out = subprocess.run([sys.executable, str(HERE / "people.py"), "add", str(cv),
                              "--name", self.slug, "--floor", "900"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop(person.ENV, None)

    def test_folder_is_complete(self):
        for name in ("profile.yml", "settings.yml", "resume/resume-cv.md"):
            self.assertTrue((self.folder / name).exists(), name)
        import yaml
        cfg = yaml.safe_load((self.folder / "settings.yml").read_text())
        self.assertEqual(cfg["pay"]["floor_usd_month"], 900)
        self.assertEqual(cfg["candidate"]["city"], "Kandy")
        self.assertEqual(cfg["candidate"]["email"], "")       # never the example's

    def test_every_path_moves_together(self):
        """Checked in a child process so the imports see the variable fresh."""
        code = ("import db, sources, ats, kit, fit, score, person;"
                "print(db.DB); print(sources.SETTINGS); print(ats.RESUME);"
                "print(kit.OUT_DIR); print(ats.CANONICAL);"
                "print(fit.HIS_FAMILY.search('BI Analyst') is not None);"
                "print(fit.SENIORITY, fit.YEARS_HAVE); print(score.SKILLS)")
        env = {**os.environ, person.ENV: self.slug}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=HERE)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().split("\n")
        for line in lines[:4]:
            self.assertIn(f"people/{self.slug}", line)
        self.assertEqual(lines[4], "cv")
        self.assertEqual(lines[5], "True")
        self.assertEqual(lines[6], "mid 3.0")
        self.assertIn("power bi", lines[7])

    def test_reach_follows_their_seniority(self):
        code = "import fit; print(fit.reach('Senior Data Analyst', '')[0])"
        env = {**os.environ, person.ENV: self.slug}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=HERE)
        self.assertEqual(out.stdout.strip(), "plausible", out.stderr)

    def test_an_unknown_person_stops_with_a_message(self):
        env = {**os.environ, person.ENV: "nobody-here"}
        out = subprocess.run([sys.executable, "-c", "import db"], capture_output=True,
                             text=True, env=env, cwd=HERE)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("people.py add", out.stderr)


class TestOwnerUnchanged(unittest.TestCase):

    def test_no_person_means_the_original_paths(self):
        os.environ.pop(person.ENV, None)
        import db
        import ats
        importlib.reload(db)
        importlib.reload(ats)
        self.assertEqual(db.DB, HERE / "jobscout.db")
        self.assertEqual(ats.RESUME, HERE.parent / "resume")
        self.assertEqual(ats.CANONICAL, "ai-solutions-engineer")


if __name__ == "__main__":
    unittest.main()
