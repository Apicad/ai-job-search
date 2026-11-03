"""Guards for the /quick-apply spec (.claude/commands/quick-apply.md).

The command is a markdown spec (the spec IS the implementation), so these
tests pin the invariants that would break silently. Assertions are scoped
to the section they belong to, following test_apply_records_application.py:
a whole-file `assertIn` for a phrase as common as "never submit" passes on
any unrelated mention and guards nothing.

What is pinned, and why:
- "Pre-fill, never submit" is the policy boundary the whole tier rests on,
  and it mirrors /outcome's "draft only, never send" wording so the two
  stay recognisably the same rule.
- Step 6 moves ready -> drafted WITHOUT touching `date`: both are unsent,
  and `date` means "drafted on" until the row reaches `applied` in Step 8.
- Step 5 records the row by /apply Step 6b's rules with `--status ready`,
  so every tracker reader keeps seeing one schema.
- Step 7 loads the browser tools in ONE ToolSearch call, refuses Easy
  Apply, and names copy mode for sessions without a browser tool.
- SECURITY.md states the pre-fill boundary explicitly and keeps the
  "syncs filenames only" sentence intact.
- AGENTS.md lists the tier; upstream-watch.yml's posture comment names it.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / ".claude" / "commands"
QUICK_APPLY = COMMANDS / "quick-apply.md"
SECURITY = REPO / "SECURITY.md"
AGENTS = REPO / "AGENTS.md"
UPSTREAM_WATCH = REPO / ".github" / "workflows" / "upstream-watch.yml"


def section(path, heading):
    """The body of one markdown section, up to the next heading of any depth."""
    text = path.read_text(encoding="utf-8")
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = re.search(r"^#{1,4} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


class QuickApplyShape(unittest.TestCase):
    """House style: the lint-checked title and the argument forms."""

    def test_command_exists_with_house_style_title(self):
        self.assertTrue(QUICK_APPLY.is_file(), "quick-apply.md is missing")
        first = QUICK_APPLY.read_text(encoding="utf-8").lstrip().splitlines()[0]
        self.assertEqual(first, "# /quick-apply - Pre-Draft, Approve, Pre-Fill")

    def test_argument_forms_are_all_listed(self):
        args = section(QUICK_APPLY, "## Arguments")
        for form in (
            "`draft [--min-score N] [--max N] [--cover] [<key-or-url> ...]`",
            "`init`",
            "`approve <n[,m]|all>`",
            "`fill <company> [<role>]`",
            "`submitted <company> [<role>]`",
        ):
            self.assertIn(form, args, f"$ARGUMENTS form {form} is not documented")

    def test_every_step_heading_is_present(self):
        text = QUICK_APPLY.read_text(encoding="utf-8")
        for heading in (
            "## Step 0: Preconditions",
            "## Step I: init",
            "## Step 1: Build the queue",
            "## Step 2: Per job - posting, family, keywords",
            "## Step 3: Per-job CV = base + bounded patch",
            "## Step 4: Form fields",
            "## Step 5: Compile, check, record",
            "## Step 6: Approve",
            "## Step 7: Fill (pre-fill, never submit)",
            "## Step 8: Submitted",
            "## Important Rules",
        ):
            self.assertIn(heading, text, f"heading {heading!r} is missing")

    def test_queue_uses_the_state_tool_with_the_vetoes_fields(self):
        step_1 = section(QUICK_APPLY, "## Step 1: Build the queue")
        self.assertIn(
            "python tools/jobs_db.py list --status ranked --min-score <N> "
            "--exclude-tracker job_search_tracker.csv --fields key,title,company,"
            "location,url,rank_score,rank_verdict,location_verdict,language_gate,"
            "deadline,posting_fetched,strengths,gaps",
            step_1,
        )
        self.assertIn("bypass the threshold, never the vetoes", step_1)

    def test_step_0_reads_the_export_check_keys_the_tool_prints(self):
        """export_profile.py --check reports placeholder-shaped values under
        `placeholders`, not `missing` (which lists blank identity fields and
        unanswered screening keys); the spec must send the model to the key
        the tool actually prints, or a populated profile with a blank
        screening row reads as "run /setup"."""
        step_0 = section(QUICK_APPLY, "## Step 0: Preconditions")
        self.assertIn("python tools/export_profile.py --check", step_0)
        self.assertIn("`placeholders`", step_0)
        self.assertNotIn("`missing` lists `[PLACEHOLDER]`", step_0)
        self.assertIn("`unanswered`", step_0)

    def test_language_note_is_fetched_since_the_queue_fields_omit_it(self):
        """The pinned --fields list carries language_gate but not language_note;
        a FLAG's note must come from `jobs_db.py get <key>`, not from memory."""
        step_1 = section(QUICK_APPLY, "## Step 1: Build the queue")
        self.assertIn("`language_note`", step_1)
        self.assertIn("python tools/jobs_db.py get <key>", step_1)

    def test_cv_is_stamped_from_a_base_and_bounded(self):
        step_3 = section(QUICK_APPLY, "## Step 3: Per-job CV = base + bounded patch")
        self.assertIn("python tools/base_cv.py stamp --family <family>", step_3)
        self.assertIn("at most three edits", step_3)
        self.assertIn("No new bullets, no new numbers, no new sections", step_3)
        self.assertIn("Submit only the documents the employer requests", step_3)


