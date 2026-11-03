#!/usr/bin/env python3
"""Export the candidate profile to profile.json for the fill step.

Parses the grounding source `01-candidate-profile.md` (Identity bullets, the
Languages table, the Education table, the `### <title> - <company> (<start> -
<end>)` experience headings and the `## Screening Answers` table) plus the
`CV language:` line in CLAUDE.md, and writes one machine-readable
`profile.json` at the repo root. /quick-apply reads that file to pre-fill
application forms, so every value is copied verbatim - nothing is inferred,
and a line the parser cannot read becomes a warning, never a guess.

Screening-answer keys are the Question cell slugified (lowercase, runs of
non-alphanumerics -> "_", trimmed), so a row the user adds to the table
exports without a code change. A blank or `[PLACEHOLDER]`-shaped answer is
listed under "unanswered" and exported as "" (the fill step reports it as
ASK USER). A `[PLACEHOLDER]`-shaped value anywhere else (identity, languages,
education, experience, CV language) means /setup never ran: exit 1 unless
`--allow-placeholders`.

`years_experience` is the union of the experience intervals in whole months
(end month inclusive; "Present"/"now"/"current" = `--today`), rounded down
to years.

Output is one compact JSON value on stdout; human-readable errors go to
stderr. Exit 0 on success, 1 on a data/check error, 2 on a usage error.

Usage examples:
    python tools/export_profile.py
    python tools/export_profile.py --check
    python tools/export_profile.py --allow-placeholders --out /tmp/profile.json
    python tools/export_profile.py --repo /tmp/fixture --today 2026-11-03
"""

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_REL = Path(".claude/skills/job-application-assistant/01-candidate-profile.md")
CLAUDE_MD_REL = Path("CLAUDE.md")
OUT_REL = Path("profile.json")

SCHEMA = 1
YEARS_METHOD = "union of experience intervals, rounded down"

# Identity bullet label -> profile.json key.
IDENTITY_KEYS = {
    "name": "name",
    "location": "location",
    "phone": "phone",
    "email": "email",
    "linkedin": "linkedin",
    "github": "github",
    "status": "status",
    "constraints": "constraints",
}
IDENTITY_ORDER = [
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
]

PRESENT_WORDS = {"present", "now", "current", "currently", "ongoing", "today"}
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
# `- **Label:** value` (the template) and `- **Label**: value` (the style the same
# file uses under Independent Projects) are both accepted.
_BULLET_RE = re.compile(r"^-\s+\*\*(.+?)\*\*\s*:?\s*(.*)$")
_EXPERIENCE_RE = re.compile(r"^(?P<head>.+?)\s+\((?P<dates>[^()]+?)\)$")
# "<start> - <end>": any dash with spaces around it, an en/em dash with or without
# spaces (never part of a date token), or a bare "YYYY-YYYY" - the one hyphen form
# that cannot be confused with a YYYY-MM date.
_RANGE_SPLIT_RES = (
    re.compile(r"^(?P<start>.+?)\s+[-–—]\s+(?P<end>.+)$"),
    re.compile(r"^(?P<start>.+?)\s*[–—]\s*(?P<end>.+)$"),
    re.compile(r"^(?P<start>\d{4})-(?P<end>\d{4})$"),
)
_MD_LINK_RE = re.compile(r"^\[([^\]]*)\]\((\S+)\)$")
_AUTOLINK_RE = re.compile(r"^<(\S+)>$")
_URI_SCHEME_PREFIX_RE = re.compile(r"^(?:mailto|tel):", re.I)
# A dash alone in a cell or after a label means "none", not a value to type into a form.
_DASH_ONLY = {"-", "–", "—"}
# `[YOUR_NAME]`, `[YOUR_SALARY_EXPECTATION, with currency and basis]`,
# `[e.g. "2 weeks"]`, `[...]` - anything /setup should have replaced.
_PLACEHOLDER_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_]+(?:[,\s][^\]]*)?\]")
_CV_LANGUAGE_RE = re.compile(r"CV language:\**\s*(.*?)\s*$")


class ExportError(Exception):
    """Raised when the profile cannot be exported."""


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

