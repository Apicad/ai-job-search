import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.check_cv import (
    body_lead_phrase,
    check_fonts,
    check_keywords,
    check_orphans,
    collect_fonts,
    keyword_normalize,
    main,
    parse_contact,
    parse_cv_source,
    read_brace_group,
    strip_latex,
)
from tools.verify_pdf import VerificationError
from tests.test_verify_pdf import make_minimal_pdf

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CV = REPO_ROOT / "cv" / "main_example.tex"
EXAMPLE_COVER = REPO_ROOT / "cover_letters" / "cover_example.tex"

# A two-role CV source mirroring the stock template's \cventry shape.
CV_SOURCE = r"""
\documentclass[11pt,a4paper,sans]{moderncv}
\name{Ada}{Example}
\phone[mobile]{+45 12 34 56 78}
\email{ada@example.com}
\begin{document}
\section{Professional Experience}
\begin{itemize}
\item{\cventry{2020-Present}{Senior Data Scientist}{Acme A/S}{Copenhagen, Denmark}{}{\vspace{1pt}
\begin{itemize}
    \item Built CI/CD pipelines in Python for 12 models. % comment with } brace
    \item Mentored \textbf{3} juniors.
\end{itemize}}}
\item{\cventry{DATE_TWO}{Data Analyst}{Globex \& Co}{Aarhus, Denmark}{}{\vspace{1pt}
\begin{itemize}
    \item Automated reporting with Node.js and SQL.
\end{itemize}}}
\end{itemize}
\section{Education}
\begin{itemize}
\item{\cventry{2012-2016}{MSc in Physics}{Aarhus University}{Aarhus, Denmark}{}{\vspace{1pt}
Thesis: ``Quantum things.'' Focus on \{braces\} and lasers.
}}
\end{itemize}
\end{document}
"""

PAGE_ONE = (
    "Ada Example\n"
    "MOBILE-ALT +45 12 34 56 78 • Envelope ada@example.com\n"
    "Professional Experience\n"
    "2020-Present Senior Data Scientist, Acme A/S, Copenhagen, Denmark\n"
    "• Built CI / CD pipelines in Python for 12 models.\n"
    "• Mentored 3 juniors.\n"
    "DATE_TWO Data Analyst, Globex & Co, Aarhus, Denmark\n"
    "• Automated reporting with Node . js and SQL.\n"
)
PAGE_TWO = (
    "Education\n"
    "2012-2016 MSc in Physics, Aarhus University, Aarhus, Denmark\n"
    "Thesis: “Quantum things.” Focus on {braces} and lasers.\n"
)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, json.loads(out.getvalue()), err.getvalue()


class SourceParsingTests(unittest.TestCase):
    def test_parses_the_real_example_cv(self):
        entries, sections = parse_cv_source(EXAMPLE_CV.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 5)
        self.assertEqual(
            [e["title"] for e in entries],
            ["[Job Title]"] * 3 + ["[Degree] in [Field]"] * 2,
        )
        self.assertEqual([e["date_raw"] for e in entries][:1], ["[YYYY-Present]"])
        self.assertEqual([e["section"] for e in entries],
                         ["Professional Experience"] * 3 + ["Education"] * 2)
        self.assertEqual(sections[:3], ["Core Competencies", "Professional Experience", "Education"])
        self.assertEqual(sections[-1], "References")

    def test_parses_contact_from_the_real_example_cv(self):
        contact = parse_contact(EXAMPLE_CV.read_text(encoding="utf-8"))
        self.assertEqual(contact, {"email": "[your.email@example.com]", "phone": "[+XX XXXXXXXXXX]"})

    def test_brace_parser_handles_nesting_escapes_and_comments(self):
        entries, _ = parse_cv_source(CV_SOURCE.replace("DATE_TWO", "2016-2020"))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["body"], "Built CI/CD pipelines in Python for 12 models. Mentored 3 juniors.")
        self.assertEqual(entries[1]["employer"], "Globex & Co")
        self.assertIn("{braces}", entries[2]["body"])
        self.assertEqual(read_brace_group("{a\\}b}", 0), ("a\\}b", 6))
        self.assertIsNone(read_brace_group("{unbalanced", 0))

    def test_strip_latex_keeps_text_of_formatting_commands(self):
        self.assertEqual(strip_latex(r"\textbf{Lead} \href{https://x}{link} \vspace{2pt} done"),
                         "Lead link done")

    def test_body_lead_phrase_takes_four_letter_words(self):
        self.assertEqual(body_lead_phrase("[Achievement or responsibility 1 - be specific]"),
                         "[Achievement or responsibility 1 - be")


class CvCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pdf = self.root / "main.pdf"
        self.pdf.touch()
        self.tex = self.root / "main.tex"
        self.write_source("2016-2020")

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_source(self, date_two):
        self.tex.write_text(CV_SOURCE.replace("DATE_TWO", date_two), encoding="utf-8")

    def pages(self, date_two="2016-2020"):
        return [PAGE_ONE.replace("DATE_TWO", date_two), PAGE_TWO]

    def cv_args(self, *extra):
        return ["cv", "--pdf", str(self.pdf), "--source", str(self.tex), *extra]

    def test_clean_two_page_cv_passes(self):
        with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["needs_visual_review"])
        self.assertEqual(result["extractor"], "pypdf")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["problems"], [])
        checks = result["checks"]
        self.assertEqual(set(checks), {"page_count", "text_layer", "contact", "dates",
                                       "orphans", "fonts", "keywords"})
        self.assertTrue(checks["contact"]["email"]["found"])
        self.assertTrue(checks["contact"]["phone"]["found"])
        self.assertTrue(all(e["found_as_ascii_range"] for e in checks["dates"]["entries"]))
        self.assertEqual(checks["orphans"]["entries_parsed"], 3)
        self.assertEqual([x["page"] for x in checks["orphans"]["located"]], [0, 0, 1])
        self.assertIn("skipped", checks["keywords"])

    def test_title_on_page_one_and_body_on_page_two_is_an_orphan(self):
        split_pages = [
            PAGE_ONE.replace("DATE_TWO", "2016-2020") + "Education\n2012-2016 MSc in Physics, Aarhus University\n",
            "Thesis: “Quantum things.” Focus on {braces} and lasers.\n",
        ]
        with patch("tools.check_cv.extract_pages", return_value=(split_pages, "pdftotext")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_visual_review"])
        suspects = result["checks"]["orphans"]["suspects"]
        self.assertEqual(suspects, [{"title": "MSc in Physics", "title_page": 0, "body_page": 1}])
        self.assertTrue(any(p.startswith("orphans: title 'MSc in Physics'") for p in result["problems"]))

    def test_section_heading_isolated_at_page_bottom_is_flagged(self):
        pages = [PAGE_ONE.replace("DATE_TWO", "2016-2020") + "Education\n", PAGE_TWO]
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        suspects = result["checks"]["orphans"]["suspects"]
        self.assertEqual([s.get("section") for s in suspects], ["Education"])

    def test_unlocated_title_needs_visual_review_but_is_not_a_failure(self):
        pages = self.pages()
        pages[1] = pages[1].replace("MSc in Physics", "M.Sc. (Physics)")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        self.assertTrue(result["needs_visual_review"])
        self.assertEqual(result["checks"]["orphans"]["unlocated"][0]["title"], "MSc in Physics")

    def test_wrong_page_count_fails(self):
        with patch("tools.check_cv.extract_pages", return_value=(self.pages() + ["References"], "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        self.assertEqual(result["checks"]["page_count"], {"ok": False, "expected": 2, "actual": 3})
        self.assertIn("page_count: expected 2 page(s), found 3", result["problems"])

    def test_expect_pages_zero_disables_the_page_check(self):
        with patch("tools.check_cv.extract_pages", return_value=(self.pages() + ["x"], "pypdf")):
            code, result, _ = run_main(self.cv_args("--expect-pages", "0"))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["page_count"]["expected"], None)

    def test_en_dash_date_fails_and_bare_year_is_reported(self):
        self.tex.write_text(
            CV_SOURCE.replace("DATE_TWO", "2016--2020").replace("{2012-2016}", "{2016}"),
            encoding="utf-8",
        )
        pages = self.pages("2016–2020")
        pages[1] = pages[1].replace("2012-2016", "2016")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        dates = {e["date"]: e for e in result["checks"]["dates"]["entries"]}
        self.assertFalse(result["checks"]["dates"]["ok"])
        self.assertTrue(dates["2016--2020"]["double_hyphen"])
        self.assertFalse(dates["2016--2020"]["found_as_ascii_range"])
        self.assertTrue(dates["2016--2020"]["found_in_text"])
        self.assertIn("en-dash", dates["2016--2020"]["problem"])
        self.assertFalse(dates["2016"]["ascii_range"])
        self.assertTrue(dates["2020-Present"]["found_as_ascii_range"])
        self.assertTrue(any("2016--2020" in p and "en-dash" in p for p in result["problems"]))

    def test_missing_phone_fails_contact_check(self):
        pages = self.pages()
        pages[0] = pages[0].replace("+45 12 34 56 78", "")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        contact = result["checks"]["contact"]
        self.assertTrue(contact["email"]["found"])
        self.assertEqual(contact["phone"], {"value": "+45 12 34 56 78", "found": False, "matched": None})
        self.assertIn("contact: phone '+45 12 34 56 78' not found as literal text", result["problems"])

    def test_phone_matches_digits_only_when_extractor_drops_spaces(self):
        pages = self.pages()
        pages[0] = pages[0].replace("+45 12 34 56 78", "+45 1234 5678")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["contact"]["phone"]["matched"], "nospace")
        pages[0] = pages[0].replace("+45 1234 5678", "(+45) 12-34-56-78")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["contact"]["phone"]["matched"], "digits")

    def test_explicit_email_and_phone_flags_override_source(self):
        with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
            code, result, _ = run_main(self.cv_args("--email", "nobody@example.org", "--phone", "+1 555"))
        self.assertEqual(code, 1)
        self.assertFalse(result["checks"]["contact"]["email"]["found"])
        self.assertFalse(result["checks"]["contact"]["phone"]["found"])

    def test_keyword_statuses_with_punctuation_normalization(self):
        keywords = self.root / "keywords.json"
        keywords.write_text(json.dumps([
            {"term": "CI/CD", "priority": "required", "synonyms": ["continuous integration"]},
            {"term": "Node.js", "priority": "nice", "synonyms": []},
            {"term": "Machine Learning", "priority": "required", "synonyms": ["models"]},
            {"term": "Kubernetes", "priority": "required", "synonyms": ["k8s"]},
            {"term": "R", "priority": "nice", "synonyms": []},
        ]), encoding="utf-8")
        with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
            code, result, _ = run_main(self.cv_args("--keywords", str(keywords)))
        self.assertEqual(code, 0, result)
        kw = result["checks"]["keywords"]
        self.assertTrue(kw["ok"])
        self.assertEqual(kw["covered"], ["CI/CD", "Node.js"])
        self.assertEqual([s["term"] for s in kw["synonym_only"]], ["Machine Learning"])
        self.assertEqual([m["term"] for m in kw["missing"]], ["Kubernetes", "R"])
        self.assertEqual(kw["required_missing"], ["Kubernetes"])

    def test_keyword_normalization_rules(self):
        self.assertEqual(keyword_normalize("CI / CD"), "ci/cd")
        self.assertEqual(keyword_normalize("Node . js"), "node.js")
        self.assertEqual(keyword_normalize("  Machine   Learning "), "machine learning")
        kw = check_keywords(["Python , SQL and C++ code"], [
            {"term": "C++", "priority": "required"},
            {"term": "python", "priority": "nice"},
        ])
        self.assertEqual(kw["covered"], ["C++", "python"])

    def test_malformed_keyword_entries_are_a_data_error_not_a_traceback(self):
        keywords = self.root / "keywords.json"
        for bad in ([{"term": 5}], [{"term": "   "}], [{"term": "x", "synonyms": [1]}], [42]):
            keywords.write_text(json.dumps(bad), encoding="utf-8")
            with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
                code, result, err = run_main(self.cv_args("--keywords", str(keywords)))
            self.assertEqual(code, 1, bad)
            self.assertFalse(result["ok"])
            self.assertIn("keyword", result["error"].lower())
            self.assertIn("Error:", err)

    def test_string_synonyms_and_bare_string_terms_are_accepted(self):
        keywords = self.root / "keywords.json"
        keywords.write_text(json.dumps([
            {"term": "Kubernetes", "priority": "required", "synonyms": "SQL"},
            " Python ",
            {"term": "Go", "priority": None, "synonyms": None},
        ]), encoding="utf-8")
        with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
            code, result, _ = run_main(self.cv_args("--keywords", str(keywords)))
        self.assertEqual(code, 0, result)
        kw = result["checks"]["keywords"]
        self.assertEqual(kw["covered"], ["Python"])
        self.assertEqual(kw["synonym_only"], [{"term": "Kubernetes", "matched": ["SQL"], "priority": "required"}])
        self.assertEqual(kw["missing"], [{"term": "Go", "priority": ""}])

    def test_negative_expect_pages_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(self.cv_args("--expect-pages", "-1"))
        self.assertEqual(ctx.exception.code, 2)

    def test_contact_macros_with_latex_escapes_match_their_rendering(self):
        self.tex.write_text(
            CV_SOURCE.replace("DATE_TWO", "2016-2020")
            .replace(r"\email{ada@example.com}", r"\email{ada\_ex@example.com}")
            .replace(r"\phone[mobile]{+45 12 34 56 78}", r"\phone[mobile]{+45~12~34~56~78}"),
            encoding="utf-8",
        )
        pages = self.pages()
        pages[0] = pages[0].replace("ada@example.com", "ada_ex@example.com")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        contact = result["checks"]["contact"]
        self.assertEqual(contact["email"], {"value": "ada_ex@example.com", "found": True, "matched": "exact"})
        self.assertEqual(contact["phone"], {"value": "+45 12 34 56 78", "found": True, "matched": "exact"})

    def test_literal_unicode_dash_in_source_is_named_not_called_a_bare_year(self):
        self.write_source("2016\u20132020")
        with patch("tools.check_cv.extract_pages", return_value=(self.pages("2016\u20132020"), "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        entry = [e for e in result["checks"]["dates"]["entries"] if e["title"] == "Data Analyst"][0]
        self.assertFalse(entry["ascii_range"])
        self.assertIn("non-ASCII dash (U+2013)", entry["problem"])
        self.assertNotIn("bare year", entry["problem"])

    def test_title_with_spaces_dropped_by_extractor_is_still_located(self):
        pages = self.pages()
        pages[1] = pages[1].replace("MSc in Physics", "MScinPhysics")
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 0, result)
        self.assertFalse(result["needs_visual_review"])
        self.assertEqual(result["checks"]["orphans"]["unlocated"], [])
        self.assertIn({"title": "MSc in Physics", "page": 1}, result["checks"]["orphans"]["located"])

    def test_garbled_text_layer_fails(self):
        pages = self.pages()
        pages[0] = pages[0] + "(cid:12)(cid:13) �\n"
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(self.cv_args())
        self.assertEqual(code, 1)
        tl = result["checks"]["text_layer"]
        self.assertEqual((tl["ok"], tl["cid_markers"], tl["replacement_chars"]), (False, 2, 1))

    def test_missing_source_skips_source_checks_and_needs_visual_review(self):
        with patch("tools.check_cv.extract_pages", return_value=(self.pages(), "pypdf")):
            code, result, _ = run_main(["cv", "--pdf", str(self.pdf), "--source",
                                        str(self.root / "missing.tex"), "--email", "ada@example.com",
                                        "--phone", "+45 12 34 56 78"])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["needs_visual_review"])
        self.assertIn("skipped", result["checks"]["dates"])
        self.assertIn("skipped", result["checks"]["orphans"])

    def test_real_example_source_through_the_cli(self):
        pages = [
            "[First] [Last]\nMOBILE-ALT [+XX XXXXXXXXXX] • Envelope [your.email@example.com]\n"
            "Core Competencies\nProfessional Experience\n"
            "[YYYY-Present] [Job Title], [Company], [City, Country]\n"
            "• [Achievement or responsibility 1 - be specific, use numbers where possible]\n"
            "[YYYY-YYYY] [Job Title], [Company], [City, Country]\n• [Achievement or responsibility 1]\n",
            "Education\n[YYYY-YYYY] [Degree] in [Field], [Institution], [City, Country]\n"
            "Thesis: “[Thesis Title].” [Brief description of research focus.]\n"
            "[YYYY-YYYY] [Degree] in [Field], [Institution]\n[Brief description or key topics.]\n"
            "References\nAvailable upon request.\n",
        ]
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pypdf")):
            code, result, _ = run_main(["cv", "--pdf", str(self.pdf), "--source", str(EXAMPLE_CV)])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["checks"]["contact"]["email"]["found"])
        self.assertEqual(result["checks"]["orphans"]["entries_parsed"], 5)
        self.assertEqual(len(result["checks"]["dates"]["entries"]), 5)
        self.assertTrue(result["checks"]["dates"]["ok"])

    def test_missing_pdf_is_a_data_error(self):
        code, result, err = run_main(["cv", "--pdf", str(self.root / "nope.pdf"), "--source", str(self.tex)])
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("PDF does not exist", result["error"])
        self.assertIn("Error:", err)

    def test_no_extractor_reports_install_hint(self):
        failure = VerificationError(
            "required command 'pdftotext' was not found. Install pypdf (`pip install pypdf`) or poppler-utils"
        )
        with patch("tools.check_cv.extract_pages", side_effect=failure):
            code, result, err = run_main(self.cv_args())
        self.assertEqual(code, 1)
        self.assertIn("no extractor", result["error"])
        self.assertIn("pip install pypdf", err)

    def test_usage_error_exits_2(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["cv", "--pdf", str(self.pdf)])
        self.assertEqual(ctx.exception.code, 2)


