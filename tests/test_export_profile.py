"""Tests for tools/export_profile.py - the 01-candidate-profile.md -> profile.json export.

Every test drives the real code path through `main([...])` against a fixture
profile written into a tempfile directory (with its own CLAUDE.md); nothing
touches the repo's own skill files, profile.json or the network.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import export_profile
from tools.export_profile import main

FIXTURE_01 = """---
framework_version: 1.2.0
---

# Candidate Profile

<!-- SETUP: This file is populated by running /setup -->

## Identity
- **Name:** Jane van Doe
- **Location:** Aarhus, Denmark
- **Phone:** +45 12 34 56 78
- **Email:** jane.doe@example.org
- **LinkedIn:** [linkedin.com/in/janedoe](https://www.linkedin.com/in/janedoe)
- **GitHub:** https://github.com/janedoe
- **Status:** Employed, open to offers
- **Constraints:** Within 45 min of Aarhus

### Languages
<!-- Every language you can work in professionally, with your honest level. -->

| Language | Level | Notes |
|----------|-------|-------|
| English | C2 | working language |
| Danish | B1/B2 (conversational) | |

## Screening Answers
<!-- Short factual answers ATS forms ask on every application. -->

| Question | Answer |
|----------|--------|
| Work authorisation | EU citizen, no sponsorship needed |
| Notice period / earliest start | 3 months |
| Salary expectation | DKK 55,000 / month gross |
| Willing to relocate | within Denmark |
| Remote / hybrid / on-site preference | hybrid, 2 days on-site |
| Portfolio URL | [URL or blank] |
| How did you hear about us (default) | Company careers page |
| Security clearance? (Danish forms) | none |

## Education

| Degree | Period | Institution | Key Topics |
|--------|--------|-------------|------------|
| MSc Geophysics | 2014-2016 | Aarhus University | inversion, signal processing |
| BSc Physics | 2011-2014 | Aarhus University | |

## Professional Experience

### Senior Data Scientist - Globex (Mar 2021 - Present)
Aarhus, Denmark
- Built the forecasting platform

### Data Scientist - Acme Corp (2019 - 2022)
Copenhagen
- Shipped models

<!-- Add more roles as needed -->

## Independent Projects
- **Thing**: a thing

## Technical Skills

### Programming & ML
- **Python** (expert): numpy, pytorch

## References
- Someone, CTO, Globex (x@y.z, 123)
"""

FIXTURE_CLAUDE_MD = """# Job Application Assistant for Jane

## Candidate Profile

### Identity
- **Name:** Jane van Doe
- **CV language:** English <!-- English unless your market expects otherwise; /setup asks -->
"""


def run(argv):
    """Run main() capturing stdout/stderr; return (exit_code, parsed_json_or_None, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()


class ExportProfileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.profile = self.repo / export_profile.PROFILE_REL
        self.profile.parent.mkdir(parents=True)
        self.claude_md = self.repo / "CLAUDE.md"
        self.out = self.repo / "profile.json"
        self.write(FIXTURE_01, FIXTURE_CLAUDE_MD)

    def write(self, profile_text, claude_md_text=FIXTURE_CLAUDE_MD):
        self.profile.write_text(profile_text, encoding="utf-8")
        self.claude_md.write_text(claude_md_text, encoding="utf-8")

    def cli(self, *argv):
        return run(["--repo", str(self.repo), "--today", "2026-11-03", *argv])

    def export(self, *argv):
        code, payload, err = self.cli(*argv)
        self.assertEqual(code, 0, err)
        return payload, json.loads(self.out.read_text(encoding="utf-8"))


class IdentityAndShape(ExportProfileTestCase):
    def test_identity_email_phone_and_links(self):
        summary, profile = self.export()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["out"], str(self.out))
        ident = profile["identity"]
        self.assertEqual(ident["name"], "Jane van Doe")
        self.assertEqual(ident["first_name"], "Jane")
        self.assertEqual(ident["last_name"], "van Doe")
        self.assertEqual(ident["email"], "jane.doe@example.org")
        self.assertEqual(ident["phone"], "+45 12 34 56 78")
        self.assertEqual(ident["location"], "Aarhus, Denmark")
        # Markdown link unwrapped to its URL; bare URL kept as-is.
        self.assertEqual(ident["linkedin"], "https://www.linkedin.com/in/janedoe")
        self.assertEqual(ident["github"], "https://github.com/janedoe")
        self.assertEqual(ident["status"], "Employed, open to offers")
        self.assertEqual(ident["constraints"], "Within 45 min of Aarhus")
        self.assertEqual(ident["cv_language"], "English")

    def test_top_level_shape_matches_the_plan(self):
        _, profile = self.export()
        self.assertEqual(
            list(profile),
            [
                "schema",
                "generated",
                "source_files",
                "identity",
                "languages",
                "education",
                "experience",
                "years_experience",
                "screening_answers",
                "unanswered",
                "warnings",
            ],
        )
        self.assertEqual(profile["schema"], 1)
        self.assertEqual(profile["generated"], "2026-11-03")
        self.assertEqual(
            profile["source_files"],
            [export_profile.PROFILE_REL.as_posix(), "CLAUDE.md"],
        )
        self.assertEqual(
            list(profile["identity"]),
            [
                "name",
                "first_name",
                "last_name",
                "email",
                "phone",
                "location",
                "linkedin",
                "github",
                "status",
                "constraints",
                "cv_language",
            ],
        )

    def test_languages_education_experience_tables(self):
        _, profile = self.export()
        self.assertEqual(
            profile["languages"],
            [
                {"language": "English", "level": "C2"},
                {"language": "Danish", "level": "B1/B2 (conversational)"},
            ],
        )
        self.assertEqual(
            profile["education"],
            [
                {"degree": "MSc Geophysics", "period": "2014-2016", "institution": "Aarhus University"},
                {"degree": "BSc Physics", "period": "2011-2014", "institution": "Aarhus University"},
            ],
        )
        self.assertEqual(
            profile["experience"],
            [
                {"title": "Senior Data Scientist", "company": "Globex", "start": "Mar 2021", "end": "Present"},
                {"title": "Data Scientist", "company": "Acme Corp", "start": "2019", "end": "2022"},
            ],
        )
        self.assertEqual(profile["warnings"], [])

    def test_html_comments_are_not_parsed(self):
        """The template's multi-line comments must never leak into values."""
        _, profile = self.export()
        text = json.dumps(profile)
        self.assertNotIn("<!--", text)
        self.assertNotIn("honest level", text)


class IdentityRobustness(ExportProfileTestCase):
    """Regressions: shapes /setup or a hand edit can plausibly leave in 01."""

    def test_colon_outside_bold_label_is_the_same_field(self):
        # `- **Name**: value` is the style 01 itself uses under Independent Projects;
        # it used to export name ": Jane van Doe" with first_name ":".
        self.write(FIXTURE_01.replace("- **Name:** Jane van Doe", "- **Name**: Søren Ærø Müller"))
        _, profile = self.export()
        ident = profile["identity"]
        self.assertEqual(ident["name"], "Søren Ærø Müller")
        self.assertEqual(ident["first_name"], "Søren")
        self.assertEqual(ident["last_name"], "Ærø Müller")
        self.assertEqual(profile["warnings"], [])

    def test_mailto_and_tel_links_export_the_bare_address(self):
        text = FIXTURE_01.replace(
            "- **Email:** jane.doe@example.org",
            "- **Email:** [jane.doe@example.org](mailto:jane.doe@example.org)",
        ).replace("- **Phone:** +45 12 34 56 78", "- **Phone:** <tel:+4512345678>")
        self.write(text)
        _, profile = self.export()
        self.assertEqual(profile["identity"]["email"], "jane.doe@example.org")
        self.assertEqual(profile["identity"]["phone"], "+4512345678")

    def test_dash_only_identity_value_is_blank(self):
        # A lone "-" means "none"; the fill step must not type it into a URL field.
        self.write(FIXTURE_01.replace("- **GitHub:** https://github.com/janedoe", "- **GitHub:** -"))
        _, profile = self.export()
        self.assertEqual(profile["identity"]["github"], "")

    def test_clean_value_helper(self):
        self.assertEqual(export_profile.clean_value("[x](mailto:a@b.c)"), "a@b.c")
        self.assertEqual(export_profile.clean_value("<https://a.b>"), "https://a.b")
        self.assertEqual(export_profile.clean_value(" – "), "")
        self.assertEqual(export_profile.clean_value("[YOUR_EMAIL]"), "[YOUR_EMAIL]")


class ExperienceDateRanges(ExportProfileTestCase):
    def test_unambiguous_year_range_without_spaces_and_en_dash_parse(self):
        text = FIXTURE_01.replace("(2019 - 2022)", "(2017-2018)").replace(
            "(Mar 2021 - Present)", "(2021-03–Present)"
        )
        self.write(text)
        _, profile = self.export()
        self.assertEqual(profile["warnings"], [])
        self.assertEqual(
            profile["experience"],
            [
                {"title": "Senior Data Scientist", "company": "Globex", "start": "2021-03", "end": "Present"},
                {"title": "Data Scientist", "company": "Acme Corp", "start": "2017", "end": "2018"},
            ],
        )
        # Jan 2017..Dec 2018 (24) + Mar 2021..Nov 2026 (69) = 93 months -> 7.
        self.assertEqual(profile["years_experience"]["total"], 7)

    def test_hyphen_without_spaces_between_non_years_still_warns(self):
        # "2021-03-2022-05" is ambiguous, so it is a warning, never a guess.
        self.write(FIXTURE_01.replace("(2019 - 2022)", "(2019-03-2022-05)"))
        _, profile = self.export()
        self.assertEqual(len(profile["experience"]), 1)
        self.assertTrue(any("unparsed heading" in w and "Acme Corp" in w for w in profile["warnings"]), profile["warnings"])
        self.assertEqual(profile["years_experience"]["total"], 5)


class ScreeningAnswers(ExportProfileTestCase):
    def test_keys_are_slugified_questions_including_a_user_added_row(self):
        _, profile = self.export()
        answers = profile["screening_answers"]
        self.assertEqual(
            list(answers),
            [
                "work_authorisation",
                "notice_period_earliest_start",
                "salary_expectation",
                "willing_to_relocate",
                "remote_hybrid_on_site_preference",
                "portfolio_url",
                "how_did_you_hear_about_us_default",
                "security_clearance_danish_forms",
            ],
        )
        self.assertEqual(answers["work_authorisation"], "EU citizen, no sponsorship needed")
        self.assertEqual(answers["salary_expectation"], "DKK 55,000 / month gross")
        self.assertEqual(answers["how_did_you_hear_about_us_default"], "Company careers page")
        # A row the user added exports without any code change.
        self.assertEqual(answers["security_clearance_danish_forms"], "none")

    def test_placeholder_row_is_unanswered_not_an_error(self):
        summary, profile = self.export()
        self.assertEqual(profile["screening_answers"]["portfolio_url"], "")
        self.assertEqual(profile["unanswered"], ["portfolio_url"])
        self.assertEqual(summary["unanswered"], ["portfolio_url"])

    def test_blank_row_is_unanswered(self):
        self.write(FIXTURE_01.replace("| Willing to relocate | within Denmark |", "| Willing to relocate |  |"))
        _, profile = self.export()
        self.assertEqual(profile["screening_answers"]["willing_to_relocate"], "")
        self.assertEqual(profile["unanswered"], ["willing_to_relocate", "portfolio_url"])

    def test_slugify(self):
        self.assertEqual(export_profile.slugify("Notice period / earliest start"), "notice_period_earliest_start")
        self.assertEqual(export_profile.slugify("Remote / hybrid / on-site preference"), "remote_hybrid_on_site_preference")
        self.assertEqual(export_profile.slugify("How did you hear about us (default)"), "how_did_you_hear_about_us_default")
        self.assertEqual(export_profile.slugify("  Portfolio URL  "), "portfolio_url")


class YearsOfExperience(ExportProfileTestCase):
    def test_union_of_overlapping_intervals_rounded_down(self):
        # 2019-2022 and Mar 2021-Present at 2026-11-03: union Jan 2019..Nov 2026 = 7y10m -> 7.
        _, profile = self.export()
        self.assertEqual(
            profile["years_experience"],
            {"total": 7, "method": "union of experience intervals, rounded down", "as_of": "2026-11-03"},
        )

    def test_overlap_is_not_double_counted(self):
        # Overlapping intervals alone would sum to 4 + 5 = 9 if naively added.
        text = FIXTURE_01.replace("(Mar 2021 - Present)", "(Jan 2021 - Dec 2025)")
        self.write(text)
        _, profile = self.export()
        self.assertEqual(profile["years_experience"]["total"], 7)  # Jan 2019..Dec 2025 = 84 months

    def test_present_follows_today(self):
        code, payload, err = run(["--repo", str(self.repo), "--today", "2024-02-01"])
        self.assertEqual(code, 0, err)
        profile = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(profile["years_experience"]["total"], 5)  # Jan 2019..Feb 2024 = 62 months
        self.assertEqual(profile["years_experience"]["as_of"], "2024-02-01")

    def test_unreadable_dates_warn_and_are_skipped_never_guessed(self):
        text = FIXTURE_01.replace("(2019 - 2022)", "(spring 2019 - some time later)")
        self.write(text)
        _, profile = self.export()
        self.assertEqual(profile["years_experience"]["total"], 5)  # Mar 2021..Nov 2026 only
        self.assertEqual(len(profile["warnings"]), 1)
        self.assertIn("could not read the dates", profile["warnings"][0])
        self.assertIn("Acme Corp", profile["warnings"][0])

    def test_unparsed_experience_heading_warns(self):
        text = FIXTURE_01.replace("### Data Scientist - Acme Corp (2019 - 2022)", "### Data Scientist at Acme Corp, 2019 to 2022")
        self.write(text)
        _, profile = self.export()
        self.assertEqual(len(profile["experience"]), 1)
        self.assertTrue(any("unparsed heading" in w for w in profile["warnings"]), profile["warnings"])


class Placeholders(ExportProfileTestCase):
    def test_pristine_template_exits_1_and_writes_nothing(self):
        pristine = (
            Path(__file__).resolve().parent.parent / export_profile.PROFILE_REL
        ).read_text(encoding="utf-8")
        self.write(pristine, "- **CV language:** [YOUR_CV_LANGUAGE]\n")
        code, payload, err = self.cli()
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("[PLACEHOLDER]", err)
        self.assertIn("identity.email", err)
        self.assertIn("identity.cv_language", err)
        self.assertFalse(self.out.exists())

    def test_allow_placeholders_exports_with_a_warning(self):
        self.write(FIXTURE_01.replace("- **Phone:** +45 12 34 56 78", "- **Phone:** [YOUR_PHONE]"))
        summary, profile = self.export("--allow-placeholders")
        self.assertEqual(profile["identity"]["phone"], "[YOUR_PHONE]")
        self.assertTrue(any("identity.phone" in w for w in profile["warnings"]), profile["warnings"])
        self.assertTrue(summary["ok"])

    def test_is_placeholder_shapes(self):
        for value in ("[YOUR_NAME]", "[YOUR_SALARY_EXPECTATION, with currency and basis]", '[e.g. "2 weeks"]', "[...]", "[URL or blank]"):
            self.assertTrue(export_profile.is_placeholder(value), value)
        for value in ("", "Aarhus, Denmark", "DKK 55,000 / month", "https://x.y/[a]", "[a](https://x.y)"):
            self.assertFalse(export_profile.is_placeholder(value), value)


class CheckMode(ExportProfileTestCase):
    def test_check_before_any_export_is_stale(self):
        code, report, err = self.cli("--check")
        self.assertEqual(code, 1, err)
        self.assertFalse(report["ok"])
        self.assertTrue(report["stale"])
        self.assertEqual(report["missing"], ["screening_answers.portfolio_url"])
        self.assertFalse(self.out.exists(), "--check must never write")

    def test_check_tracks_mtimes(self):
        self.export()
        code, report, _ = self.cli("--check")
        self.assertEqual(code, 0)
        self.assertEqual((report["ok"], report["stale"]), (True, False))

        # Profile edited after the export -> stale.
        newer = self.out.stat().st_mtime + 100
        os.utime(self.profile, (newer, newer))
        code, report, _ = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertEqual((report["ok"], report["stale"]), (False, True))

        # Re-export (or touch the export) -> fresh again.
        os.utime(self.out, (newer + 100, newer + 100))
        code, report, _ = self.cli("--check")
        self.assertEqual(code, 0)
        self.assertFalse(report["stale"])

        # CLAUDE.md is a source file too.
        os.utime(self.claude_md, (newer + 200, newer + 200))
        code, report, _ = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertTrue(report["stale"])

    def test_check_reports_placeholders_without_exiting_early(self):
        self.export()
        self.write(FIXTURE_01.replace("jane.doe@example.org", "[YOUR_EMAIL]"))
        older = (self.out.stat().st_mtime - 100,) * 2  # not stale, just wrong
        os.utime(self.profile, older)
        os.utime(self.claude_md, older)
        code, report, _ = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["stale"])
        self.assertEqual(report["placeholders"], ["identity.email"])
        self.assertIn("identity.email", report["missing"])


class PathsAndErrors(ExportProfileTestCase):
    def test_out_and_explicit_source_overrides(self):
        other = self.repo / "elsewhere" / "p.json"
        alt_profile = self.repo / "alt01.md"
        alt_profile.write_text(FIXTURE_01.replace("Jane van Doe", "Alt Name"), encoding="utf-8")
        code, summary, err = run(
            ["--profile", str(alt_profile), "--claude-md", str(self.claude_md), "--out", str(other), "--today", "2026-11-03"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["out"], str(other))
        self.assertEqual(json.loads(other.read_text(encoding="utf-8"))["identity"]["name"], "Alt Name")
        self.assertFalse(self.out.exists())

    def test_missing_profile_exits_1(self):
        self.profile.unlink()
        code, payload, err = self.cli()
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("profile not found", err)

    def test_missing_claude_md_warns_and_leaves_cv_language_blank(self):
        self.claude_md.unlink()
        _, profile = self.export()
        self.assertEqual(profile["identity"]["cv_language"], "")
        self.assertTrue(any("cv_language" in w for w in profile["warnings"]), profile["warnings"])

    def test_bad_today_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            main(["--repo", str(self.repo), "--today", "yesterday"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
