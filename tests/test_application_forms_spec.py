"""Guards for `08-application-forms.md` after the /quick-apply extension.

The spec used to say form fields are saved "alongside their other application
material" - no fixed path - and had no screening-answer field type. The fast
tier (/quick-apply) reads the file back by label and copies screening answers
from `01-candidate-profile.md`, so three things must stay pinned:

1. The output path is fixed (`documents/applications/<company>_<role>/
   form_fields.txt`) and `documents/README.md` admits the file as one `/setup`
   ignores, next to the existing `interview_prep_<stage>.md` sentence.
2. Screening answers are copied, never drafted; a blank is `ASK USER`, never
   a plausible guess; EEO questions go under `LEFT FOR YOU`.
3. The file layout is machine-parsable (`## FIELD:` blocks, `## LEFT FOR YOU`,
   `NOTE TO SELF:` kept internal), so the fill step can map form fields to it.

Section-scoped substring pins, like the other spec tests.
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FORMS = REPO / ".claude" / "skills" / "job-application-assistant" / "08-application-forms.md"
DOCS_README = REPO / "documents" / "README.md"
APPLY = REPO / ".claude" / "commands" / "apply.md"

FIXED_PATH = "documents/applications/<company>_<role>/form_fields.txt"


def _sections(text: str) -> dict[str, str]:
    """Map each `## ` heading to its body, up to the next `## ` heading.

    Fence-aware: a `## ` line inside a ``` code block (the layout example in
    08, the outcome.md example in the README) is content, not a heading.
    """
    result: dict[str, list[str]] = {}
    current = None
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            current = line[3:].strip()
            result[current] = []
            continue
        if current is not None:
            result[current].append(line)
    return {heading: "\n".join(lines) for heading, lines in result.items()}


class ApplicationFormsSpec(unittest.TestCase):
    def setUp(self):
        self.text = FORMS.read_text(encoding="utf-8")
        self.sections = _sections(self.text)

    def _section(self, heading: str) -> str:
        self.assertIn(heading, self.sections, f"08 has no section {heading!r}")
        return self.sections[heading]

    def test_framework_version_bumped(self):
        self.assertTrue(self.text.startswith("---\n"))
        frontmatter = self.text[4:self.text.find("\n---", 4)]
        self.assertRegex(frontmatter, r"^framework_version:\s*1\.1\.0\s*$")

    def test_screening_answers_field_type_exists_before_output_format(self):
        self.assertIn("## Field type: screening answers", self.text)
        self.assertLess(
            self.text.index("## Field type: screening answers"),
            self.text.index("## Output format"),
        )

    def test_screening_answers_are_copied_never_drafted(self):
        body = self._section("Field type: screening answers")
        self.assertIn("copied, never drafted", body)
        self.assertIn("`## Screening Answers` table in `01-candidate-profile.md`", body)
        self.assertIn("tools/export_profile.py", body)
        self.assertIn("profile.json", body)

    def test_blank_screening_answer_is_ask_user_never_a_guess(self):
        body = self._section("Field type: screening answers")
        self.assertIn("ASK USER", body)
        self.assertIn("never a plausible guess", body)

    def test_eeo_questions_are_left_for_the_candidate(self):
        body = self._section("Field type: screening answers")
        self.assertIn("LEFT FOR YOU", body)
        for word in ("Demographic", "disability", "veteran", "EEO"):
            self.assertIn(word, body)
        self.assertIn("never answered here or in any pre-fill", body)

    def test_output_format_names_the_fixed_path(self):
        body = self._section("Output format")
        self.assertIn(FIXED_PATH, body)
        self.assertIn("**Subfolder naming** rule in `documents/README.md`", body)
        self.assertIn("gitignored", body)
        self.assertIn("`/setup` ignores the file", body)
        self.assertIn("One file per employer", body)
        self.assertIn("`/quick-apply`", body)
        self.assertNotIn("alongside their other application material", body)

    def test_output_format_keeps_the_include_list_and_note_to_self(self):
        body = self._section("Output format")
        self.assertIn("A header naming the employer", body)
        self.assertIn("A dates quick-reference", body)
        self.assertIn("**`NOTE TO SELF` blocks**", body)
        self.assertIn("*not for pasting into the form*", body)

    def test_output_format_layout_is_machine_parsable(self):
        body = self._section("Output format")
        self.assertIn("# Application form fields - <Employer> - <Role> (<YYYY-MM-DD>)", body)
        self.assertIn("## DATES QUICK-REFERENCE", body)
        self.assertIn("## FIELD:", body)
        self.assertRegex(body, r"## FIELD: <label[^\n]*\[type: [^\n]*\[limit: [^\n]*\[count: ")
        self.assertIn("[variant: short]", body)
        self.assertIn("## LEFT FOR YOU", body)
        self.assertIn("NOTE TO SELF:", body)
        self.assertIn("never pasted", body)

    def test_output_format_layout_block_orders_the_headers(self):
        body = self._section("Output format")
        block = body.partition("```\n")[2].partition("\n```")[0]
        order = [
            "# Application form fields",
            "## DATES QUICK-REFERENCE",
            "## FIELD:",
            "[variant: short]",
            "## LEFT FOR YOU",
            "NOTE TO SELF:",
        ]
        positions = [block.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_checklist_pins_verbatim_screening_answers(self):
        body = self._section("Verification before handing it over")
        self.assertIn(
            "- [ ] Every screening answer matches `01`'s Screening Answers table verbatim; "
            "blanks say `ASK USER`",
            body,
        )
        self.assertIn("- [ ] `NOTE TO SELF` blocks clearly marked as internal", body)

    def test_grounding_rule_unchanged(self):
        self.assertIn("## The rule that governs everything here", self.text)
        self.assertIn("`01-candidate-profile.md`, the master CV (`cv/main_example.tex`)", self.text)


class DocumentsReadmeAdmitsFormFields(unittest.TestCase):
    def setUp(self):
        self.text = DOCS_README.read_text(encoding="utf-8")
        self.applications = _sections(self.text)["applications/"]

    def test_readme_names_form_fields_as_an_extra_file_setup_ignores(self):
        self.assertIn("`form_fields.txt`", self.applications)
        self.assertIn("08-application-forms.md", self.applications)
        self.assertIn("`/setup` ignores", self.applications)

    def test_readme_still_names_interview_prep_files(self):
        self.assertIn("interview_prep_", self.applications)
        self.assertIn(
            "`/setup` reads only the four files named above and ignores these.",
            self.applications,
        )

    def test_fixed_path_matches_the_readme_folder_layout(self):
        folder, _, filename = FIXED_PATH.rpartition("/")
        self.assertEqual(filename, "form_fields.txt")
        self.assertEqual(folder, "documents/applications/<company>_<role>")
        self.assertIn("<company>_<role>/", self.text)


class ApplyOfferStillResolvesToTheSpec(unittest.TestCase):
    """`/apply`'s optional offer saves "per that file's Output format", so it
    inherits the fixed path without naming it."""

    def test_apply_defers_to_the_output_format_section(self):
        text = APPLY.read_text(encoding="utf-8")
        section = text.partition("### Application-Form Fields")[2].partition("\n### ")[0]
        self.assertIn('Save per that file\'s "Output format" section', section)
        self.assertIn("08-application-forms.md", section)


if __name__ == "__main__":
    unittest.main()
