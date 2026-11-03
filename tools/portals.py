#!/usr/bin/env python3
"""List installed job-portal skills from .agents/skills/*/SKILL.md frontmatter.

/scrape needs to know which portals are installed, which are enabled, and
where each portal's CLI lives. Reading every SKILL.md to learn that costs
tens of kilobytes of context; this tool answers it with one small JSON array.
It reads the frontmatter with a minimal YAML-subset parser so it works with
the standard library only (no PyYAML).

Usage:
    python tools/portals.py list
    python tools/portals.py list --enabled-only
    python tools/portals.py list --format table --root /path/to/repo

Output (json, default): a JSON array sorted by name, one object per skill:
    {"name", "enabled", "version", "cli", "skill_md", "has_cli",
     "description_first_line", "warnings"}
`enabled` defaults to true when the key is absent. A skill whose SKILL.md
has no frontmatter or no `allowed-tools` is still listed, with `cli` null
and the problem described in `warnings`, so one malformed fork-added skill
never hides the others.

Exit codes: 0 success, 1 the root has no .agents/skills directory, 2 usage.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SKILLS_DIR = Path(".agents") / "skills"

TRUE_WORDS = {"true", "yes", "on"}
FALSE_WORDS = {"false", "no", "off"}


class PortalsError(Exception):
    """Raised when the listing cannot be produced at all."""


def strip_inline_comment(value):
    """Drop a trailing ` # comment` from a scalar.

    As in YAML, a quote only quotes when it opens the value; an apostrophe
    inside a plain scalar (`Don't search #tags`) is ordinary text.
    """
    value = value.strip()
    if value[:1] in ("'", '"'):
        close = value.find(value[0], 1)
        return value[: close + 1] if close != -1 else value
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1] in " \t"):
            return value[:index].rstrip()
    return value


def unquote(value):
    """Remove one layer of matching single or double quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text):
    """Parse the leading `---` block of a SKILL.md into a flat dict.

    Supports the subset the shipped skills use: `key: scalar` lines (with
    inline `# comments` stripped), `key: >` / `key: |` block scalars whose
    continuation lines are indented, and quoted scalars. Returns
    (mapping, warnings); mapping is None when no frontmatter block exists
    or it is unterminated.
    """
    warnings = []
    if not text.startswith("---"):
        return None, ["missing frontmatter (file must start with ---)"]
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return None, ["unterminated frontmatter (no closing ---)"]

    data = {}
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0] in " \t":
            warnings.append(f"frontmatter line {index}: unexpected indentation: {stripped[:40]!r}")
            continue
        key, sep, rest = line.partition(":")
        if not sep or not key.strip() or " " in key.strip():
            warnings.append(f"frontmatter line {index}: not a 'key: value' line: {stripped[:40]!r}")
            continue
        key = key.strip()
        value = strip_inline_comment(rest.strip())
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            block = []
            while index < end and (not lines[index].strip() or lines[index][0] in " \t"):
                block.append(lines[index].strip())
                index += 1
            parts = [part for part in block if part]
            joiner = " " if value.startswith(">") else "\n"
            data[key] = joiner.join(parts)
            data.setdefault("\0blocks", {})[key] = parts
            continue
        data[key] = unquote(value)
    return data, warnings


def parse_bool(value, default=True):
    """Interpret true/false/yes/no case-insensitively; anything else -> default."""
    if value is None:
        return default, None
    word = str(value).strip().lower()
    if word in ("", "null", "~"):  # YAML null: same as an absent key
        return default, None
    if word in TRUE_WORDS:
        return True, None
    if word in FALSE_WORDS:
        return False, None
    return default, f"unrecognized enabled value {value!r}; treating as {str(default).lower()}"


def cli_from_allowed_tools(value):
    """Return the `bun run <path>` target from an allowed-tools value, or None."""
    if not isinstance(value, str):
        return None
    match = re.search(r"bun run ([^\s)]+)", value)
    if not match:
        return None
    target = match.group(1).rstrip("*")
    return target or None