def slugify(text):
    """Lowercase, non-alphanumeric runs -> "_", trimmed."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_placeholder(value):
    """True for a `[PLACEHOLDER]`-shaped value /setup should have replaced."""
    value = (value or "").strip()
    if not value:
        return False
    if value.startswith("[") and value.endswith("]") and "](" not in value:
        return True
    return bool(_PLACEHOLDER_TOKEN_RE.search(value))


def clean_value(value):
    """Strip whitespace, unwrap `[text](url)` / `<url>`, drop `mailto:`/`tel:`, "-" -> ""."""
    value = (value or "").strip()
    match = _MD_LINK_RE.match(value)
    if match:
        value = match.group(2)
    else:
        match = _AUTOLINK_RE.match(value)
        if match:
            value = match.group(1)
    value = _URI_SCHEME_PREFIX_RE.sub("", value).strip()
    if value in _DASH_ONLY:
        return ""
    return value


def split_date_range(dates):
    """Return (start, end) for "<start> - <end>" or None when no range is readable."""
    text = dates.strip()
    for regex in _RANGE_SPLIT_RES:
        match = regex.match(text)
        if match:
            return match.group("start").strip(), match.group("end").strip()
    return None


def strip_comments(text):
    return _COMMENT_RE.sub("", text)


def split_sections(lines):
    """Split markdown lines into [(h2_title, [lines])]; a preamble gets title ""."""
    sections = [("", [])]
    for line in lines:
        match = _H2_RE.match(line)
        if match:
            sections.append((match.group(1), []))
        else:
            sections[-1][1].append(line)
    return sections


def find_section(sections, title):
    wanted = title.lower()
    for name, body in sections:
        if name.lower() == wanted:
            return body
    return None


def parse_table(lines):
    """Return (header_cells, rows) for the first pipe table in lines."""
    header = None
    rows = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header is not None and rows:
                break  # table ended
            continue
        cells = [c.strip() for c in stripped.strip("|").replace("\\|", "\x00").split("|")]
        cells = [c.replace("\x00", "|") for c in cells]
        if header is None:
            header = cells
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells):
            continue  # separator row
        rows.append(cells)
    return header, rows


def parse_month(text, today):
    """Return (year, month) or None for an unparseable date fragment."""
    raw = text.strip().rstrip(".").strip()
    low = raw.lower()
    if low in PRESENT_WORDS:
        return today.year, today.month
    match = re.fullmatch(r"(\d{4})", low)
    if match:
        return int(match.group(1)), None
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?", low)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else None
    match = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", low)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else None
    match = re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", low)
    if match and match.group(1) in MONTHS:
        return int(match.group(2)), MONTHS[match.group(1)]
    match = re.fullmatch(r"(\d{4})\s+([a-z]+)\.?", low)
    if match and match.group(2) in MONTHS:
        return int(match.group(1)), MONTHS[match.group(2)]
    return None


def _month_index(year, month):
    return year * 12 + (month - 1)


def years_of_experience(experience, today, warnings):
    """Whole years covered by the union of experience intervals, rounded down."""
    intervals = []
    for entry in experience:
        start = parse_month(entry["start"], today)
        end = parse_month(entry["end"], today)
        if start is None or end is None:
            warnings.append(
                "experience: could not read the dates of "
                f"'{entry['title']} - {entry['company']}' "
                f"({entry['start']} - {entry['end']}); skipped in years_experience"
            )
            continue
        start_idx = _month_index(start[0], start[1] or 1)
        end_idx = _month_index(end[0], end[1] or 12)
        if end_idx < start_idx:
            warnings.append(
                f"experience: '{entry['title']} - {entry['company']}' ends before it "
                "starts; skipped in years_experience"
            )
            continue
        intervals.append((start_idx, end_idx))
    intervals.sort()
    months = 0
    current = None
    for start_idx, end_idx in intervals:
        if current is None or start_idx > current[1] + 1:
            if current is not None:
                months += current[1] - current[0] + 1
            current = [start_idx, end_idx]
        else:
            current[1] = max(current[1], end_idx)
    if current is not None:
        months += current[1] - current[0] + 1
    return months // 12


# --------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------

def parse_identity(body, warnings):
    identity = {key: "" for key in IDENTITY_ORDER}
    language_lines = []
    in_languages = False
    for line in body:
        stripped = line.strip()
        h3 = _H3_RE.match(stripped)
        if h3:
            in_languages = h3.group(1).strip().lower() == "languages"
            if not in_languages:
                warnings.append(f"identity: unrecognised subsection '{h3.group(1)}' ignored")
            continue
        if in_languages:
            language_lines.append(line)
            continue
        if not stripped:
            continue
        match = _BULLET_RE.match(stripped)
        if not match:
            warnings.append(f"identity: unparsed line: {stripped}")
            continue
        label = match.group(1).strip().rstrip(":").strip().lower()
        value = clean_value(match.group(2))
        key = IDENTITY_KEYS.get(label)
        if key is None:
            warnings.append(f"identity: unrecognised field '{match.group(1).strip()}' ignored")
            continue
        identity[key] = value
    if identity["name"]:
        parts = identity["name"].split()
        identity["first_name"] = parts[0]
        identity["last_name"] = " ".join(parts[1:])
    return identity, language_lines


def parse_languages(lines, warnings):
    header, rows = parse_table(lines)
    languages = []
    if header is None:
        warnings.append("languages: no table found under '### Languages'")
        return languages
    for cells in rows:
        if len(cells) < 2 or not cells[0]:
            warnings.append(f"languages: unparsed row: | {' | '.join(cells)} |")
            continue
        languages.append({"language": cells[0], "level": cells[1]})
    return languages


def parse_education(body, warnings):
    if body is None:
        warnings.append("education: section '## Education' not found")
        return []
    header, rows = parse_table(body)
    education = []
    if header is None:
        warnings.append("education: no table found under '## Education'")
        return education
    for cells in rows:
        if len(cells) < 3 or not cells[0]:
            warnings.append(f"education: unparsed row: | {' | '.join(cells)} |")
            continue
        education.append({"degree": cells[0], "period": cells[1], "institution": cells[2]})
    return education


def parse_experience(body, warnings):
    if body is None:
        warnings.append("experience: section '## Professional Experience' not found")
        return []
    experience = []
    for line in body:
        h3 = _H3_RE.match(line.strip())
        if not h3:
            continue
        heading = h3.group(1).strip()
        match = _EXPERIENCE_RE.match(heading)
        date_range = split_date_range(match.group("dates")) if match else None
        if not match or date_range is None:
            warnings.append(f"experience: unparsed heading: ### {heading}")
            continue
        head = match.group("head")
        title, sep, company = head.partition(" - ")
        if not sep:
            title, sep, company = head.partition(" – ")
        if not sep:
            warnings.append(f"experience: no '<title> - <company>' in heading: ### {heading}")
            continue
        experience.append(
            {
                "title": title.strip(),
                "company": company.strip(),
                "start": date_range[0],
                "end": date_range[1],
            }
        )
    return experience


def parse_screening(body, warnings):
    answers = {}
    unanswered = []
    if body is None:
        warnings.append("screening: section '## Screening Answers' not found")
        return answers, unanswered
    header, rows = parse_table(body)
    if header is None:
        warnings.append("screening: no table found under '## Screening Answers'")
        return answers, unanswered
    for cells in rows:
        if not cells or not cells[0]:
            warnings.append(f"screening: unparsed row: | {' | '.join(cells)} |")
            continue
        key = slugify(cells[0])
        if not key:
            warnings.append(f"screening: question cannot be slugified: {cells[0]!r}")
            continue
        answer = clean_value(cells[1]) if len(cells) > 1 else ""
        if not answer or is_placeholder(answer):
            answers[key] = ""
            unanswered.append(key)
        else:
            answers[key] = answer
    return answers, unanswered


def parse_cv_language(claude_md_path, warnings):
    if not claude_md_path.exists():
        warnings.append(f"cv_language: {claude_md_path} not found")
        return ""
    text = strip_comments(claude_md_path.read_text(encoding="utf-8"))
    for line in text.splitlines():
        match = _CV_LANGUAGE_RE.search(line)
        if match:
            return clean_value(match.group(1))
    warnings.append("cv_language: no 'CV language:' line in CLAUDE.md")
    return ""


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _relative(path, repo):
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path)


def build_profile(profile_path, claude_md_path, repo, today):
    """Parse the sources into the profile.json dict. Raises ExportError."""
    if not profile_path.exists():
        raise ExportError(f"profile not found: {profile_path}")
    warnings = []
    text = strip_comments(profile_path.read_text(encoding="utf-8"))
    lines = text.splitlines()
    sections = split_sections(lines)

    identity_body = find_section(sections, "Identity")
    if identity_body is None:
        raise ExportError("section '## Identity' not found in the profile")
    identity, language_lines = parse_identity(identity_body, warnings)
    languages = parse_languages(language_lines, warnings)
    education = parse_education(find_section(sections, "Education"), warnings)
    experience = parse_experience(find_section(sections, "Professional Experience"), warnings)
    screening, unanswered = parse_screening(find_section(sections, "Screening Answers"), warnings)
    identity["cv_language"] = parse_cv_language(claude_md_path, warnings)

    return {
        "schema": SCHEMA,
        "generated": today.isoformat(),
        "source_files": [_relative(profile_path, repo), _relative(claude_md_path, repo)],
        "identity": {key: identity[key] for key in IDENTITY_ORDER},
        "languages": languages,
        "education": education,
        "experience": experience,
        "years_experience": {
            "total": years_of_experience(experience, today, warnings),
            "method": YEARS_METHOD,
            "as_of": today.isoformat(),
        },
        "screening_answers": screening,
        "unanswered": unanswered,
        "warnings": warnings,
    }


def placeholder_fields(profile):
    """Dotted paths of `[PLACEHOLDER]`-shaped values outside screening answers."""
    found = []
    for key, value in profile["identity"].items():
        if is_placeholder(value):
            found.append(f"identity.{key}")
    for name in ("languages", "education", "experience"):
        for index, entry in enumerate(profile[name]):
            for key, value in entry.items():
                if is_placeholder(value):
                    found.append(f"{name}[{index}].{key}")
    return found


def blank_identity_fields(profile):
    return [
        f"identity.{key}"
        for key in ("name", "email", "phone", "location")
        if not profile["identity"][key] or is_placeholder(profile["identity"][key])
    ]


def is_stale(out_path, sources):
    if not out_path.exists():
        return True
    out_mtime = out_path.stat().st_mtime
    return any(src.exists() and src.stat().st_mtime > out_mtime for src in sources)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_today(value):
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--today must be YYYY-MM-DD, got {value!r}") from exc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="export_profile.py",
        description="Export 01-candidate-profile.md (+ CLAUDE.md's CV language) to profile.json.",
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repo root the default paths hang off")
    parser.add_argument("--profile", type=Path, help=f"path to 01 (default <repo>/{PROFILE_REL})")
    parser.add_argument("--claude-md", type=Path, help=f"path to CLAUDE.md (default <repo>/{CLAUDE_MD_REL})")
    parser.add_argument("--out", type=Path, help=f"where to write profile.json (default <repo>/{OUT_REL})")
    parser.add_argument(
        "--check",
        action="store_true",
        help='do not write; print {"ok","missing","stale",...} and exit 1 unless ok',
    )
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="export even when [PLACEHOLDER]-shaped values remain (they are listed in warnings)",
    )
    parser.add_argument("--today", type=_parse_today, default=None, help="YYYY-MM-DD (default: today)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = args.repo
    profile_path = args.profile or repo / PROFILE_REL
    claude_md_path = args.claude_md or repo / CLAUDE_MD_REL
    out_path = args.out or repo / OUT_REL
    today = args.today or _dt.date.today()

    try:
        profile = build_profile(profile_path, claude_md_path, repo, today)
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    placeholders = placeholder_fields(profile)

    if args.check:
        stale = is_stale(out_path, [profile_path, claude_md_path])
        missing = blank_identity_fields(profile) + [
            f"screening_answers.{key}" for key in profile["unanswered"]
        ]
        ok = not stale and not placeholders
        report = {
            "ok": ok,
            "missing": missing,
            "stale": stale,
            "placeholders": placeholders,
            "unanswered": profile["unanswered"],
            "out": str(out_path),
            "warnings": profile["warnings"],
        }
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if ok else 1

    if placeholders:
        if not args.allow_placeholders:
            print(
                "error: [PLACEHOLDER]-shaped values remain (run /setup, or pass "
                "--allow-placeholders): " + ", ".join(placeholders),
                file=sys.stderr,
            )
            return 1
        profile["warnings"].append("placeholders exported as-is: " + ", ".join(placeholders))

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"error: cannot write {out_path}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "ok": True,
        "out": str(out_path),
        "generated": profile["generated"],
        "years_experience": profile["years_experience"]["total"],
        "experience": len(profile["experience"]),
        "screening_answers": len(profile["screening_answers"]),
        "unanswered": profile["unanswered"],
        "warnings": profile["warnings"],
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