class _Indirect:
    """Stands in for a pypdf IndirectObject: resolved through get_object()."""

    def __init__(self, obj, idnum):
        self._obj = obj
        self.idnum = idnum

    def get_object(self):
        return self._obj


class _FakeReader:
    def __init__(self, pages):
        self.pages = pages


def fake_reader(*basefonts, xobject_font=None):
    fonts = {f"/F{i}": _Indirect({"/BaseFont": name}, 10 + i) for i, name in enumerate(basefonts)}
    resources = {"/Font": _Indirect(fonts, 5)}
    if xobject_font:
        resources["/XObject"] = {
            "/Fm1": _Indirect({"/Resources": {"/Font": {"/F9": {"/BaseFont": xobject_font}}}}, 99)
        }
    return _FakeReader([
        _Indirect({"/Resources": _Indirect(resources, 4)}, 3),
        {"/Resources": {"/Font": {}}},
        {},  # page with no resources at all
    ])


class CoverCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pdf = self.root / "cover.pdf"
        self.pdf.touch()
        self.page = (
            "[YOUR NAME]\nyour.email@example.com | [+XX XXXXXXXXXX] | LinkedIn\n"
            "Dear [Hiring Manager / Team],\n• [Achievement 1]: result\n"
            "I look forward to hearing from you.\nKind regards,\n[YOUR NAME]\n"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_collect_fonts_follows_indirect_objects_and_xobjects(self):
        reader = fake_reader("/ABCDEF+Raleway-Medium", "/GHIJKL+Raleway-Bold", xobject_font="/Helvetica")
        self.assertEqual(collect_fonts(reader),
                         ["ABCDEF+Raleway-Medium", "GHIJKL+Raleway-Bold", "Helvetica"])

    def test_allowed_font_check_with_fake_reader(self):
        with patch("tools.check_cv._open_reader", return_value=fake_reader("/ABCDEF+Raleway-Medium")):
            self.assertTrue(check_fonts(self.pdf, ["Raleway"])["ok"])
        bad = fake_reader("/ABCDEF+Raleway-Medium", "/LMSans10-Regular")
        with patch("tools.check_cv._open_reader", return_value=bad):
            result = check_fonts(self.pdf, ["raleway"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["disallowed"], ["LMSans10-Regular"])

    def test_fonts_skipped_without_pypdf(self):
        with patch("tools.check_cv._open_reader", return_value=None):
            result = check_fonts(self.pdf, ["Raleway"])
        self.assertEqual(result, {"ok": True, "skipped": "pypdf not installed", "fonts": []})

    def test_cover_cli_passes_with_real_example_source(self):
        reader = fake_reader("/ABCDEF+Raleway-Medium", "/ABCDEF+Raleway-Bold")
        with patch("tools.check_cv.extract_pages", return_value=([self.page], "pypdf")), \
                patch("tools.check_cv._open_reader", return_value=reader):
            code, result, _ = run_main([
                "cover", "--pdf", str(self.pdf), "--source", str(EXAMPLE_COVER),
                "--contains", "Kind regards", "--contains", "your.email@example.com",
                "--allowed-font", "Raleway",
            ])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["needs_visual_review"])
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["checks"]["page_count"], {"ok": True, "expected": 1, "actual": 1})
        self.assertEqual(result["checks"]["contains"]["signature"]["terms"], ["Kind regards,", "[YOUR NAME]"])
        self.assertEqual(result["checks"]["fonts"]["fonts"], ["ABCDEF+Raleway-Bold", "ABCDEF+Raleway-Medium"])
        self.assertIn("skipped", result["checks"]["dates"])

    def test_cover_cli_fails_on_two_pages_wrong_font_and_missing_text(self):
        reader = fake_reader("/ABCDEF+Raleway-Medium", "/CMR10")
        pages = [self.page.replace("Kind regards,\n[YOUR NAME]\n", ""), "Kind regards,\n[YOUR NAME]\n"]
        with patch("tools.check_cv.extract_pages", return_value=(pages, "pdftotext")), \
                patch("tools.check_cv._open_reader", return_value=reader):
            code, result, _ = run_main([
                "cover", "--pdf", str(self.pdf), "--source", str(EXAMPLE_COVER),
                "--contains", "Sincerely", "--allowed-font", "Raleway",
            ])
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["fonts"]["disallowed"], ["CMR10"])
        self.assertEqual(result["checks"]["contains"]["missing"], ["Sincerely"])
        self.assertIn("page_count: expected 1 page(s), found 2", result["problems"])
        self.assertTrue(any(p.startswith("fonts:") for p in result["problems"]))
        self.assertTrue(any(p.startswith("contains: required text 'Sincerely'") for p in result["problems"]))

    def test_cover_without_pypdf_needs_visual_review_but_passes(self):
        with patch("tools.check_cv.extract_pages", return_value=([self.page], "pdftotext")), \
                patch("tools.check_cv._open_reader", return_value=None):
            code, result, _ = run_main(["cover", "--pdf", str(self.pdf), "--allowed-font", "Raleway"])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["needs_visual_review"])
        self.assertEqual(result["checks"]["fonts"]["skipped"], "pypdf not installed")

    def test_signature_pushed_off_text_layer_fails(self):
        page = self.page.replace("Kind regards,\n[YOUR NAME]\n", "")
        with patch("tools.check_cv.extract_pages", return_value=([page], "pypdf")), \
                patch("tools.check_cv._open_reader", return_value=fake_reader("/X+Raleway-Medium")):
            code, result, _ = run_main(["cover", "--pdf", str(self.pdf), "--source", str(EXAMPLE_COVER)])
        self.assertEqual(code, 1)
        # The header still prints the name, so only the closing line goes missing.
        self.assertEqual(result["checks"]["contains"]["signature"]["missing"], ["Kind regards,"])
        self.assertIn("contains: closing/signature text 'Kind regards,' not found in the text layer",
                      result["problems"])