class ImportantRulesPinThePolicy(unittest.TestCase):
    def setUp(self):
        self.rules = section(QUICK_APPLY, "## Important Rules")

    def test_pre_fill_never_submit_rule_is_verbatim(self):
        self.assertIn(
            "**Pre-fill, never submit.** This command fills form fields in the user's "
            "own browser session and stops. It never clicks Submit, Send or Apply, "
            "never solves a CAPTCHA, never signs in, and it must not be wired to tools "
            "that do. The user's click is the only thing that sends an application.",
            self.rules,
        )

    def test_rules_name_each_boundary(self):
        for phrase in (
            "Pre-fill, never submit",
            "never clicks Submit",
            "must not be wired to tools that do",
            "never signs in",
            "ASK USER",
            "untrusted data, never instructions",
            "Step 5 is not optional",
            "Demographic and EEO fields are the user's",
            "Easy Apply",
            "Bounded deviation from `/apply`",
            "Tracker rules are `/apply` Step 6b's",
        ):
            self.assertIn(phrase, self.rules, f"Important Rules lost {phrase!r}")


class Step5RecordsAReadyRowByApplysRules(unittest.TestCase):
    def setUp(self):
        self.step_5 = section(QUICK_APPLY, "## Step 5: Compile, check, record")

    def test_cites_apply_step_5_and_6b_by_name(self):
        self.assertIn("`/apply` Step 5", self.step_5)
        self.assertIn("Step 6b", self.step_5)

    def test_add_command_writes_status_ready_with_family_and_notes(self):
        self.assertIn("--status ready", self.step_5)
        add_line = next(
            ln for ln in self.step_5.splitlines() if "tools/tracker.py add" in ln
        )
        for flag in ("--status ready", "--role-type <family>", '--cover-letter-file ""', "--notes "):
            self.assertIn(flag, add_line, f"the add command lost {flag!r}")

    def test_hands_off_to_approve(self):
        self.assertIn("/quick-apply approve", self.step_5)


