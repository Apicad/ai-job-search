"""Tests for tools/base_cv.py - base CVs per role family for /quick-apply.

Every test drives the real code path through `main([...])` against a
temporary cv/ directory; nothing touches the repo's own cv/ folder and
nothing hits the network. The slug cases are the ones documents/README.md's
Subfolder naming rule is pinned to in
tests/test_apply_records_application.py, imported from there so the two
cannot drift apart.
"""
import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import base_cv
from tools.base_cv import main

try:
    # Read the table without binding the TestCase class in this module's
    # namespace, or unittest would collect and run its tests here a second time.
    DERIVATIONS = importlib.import_module(
        "tests.test_apply_records_application"
    ).ArchiveNameIsOnePathComponent.DERIVATIONS
except (ImportError, AttributeError):  # pragma: no cover - the table is shared, not copied
    DERIVATIONS = None

BASE_TEX = (
    "\\documentclass{moderncv}\n"
    "\\begin{document}\n"
    "A data scientist who ships models, which is what I would bring to [EMPLOYER].\n"
    "\\section{Core Competencies}\n"
    "\\end{document}\n"
)


def run(argv):
    """Run main() capturing stdout/stderr; return (exit_code, parsed_json_or_None, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()


class BaseCvTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cv_dir = Path(self._tmp.name) / "cv"
        self.cv_dir.mkdir()

    def write_base(self, family="data_science", text=BASE_TEX, ext=".tex"):
        path = self.cv_dir / f"base_{family}{ext}"
        path.write_text(text, encoding="utf-8")
        return path

    def cli(self, *argv):
        return run(["--cv-dir", str(self.cv_dir), *argv])


class SlugCommand(BaseCvTestCase):
    def test_shared_derivation_table(self):
        self.assertIsNotNone(
            DERIVATIONS,
            "the slug cases live in test_apply_records_application.py; import failed",
        )
        for company, role, expected in DERIVATIONS:
            with self.subTest(company=company, role=role):
                code, payload, err = self.cli("slug", "--company", company, "--role", role)
                if expected is None:
                    self.assertEqual(code, 1)
                    self.assertIsNone(payload)
                    self.assertIn("at least one letter or digit", err)
                else:
                    self.assertEqual(code, 0, err)
                    self.assertEqual(payload, {"slug": expected})
                    self.assertNotIn("/", payload["slug"])
                    self.assertNotIn("..", payload["slug"])

    def test_slugify_matches_the_documented_rule(self):
        self.assertEqual(base_cv.slugify("Bang & Olufsen", "Senior ML Engineer"),
                         "bang_olufsen_senior_ml_engineer")
        self.assertEqual(base_cv.slugify("  Acme  ", "Dev"), "acme_dev")
        self.assertEqual(base_cv.slugify("///", "..."), "")


class ListCommand(BaseCvTestCase):
    def test_empty_directory_lists_nothing(self):
        code, payload, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(payload, [])

    def test_missing_directory_lists_nothing(self):
        code, payload, _ = run(["--cv-dir", str(self.cv_dir / "absent"), "list"])
        self.assertEqual(code, 0)
        self.assertEqual(payload, [])

    def test_lists_families_sorted_with_and_without_a_compiled_pdf(self):
        self.write_base("ml_engineering")
        self.write_base("data_science")
        (self.cv_dir / "base_data_science.pdf").write_bytes(b"%PDF-1.4\n")
        # not bases: the master CV, a per-job CV, a stray build file
        (self.cv_dir / "main_example.tex").write_text("x", encoding="utf-8")
        (self.cv_dir / "main_acme_dev.tex").write_text("x", encoding="utf-8")
        (self.cv_dir / "base_ml_engineering.aux").write_text("x", encoding="utf-8")
        code, payload, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertEqual([e["family"] for e in payload], ["data_science", "ml_engineering"])
        self.assertEqual([e["compiled_pdf"] for e in payload], [True, False])
        self.assertEqual(payload[0]["path"], str(self.cv_dir / "base_data_science.tex"))
        self.assertEqual(set(payload[0]), {"family", "path", "compiled_pdf"})


class StampCommand(BaseCvTestCase):
    def stamp(self, *extra, family="data_science", company="Acme", role="ML Engineer",
              employer="Acme Corp"):
        return self.cli(
            "stamp", "--family", family, "--company", company, "--role", role,
            "--employer", employer, *extra,
        )

    def test_stamps_a_per_job_copy_replacing_the_token(self):
        self.write_base()
        code, payload, err = self.stamp()
        self.assertEqual(code, 0, err)
        target = self.cv_dir / "main_acme_ml_engineer.tex"
        self.assertEqual(payload, {
            "cv_file": str(target),
            "family": "data_science",
            "slug": "acme_ml_engineer",
            "replaced": 1,
        })
        stamped = target.read_text(encoding="utf-8")
        self.assertNotIn("[EMPLOYER]", stamped)
        self.assertIn("I would bring to Acme Corp.", stamped)
        self.assertEqual(stamped, BASE_TEX.replace("[EMPLOYER]", "Acme Corp"))
        # the base is untouched
        self.assertEqual((self.cv_dir / "base_data_science.tex").read_text(encoding="utf-8"), BASE_TEX)

    def test_replaces_every_token_and_reports_the_count(self):
        self.write_base(text=BASE_TEX + "% header for [EMPLOYER]\n")
        code, payload, _ = self.stamp()
        self.assertEqual(code, 0)
        self.assertEqual(payload["replaced"], 2)
        self.assertNotIn("[EMPLOYER]", (self.cv_dir / "main_acme_ml_engineer.tex").read_text(encoding="utf-8"))

    def test_refuses_to_overwrite_without_force(self):
        self.write_base()
        target = self.cv_dir / "main_acme_ml_engineer.tex"
        target.write_text("edited by hand", encoding="utf-8")
        code, payload, err = self.stamp()
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("--force", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "edited by hand")

    def test_force_overwrites(self):
        self.write_base()
        target = self.cv_dir / "main_acme_ml_engineer.tex"
        target.write_text("edited by hand", encoding="utf-8")
        code, payload, err = self.stamp("--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["replaced"], 1)
        self.assertIn("Acme Corp", target.read_text(encoding="utf-8"))

    def test_missing_token_is_an_error_that_names_the_rule(self):
        self.write_base(text=BASE_TEX.replace("[EMPLOYER]", "a great employer"))
        code, payload, err = self.stamp()
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("[EMPLOYER]", err)
        self.assertIn("profile statement", err)
        self.assertFalse((self.cv_dir / "main_acme_ml_engineer.tex").exists())

    def test_missing_family_is_an_error_listing_known_families(self):
        self.write_base("data_science")
        code, payload, err = self.stamp(family="quant")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("quant", err)
        self.assertIn("data_science", err)
        self.assertIn("/quick-apply init", err)
        self.assertEqual(sorted(p.name for p in self.cv_dir.iterdir()), ["base_data_science.tex"])

    def test_family_is_normalised_like_a_role_type(self):
        self.write_base("data_science_ml")
        code, payload, err = self.stamp(family="Data Science / ML")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["family"], "data_science_ml")

    def test_family_keeps_unicode_letters_like_list_and_slug_do(self):
        # `list` reads the family back from the filename and `slugify` keeps
        # Danish letters, so `--family` must not strip them to ASCII (a
        # base_økonomi.tex that `list` reports must be stampable by that name).
        self.write_base("økonomi")
        code, payload, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertEqual([e["family"] for e in payload], ["økonomi"])
        code, payload, err = self.stamp(family="Økonomi")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["family"], "økonomi")
        self.assertEqual(base_cv.family_slug("Data Science / ML"), "data_science_ml")
        self.assertEqual(base_cv.family_slug("Ørsted & Co__"), "ørsted_co")
        self.assertEqual(base_cv.family_slug("../.."), "")

    def test_non_utf8_base_is_a_data_error_not_a_traceback(self):
        (self.cv_dir / "base_latin.tex").write_bytes(b"bring to [EMPLOYER] caf\xe9\n")
        code, payload, err = self.stamp(family="latin")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("UTF-8", err)
        self.assertIn("base_latin.tex", err)
        self.assertFalse((self.cv_dir / "main_acme_ml_engineer.tex").exists())

    def test_family_with_no_alphanumerics_is_rejected_before_touching_disk(self):
        code, payload, err = self.stamp(family="../..")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("--family", err)

    def test_empty_slug_is_an_error(self):
        self.write_base()
        code, payload, err = self.stamp(company="../..", role="///")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("Subfolder naming", err)
        self.assertEqual(sorted(p.name for p in self.cv_dir.iterdir()), ["base_data_science.tex"])

    def test_custom_extension(self):
        self.write_base(ext=".txt", text="Dear [EMPLOYER],\n")
        code, payload, err = self.stamp("--ext", ".txt")
        self.assertEqual(code, 0, err)
        target = self.cv_dir / "main_acme_ml_engineer.txt"
        self.assertEqual(payload["cv_file"], str(target))
        self.assertEqual(target.read_text(encoding="utf-8"), "Dear Acme Corp,\n")
        self.assertFalse((self.cv_dir / "main_acme_ml_engineer.tex").exists())
        # the extension may be given without its dot
        code, payload, err = self.stamp("--ext", "txt", "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["cv_file"], str(target))

    def test_custom_extension_looks_for_the_base_with_that_extension(self):
        self.write_base()  # only base_data_science.tex exists
        code, payload, err = self.stamp("--ext", ".txt")
        self.assertEqual(code, 1)
        self.assertIn("base_data_science.txt", err)

    def test_employer_latex_specials_are_escaped_unless_raw(self):
        self.write_base()
        code, _, err = self.stamp(employer="Bang & Olufsen")
        self.assertEqual(code, 0, err)
        self.assertIn("Bang \\& Olufsen.", (self.cv_dir / "main_acme_ml_engineer.tex").read_text(encoding="utf-8"))
        code, _, err = self.stamp("--force", employer="Bang \\& Olufsen")
        self.assertEqual(code, 0, err)
        self.assertIn("Bang \\& Olufsen.", (self.cv_dir / "main_acme_ml_engineer.tex").read_text(encoding="utf-8"))
        code, _, err = self.stamp("--force", "--raw", employer="Bang & Olufsen")
        self.assertEqual(code, 0, err)
        self.assertIn("Bang & Olufsen.", (self.cv_dir / "main_acme_ml_engineer.tex").read_text(encoding="utf-8"))

    def test_empty_employer_is_rejected(self):
        self.write_base()
        code, payload, err = self.stamp(employer="   ")
        self.assertEqual(code, 1)
        self.assertIn("--employer", err)
        self.assertFalse((self.cv_dir / "main_acme_ml_engineer.tex").exists())


class SpecPins(unittest.TestCase):
    """05-cv-templates.md documents the base-CV contract the tool enforces."""

    SPEC = Path(__file__).resolve().parent.parent / ".claude" / "skills" / \
        "job-application-assistant" / "05-cv-templates.md"

    def test_05_documents_the_base_cv_contract(self):
        text = self.SPEC.read_text(encoding="utf-8")
        body = text.partition("### Base CVs for the quick tier")[2].partition("\n### ")[0]
        self.assertTrue(body, "05-cv-templates.md lacks the 'Base CVs for the quick tier' subsection")
        for needle in (
            "cv/base_<family>.tex",
            "[EMPLOYER]",
            "exactly 2 pages",
            "never sent as-is and never edited per job",
            "/quick-apply init",
            "tools/base_cv.py stamp",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        # the subsection follows the profile statement templates, before Core Competencies
        self.assertLess(text.index("### Profile Statement / Elevator Pitch"),
                        text.index("### Base CVs for the quick tier"))
        self.assertLess(text.index("### Base CVs for the quick tier"),
                        text.index("### Core Competencies / Skills Section"))


if __name__ == "__main__":
    unittest.main()
