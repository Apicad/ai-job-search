import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.portals import (
    cli_from_allowed_tools,
    main,
    parse_bool,
    parse_frontmatter,
    strip_inline_comment,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_PORTALS = (
    "freehire-search",
    "jobbank-search",
    "jobdanmark-search",
    "jobindex-search",
    "jobnet-search",
    "linkedin-search",
)
DANISH_PORTALS = ("jobbank-search", "jobdanmark-search", "jobindex-search", "jobnet-search")


def run_main(argv):
    """Run the CLI via main() and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def write_skill(root, directory, frontmatter, cli=True, body="# Skill\n"):
    skill_dir = root / ".agents" / "skills" / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    if cli:
        cli_file = skill_dir / "cli" / "src" / "cli.ts"
        cli_file.parent.mkdir(parents=True)
        cli_file.write_text("// stub\n", encoding="utf-8")


def file_enabled_flag(skill_md):
    """Derive a shipped portal's enabled flag from its file (absent -> True)."""
    text = skill_md.read_text(encoding="utf-8")
    end = text.find("\n---", 4)
    match = re.search(r"^enabled:\s*([A-Za-z]+)", text[4:end], re.MULTILINE)
    if not match:
        return True
    return match.group(1).lower() in ("true", "yes", "on")


class ParserHelpers(unittest.TestCase):
    def test_strip_inline_comment(self):
        self.assertEqual(strip_inline_comment("true  # set to false ..."), "true")
        self.assertEqual(strip_inline_comment("true"), "true")
        self.assertEqual(strip_inline_comment('"a # not comment"  # real'), '"a # not comment"')
        self.assertEqual(strip_inline_comment("#only"), "")
        # A quote only quotes when it opens the value (YAML plain-scalar rule).
        self.assertEqual(strip_inline_comment("Don't search #hashtags here"), "Don't search")
        self.assertEqual(strip_inline_comment("o'neill-search  # comment"), "o'neill-search")
        self.assertEqual(strip_inline_comment("'it''s'  # c"), "'it'")

    def test_parse_bool_accepts_words_case_insensitively(self):
        for word in ("true", "True", "YES", "on"):
            self.assertEqual(parse_bool(word)[0], True, word)
        for word in ("false", "FALSE", "no", "Off"):
            self.assertEqual(parse_bool(word)[0], False, word)
        self.assertEqual(parse_bool(None), (True, None))
        value, warning = parse_bool("maybe")
        self.assertTrue(value)
        self.assertIn("maybe", warning)
        for null_like in ("", "  ", "null", "~"):
            self.assertEqual(parse_bool(null_like), (True, None), repr(null_like))

    def test_cli_from_allowed_tools(self):
        self.assertEqual(
            cli_from_allowed_tools("Bash(bun run .agents/skills/x/cli/src/cli.ts *)"),
            ".agents/skills/x/cli/src/cli.ts",
        )
        self.assertIsNone(cli_from_allowed_tools("Bash(python foo.py *)"))
        self.assertIsNone(cli_from_allowed_tools(None))

    def test_parse_frontmatter_folded_block_and_comments(self):
        text = (
            "---\n"
            "name: demo\n"
            "description: >\n"
            "  First line here\n"
            "  second line\n"
            "\n"
            "context: fork\n"
            "enabled: false  # a comment\n"
            "allowed-tools: Bash(bun run x/cli.ts *)\n"
            "---\n"
            "# body\n"
        )
        data, warnings = parse_frontmatter(text)
        self.assertEqual(warnings, [])
        self.assertEqual(data["name"], "demo")
        self.assertEqual(data["enabled"], "false")
        self.assertEqual(data["description"], "First line here second line")
        self.assertEqual(data["\0blocks"]["description"], ["First line here", "second line"])

    def test_parse_frontmatter_missing_or_unterminated(self):
        self.assertIsNone(parse_frontmatter("# no frontmatter\n")[0])
        self.assertIsNone(parse_frontmatter("---\nname: x\n")[0])


class TempSkillTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        write_skill(
            self.root,
            "alpha-search",
            "---\n"
            "name: alpha-search\n"
            "version: 2.1.0\n"
            "description: >\n"
            "  Alpha portal first line\n"
            "  and more text.\n"
            "context: fork\n"
            "enabled: true  # set to false to keep this portal installed but have /scrape skip it\n"
            "allowed-tools: Bash(bun run .agents/skills/alpha-search/cli/src/cli.ts *)\n"
            "---\n",
        )
        write_skill(
            self.root,
            "beta-search",
            "---\n"
            "name: beta-search\n"
            "version: 1.0.0\n"
            "description: Beta portal single line\n"
            "enabled: No\n"
            "allowed-tools: Bash(bun run .agents/skills/beta-search/cli/src/cli.ts *)\n"
            "---\n",
            cli=False,
        )
        # Malformed: no enabled key, a bare line without a colon, no allowed-tools.
        write_skill(
            self.root,
            "gamma-search",
            "---\n"
            "name: gamma-search\n"
            "this line is not yaml\n"
            "description: Gamma portal\n"
            "---\n",
            cli=False,
        )

    def test_list_json_via_main(self):
        code, out, err = run_main(["list", "--root", str(self.root)])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        records = json.loads(out)
        self.assertEqual([r["name"] for r in records], ["alpha-search", "beta-search", "gamma-search"])

        alpha, beta, gamma = records
        self.assertTrue(alpha["enabled"])
        self.assertEqual(alpha["version"], "2.1.0")
        self.assertEqual(alpha["cli"], ".agents/skills/alpha-search/cli/src/cli.ts")
        self.assertTrue(alpha["has_cli"])
        self.assertEqual(alpha["skill_md"], ".agents/skills/alpha-search/SKILL.md")
        self.assertEqual(alpha["description_first_line"], "Alpha portal first line")
        self.assertEqual(alpha["warnings"], [])

        self.assertFalse(beta["enabled"])
        self.assertEqual(beta["cli"], ".agents/skills/beta-search/cli/src/cli.ts")
        self.assertFalse(beta["has_cli"])
        self.assertEqual(beta["description_first_line"], "Beta portal single line")
        self.assertTrue(any("not found" in w for w in beta["warnings"]))

        self.assertTrue(gamma["enabled"], "missing enabled key defaults to true")
        self.assertIsNone(gamma["cli"])
        self.assertFalse(gamma["has_cli"])
        self.assertIsNone(gamma["version"])
        self.assertEqual(gamma["description_first_line"], "Gamma portal")
        self.assertTrue(any("allowed-tools" in w for w in gamma["warnings"]))
        self.assertTrue(any("not yaml" in w for w in gamma["warnings"]))

    def test_enabled_only_filters(self):
        code, out, _ = run_main(["list", "--root", str(self.root), "--enabled-only"])
        self.assertEqual(code, 0)
        self.assertEqual([r["name"] for r in json.loads(out)], ["alpha-search", "gamma-search"])

    def test_skill_without_frontmatter_is_listed_with_warning(self):
        write_skill(self.root, "delta-search", "# Delta\n\nNo frontmatter at all.\n", cli=False)
        code, out, _ = run_main(["list", "--root", str(self.root)])
        self.assertEqual(code, 0)
        delta = [r for r in json.loads(out) if r["name"] == "delta-search"]
        self.assertEqual(len(delta), 1)
        self.assertTrue(delta[0]["enabled"])
        self.assertIsNone(delta[0]["cli"])
        self.assertTrue(any("frontmatter" in w for w in delta[0]["warnings"]))

    def test_table_format(self):
        code, out, _ = run_main(["list", "--root", str(self.root), "--format", "table"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("NAME"))
        self.assertEqual(len(lines), 4)
        self.assertIn("alpha-search", lines[1])
        self.assertIn("(missing)", lines[2])

    def test_missing_skills_dir_exits_1(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = run_main(["list", "--root", empty])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(".agents/skills", err)

    def test_usage_error_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["list", "--format", "xml"])
        self.assertEqual(ctx.exception.code, 2)


class Regressions(unittest.TestCase):
    """Each case reproduces a divergence from YAML/lint_skills that was fixed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def records(self, *extra):
        code, out, err = run_main(["list", "--root", str(self.root), *extra])
        self.assertEqual(code, 0, err)
        return {r["name"]: r for r in json.loads(out)}

    def test_bom_file_still_honors_enabled_false(self):
        write_skill(
            self.root,
            "bom-search",
            "\ufeff---\n"
            "name: bom-search\n"
            "description: Saved by a Windows editor\n"
            "enabled: false\n"
            "allowed-tools: Bash(bun run .agents/skills/bom-search/cli/src/cli.ts *)\n"
            "---\n",
        )
        record = self.records()["bom-search"]
        self.assertFalse(record["enabled"])
        self.assertEqual(record["warnings"], [])
        self.assertTrue(record["has_cli"])
        self.assertNotIn("bom-search", self.records("--enabled-only"))

    def test_crlf_line_endings(self):
        write_skill(
            self.root,
            "crlf-search",
            "---\r\nname: crlf-search\r\ndescription: >\r\n  CRLF first\r\n  more\r\n"
            "enabled: false  # c\r\n"
            "allowed-tools: Bash(bun run .agents/skills/crlf-search/cli/src/cli.ts *)\r\n---\r\n",
        )
        record = self.records()["crlf-search"]
        self.assertFalse(record["enabled"])
        self.assertEqual(record["description_first_line"], "CRLF first")
        self.assertEqual(record["warnings"], [])

    def test_apostrophe_in_plain_scalar_does_not_swallow_comment(self):
        write_skill(
            self.root,
            "apos-search",
            "---\n"
            "name: apos-search\n"
            "description: Don't search #hashtags here  # note\n"
            "enabled: true  # it's on\n"
            "allowed-tools: Bash(bun run .agents/skills/apos-search/cli/src/cli.ts *)\n"
            "---\n",
        )
        record = self.records()["apos-search"]
        self.assertEqual(record["description_first_line"], "Don't search")
        self.assertTrue(record["enabled"])
        self.assertEqual(record["warnings"], [])

    def test_empty_enabled_value_is_absent_not_a_warning(self):
        write_skill(
            self.root,
            "null-search",
            "---\nname: null-search\ndescription: x\nenabled:\n"
            "allowed-tools: Bash(bun run .agents/skills/null-search/cli/src/cli.ts *)\n---\n",
        )
        record = self.records()["null-search"]
        self.assertTrue(record["enabled"])
        self.assertEqual(record["warnings"], [])

    def test_glob_cli_target_resolves_like_lint_skills(self):
        write_skill(
            self.root,
            "glob-search",
            "---\nname: glob-search\ndescription: x\n"
            "allowed-tools: Bash(bun run .agents/skills/*/cli/src/cli.ts *)\n---\n",
        )
        record = self.records()["glob-search"]
        self.assertEqual(record["cli"], ".agents/skills/*/cli/src/cli.ts")
        self.assertTrue(record["has_cli"])
        self.assertEqual(record["warnings"], [])

        write_skill(
            self.root,
            "noglob-search",
            "---\nname: noglob-search\ndescription: x\n"
            "allowed-tools: Bash(bun run .agents/skills/*/cli/src/nothing.ts *)\n---\n",
            cli=False,
        )
        record = self.records()["noglob-search"]
        self.assertFalse(record["has_cli"])
        self.assertTrue(any("not found" in w for w in record["warnings"]))

    def test_add_portal_path_form_resolves_via_agents_dir(self):
        # /add-portal spec: `allowed-tools: Bash(bun run skills/<name>/cli/src/cli.ts *)`
        write_skill(
            self.root,
            "local-search",
            "---\nname: local-search\ndescription: x\n"
            "allowed-tools: Bash(bun run skills/local-search/cli/src/cli.ts *)\n---\n",
        )
        record = self.records()["local-search"]
        self.assertEqual(record["cli"], "skills/local-search/cli/src/cli.ts")
        self.assertTrue(record["has_cli"])
        self.assertEqual(record["warnings"], [])


class RealRepo(unittest.TestCase):
    def test_shipped_portals_listed(self):
        code, out, err = run_main(["list", "--root", str(REPO_ROOT)])
        self.assertEqual(code, 0, err)
        records = {r["name"]: r for r in json.loads(out)}
        for name in SHIPPED_PORTALS:
            self.assertIn(name, records)
            record = records[name]
            self.assertEqual(record["cli"], f".agents/skills/{name}/cli/src/cli.ts")
            self.assertTrue(record["has_cli"], name)
            self.assertEqual(record["skill_md"], f".agents/skills/{name}/SKILL.md")
            self.assertEqual(record["warnings"], [], name)
            self.assertTrue(record["description_first_line"], name)
            self.assertTrue(record["version"], name)

        self.assertTrue(records["linkedin-search"]["enabled"])
        for name in DANISH_PORTALS:
            expected = file_enabled_flag(REPO_ROOT / ".agents" / "skills" / name / "SKILL.md")
            self.assertEqual(records[name]["enabled"], expected, name)

        self.assertEqual(list(records), sorted(records))


if __name__ == "__main__":
    unittest.main()