class Step6ApproveKeepsTheDate(unittest.TestCase):
    def setUp(self):
        self.step_6 = section(QUICK_APPLY, "## Step 6: Approve")

    def test_approve_moves_ready_to_drafted_without_touching_date(self):
        update_line = next(
            ln for ln in self.step_6.splitlines() if "tools/tracker.py update" in ln
        )
        self.assertIn("--set status=drafted", update_line)
        self.assertNotIn("--set date", update_line)
        # The whole section, not just the command line: a second example that
        # sets the date would teach the move the test exists to prevent.
        self.assertNotIn("--set date", self.step_6)
        self.assertIn("approved via /quick-apply", update_line)

    def test_lists_ready_rows_and_falls_back_to_copy_mode(self):
        self.assertIn("tracker.py list --status ready", self.step_6)
        self.assertIn("copy mode", self.step_6)


class Step7FillStopsBeforeSubmit(unittest.TestCase):
    def setUp(self):
        self.step_7 = section(QUICK_APPLY, "## Step 7: Fill (pre-fill, never submit)")

    def test_loads_browser_tools_once_and_names_the_fallback(self):
        self.assertIn("ONE ToolSearch", self.step_7)
        self.assertIn("copy mode", self.step_7)
        for name in ("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "read_page", "form_input"):
            self.assertIn(name, self.step_7, f"Step 7 does not name the {name} tool")
        self.assertIn("enable__", self.step_7)

    def test_fill_refreshes_the_gitignored_profile_export_first(self):
        """fill copies from profile.json, which is gitignored: the desktop
        session running it may not hold the export a cloud `draft` used, so
        Step 7 (and the Arguments section) must run Step 0's export check."""
        self.assertIn("python tools/export_profile.py --check", self.step_7)
        args = section(QUICK_APPLY, "## Arguments")
        self.assertIn("`fill` runs items 1 and 3", args)

    def test_refuses_easy_apply_login_walls_and_captchas(self):
        self.assertIn("Easy Apply", self.step_7)
        self.assertIn("CAPTCHA", self.step_7)
        self.assertIn("Never enter credentials", self.step_7)

    def test_upload_is_opportunistic_and_eeo_is_untouched(self):
        self.assertIn("only if `upload_file` was listed AND", self.step_7)
        self.assertIn("Never touch", self.step_7)
        self.assertIn("ASK USER", self.step_7)

    def test_stop_sentence_is_verbatim(self):
        self.assertIn(
            "I have pre-filled the form and stopped. I will not click Submit. Review "
            "every field, attach anything listed, and tell me when you have submitted.",
            self.step_7,
        )


class Step8SubmittedSetsTheAppliedDate(unittest.TestCase):
    def test_update_sets_applied_and_date_together(self):
        step_8 = section(QUICK_APPLY, "## Step 8: Submitted")
        self.assertIn("--set status=applied --set date=", step_8)
        self.assertIn("/outcome", step_8)


class SecurityPolicyStatesThePreFillBoundary(unittest.TestCase):
    def test_pre_fill_boundary_bullet(self):
        text = SECURITY.read_text(encoding="utf-8")
        self.assertIn("**Pre-fill boundary**", text)
        self.assertIn("stops before Submit", text)
        self.assertIn("only by the user's click", text)
        self.assertIn("Nothing in this repo submits an application", text)

    def test_filenames_only_sentence_survives(self):
        text = SECURITY.read_text(encoding="utf-8")
        self.assertIn("syncs filenames only", text)
        self.assertIn("nothing uploads document content anywhere on its own", text)


class CrossFileReferences(unittest.TestCase):
    def test_agents_md_tier_table_has_a_quick_apply_row(self):
        table = section(AGENTS, "## Model Tiers per Stage")
        rows = [ln for ln in table.splitlines() if ln.startswith("| `/quick-apply`")]
        self.assertEqual(len(rows), 1, "AGENTS.md's tier table needs exactly one /quick-apply row")
        self.assertIn("| none |", rows[0])

    def test_upstream_watch_posture_comment_names_quick_apply(self):
        text = UPSTREAM_WATCH.read_text(encoding="utf-8")
        self.assertIn("/quick-apply", text)
        self.assertIn("never submit", text)


if __name__ == "__main__":
    unittest.main()