def resolve_cli(root, target):
    """Skill paths resolve relative to the repo root, then to .agents/.

    Mirrors lint_skills.py, including glob targets such as
    `.agents/skills/*/cli/src/cli.ts`, which count when any file matches.
    """
    if "*" in target:
        return any(path.is_file() for base in (root, root / ".agents") for path in base.glob(target))
    for candidate in (root / target, root / ".agents" / target):
        if candidate.is_file():
            return True
    return False


def describe_skill(root, skill_md):
    """Build one listing record for a SKILL.md; never raises on bad content."""
    directory = skill_md.parent.name
    record = {
        "name": directory,
        "enabled": True,
        "version": None,
        "cli": None,
        "skill_md": skill_md.relative_to(root).as_posix(),
        "has_cli": False,
        "description_first_line": "",
        "warnings": [],
    }
    try:
        text = skill_md.read_text(encoding="utf-8-sig")  # tolerate a Windows-editor BOM
    except (OSError, UnicodeDecodeError) as exc:
        record["warnings"].append(f"could not read SKILL.md: {exc}")
        return record

    data, warnings = parse_frontmatter(text)
    record["warnings"].extend(warnings)
    if data is None:
        return record

    blocks = data.pop("\0blocks", {})
    name = data.get("name")
    if name:
        record["name"] = str(name)
    else:
        record["warnings"].append("frontmatter has no 'name'; using directory name")

    version = data.get("version")
    record["version"] = str(version) if version not in (None, "") else None

    enabled, warning = parse_bool(data.get("enabled"), default=True)
    record["enabled"] = enabled
    if warning:
        record["warnings"].append(warning)

    if "description" in blocks and blocks["description"]:
        record["description_first_line"] = blocks["description"][0]
    elif data.get("description"):
        record["description_first_line"] = str(data["description"]).splitlines()[0].strip()
    else:
        record["warnings"].append("frontmatter has no 'description'")

    allowed = data.get("allowed-tools")
    if allowed is None:
        record["warnings"].append("frontmatter has no 'allowed-tools'; cli unknown")
    else:
        cli = cli_from_allowed_tools(allowed)
        if cli is None:
            record["warnings"].append(f"allowed-tools has no 'bun run <path>' entry: {allowed!r}")
        else:
            record["cli"] = cli
            record["has_cli"] = resolve_cli(root, cli)
            if not record["has_cli"]:
                record["warnings"].append(f"cli file not found: {cli}")
    return record


def list_portals(root, enabled_only=False):
    root = Path(root).resolve()
    skills_dir = root / SKILLS_DIR
    if not skills_dir.is_dir():
        raise PortalsError(f"no {SKILLS_DIR.as_posix()} directory under {root}")
    records = [describe_skill(root, path) for path in sorted(skills_dir.glob("*/SKILL.md"))]
    if enabled_only:
        records = [record for record in records if record["enabled"]]
    records.sort(key=lambda record: record["name"])
    return records


def format_table(records):
    if not records:
        return "(no portal skills found)"
    headers = ("NAME", "ENABLED", "VERSION", "CLI", "WARNINGS")
    rows = [
        (
            record["name"],
            "yes" if record["enabled"] else "no",
            record["version"] or "-",
            (record["cli"] or "-") + ("" if record["has_cli"] or not record["cli"] else " (missing)"),
            "; ".join(record["warnings"]) or "-",
        )
        for record in records
    ]
    widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) for row in [headers] + rows]
    return "\n".join(line.rstrip() for line in lines)


def build_parser():
    parser = argparse.ArgumentParser(
        description="List installed job-portal skills and their enabled toggles."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list portal skills under <root>/.agents/skills")
    list_parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root containing .agents/skills (default: this repo)",
    )
    list_parser.add_argument(
        "--enabled-only",
        action="store_true",
        help="omit portals whose SKILL.md sets enabled: false",
    )
    list_parser.add_argument(
        "--format",
        choices=("json", "table"),
        default="json",
        help="output format (default: json)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        records = list_portals(args.root, enabled_only=args.enabled_only)
    except PortalsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "table":
        print(format_table(records))
    else:
        print(json.dumps(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