class RealPdfTests(unittest.TestCase):
    """No mocks: a hand-built PDF goes through whichever extractor is installed."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pdf = self.root / "main.pdf"
        make_minimal_pdf(self.pdf, [
            "Ada\n+45 12 34 56 78 ada@example.com\nExperience\n"
            "2020-Present Senior Data Scientist, Acme\nBuilt CI/CD pipelines for models",
            "Education\n2012-2016 MSc in Physics, Aarhus University\nThesis on lasers and more stuff",
        ])
        self.tex = self.root / "main.tex"
        self.tex.write_text(
            "\\email{ada@example.com}\\phone[mobile]{+45 12 34 56 78}\n"
            "\\section{Experience}\n"
            "\\item{\\cventry{2020-Present}{Senior Data Scientist}{Acme}{Copenhagen}{}{Built CI/CD pipelines for models}}\n"
            "\\section{Education}\n"
            "\\item{\\cventry{2012-2016}{MSc in Physics}{Aarhus University}{Aarhus}{}{Thesis on lasers and more stuff}}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_or_skip(self, argv):
        code, result, err = run_main(argv)
        if code == 1 and "no extractor" in result.get("error", ""):
            self.skipTest(f"no PDF extractor available: {err.strip()}")
        return code, result

    def test_cv_subcommand_on_a_real_pdf(self):
        code, result = self.run_or_skip(["cv", "--pdf", str(self.pdf), "--source", str(self.tex)])
        self.assertEqual(code, 0, result)
        self.assertIn(result["extractor"], ("pypdf", "pdftotext"))
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["checks"]["orphans"]["entries_parsed"], 2)
        self.assertEqual([x["page"] for x in result["checks"]["orphans"]["located"]], [0, 1])

    def test_cover_subcommand_on_a_real_pdf(self):
        code, result = self.run_or_skip([
            "cover", "--pdf", str(self.pdf), "--expect-pages", "2",
            "--allowed-font", "Helvetica", "--contains", "Thesis on lasers",
        ])
        self.assertEqual(code, 0, result)
        fonts = result["checks"]["fonts"]
        if "skipped" not in fonts:
            self.assertEqual(fonts["fonts"], ["Helvetica"])
        code, result = self.run_or_skip(["cover", "--pdf", str(self.pdf), "--allowed-font", "Raleway"])
        self.assertEqual(code, 1)
        self.assertIn("page_count: expected 1 page(s), found 2", result["problems"])


class OrphanHelperTests(unittest.TestCase):
    def test_duplicate_title_elsewhere_does_not_hide_an_orphan(self):
        entries = [{"title": "Data Scientist", "body": "Built things fast daily now", "section": "X"}]
        pages = ["Profile: Data Scientist with ...\nData Scientist, Acme\n", "• Built things fast daily now\n"]
        result = check_orphans(pages, entries, [])
        self.assertEqual(result["suspects"], [{"title": "Data Scientist", "title_page": 0, "body_page": 1}])
        pages = ["Profile: Data Scientist with ...\n", "Data Scientist, Acme\n• Built things fast daily now\n"]
        self.assertEqual(check_orphans(pages, entries, [])["suspects"], [])


if __name__ == "__main__":
    unittest.main()
