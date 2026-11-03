"""Guards for /apply Step 5 (compile & inspect) after it became script-first.

The step used to Read both PDFs as page images on every iteration of an
open-ended "iterate until clean" loop, then read a dumped text file and build
the keyword table by hand. `tools/check_cv.py` now does the page count,
orphan, contact, date, font and keyword checks mechanically, and the visual
Read happens exactly once at the end. These pins keep the loop from growing
back and keep the mandatory visual pass from being dropped entirely.
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPLY = REPO / ".claude" / "commands" / "apply.md"
CLAUDE_MD = REPO / "CLAUDE.md"
CV_TEMPLATES = (
    REPO / ".claude" / "skills" / "job-application-assistant" / "05-cv-templates.md"
)


def _subsections(text: str) -> dict[str, str]:
    parts = text.split("\n### ")
    result = {}
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        result[heading.strip()] = body
    return result


class ApplyStep5Spec(unittest.TestCase):
    def setUp(self):
        self.text = APPLY.read_text(encoding="utf-8")
        self.sub = _subsections(self.text)

    def _sub(self, prefix: str) -> str:
        for heading, body in self.sub.items():
            if heading.startswith(prefix):
                return body
        self.fail(f"apply.md has no subsection starting with {prefix!r}")

    def test_step5_is_still_mandatory(self):
        self.assertIn("## Step 5: DRAFTER - Compile & Inspect PDFs (MANDATORY)", self.text)
        self.assertIn("Step 5 (compile and inspect PDFs) is mandatory", self.text)

    def test_5b_runs_the_checker_on_both_documents_before_any_read(self):
        body = self._sub("5b.")
        self.assertIn("tools/check_cv.py cv --pdf", body)
        self.assertIn("tools/check_cv.py cover --pdf", body)
        self.assertIn("--keywords cv/main_<company>_<role>.keywords.json", body)
        self.assertIn("Do not Read the PDFs yet", body)

    def test_5b_keeps_the_utf8_fallback_rule(self):
        body = self._sub("5b.")
        self.assertIn("pdftotext -layout", body)
        self.assertIn("-enc UTF-8", body)

    def test_5c_reads_the_pdfs_exactly_once(self):
        body = self._sub("5c.")
        self.assertRegex(body, r"Read both PDFs via the Read tool \*\*once\*\*")
        self.assertIn("never as a routine loop", body)

    def test_5c_falls_back_to_the_full_visual_checklist_on_request(self):
        body = self._sub("5c.")
        self.assertIn("needs_visual_review: true", body)
        self.assertIn("reading order matches the visual order", body.lower())

    def test_5d_keeps_the_four_keyword_statuses_and_the_no_stuffing_rule(self):
        body = self._sub("5d.")
        for status in ("covered", "synonym-only", "missing (have it)", "missing (gap)"):
            self.assertIn(status, body)
        self.assertIn("Never stuff keywords", body)

    def test_step2_writes_the_keyword_file_and_5e_deletes_it(self):
        self.assertIn("cv/main_<company>_<role>.keywords.json", self.text)
        self.assertIn('"synonyms"', self.text)
        self.assertIn(".keywords.json` file from Step 2", self._sub("5e."))


class ChecklistAndTemplateSpec(unittest.TestCase):
    def test_claude_md_checklist_names_the_checker_and_a_single_visual_read(self):
        text = CLAUDE_MD.read_text(encoding="utf-8")
        self.assertIn("tools/check_cv.py", text)
        self.assertRegex(text, r"visually inspected \*\*once\*\*")

    def test_cv_template_loop_runs_the_checker_before_the_visual_read(self):
        text = CV_TEMPLATES.read_text(encoding="utf-8")
        loop = text.partition("## Compile-and-Inspect Loop")[2].partition("\n## ")[0]
        self.assertIn("tools/check_cv.py cv", loop)
        self.assertIn("Read the PDF via the Read tool **once**", loop)


if __name__ == "__main__":
    unittest.main()
