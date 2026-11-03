#!/usr/bin/env python3
"""Base CVs per role family for /quick-apply (cv/base_<family>.tex).

A base CV is the master CV already tailored to a *role family* (see "Base CVs
for the quick tier" in 05-cv-templates.md): its profile statement ends with
the literal token `[EMPLOYER]`, and `stamp` turns it into the per-job file
`cv/main_<company>_<role>.tex` by copying it and replacing that token. The
model then applies /quick-apply's bounded edits to the copy; the base itself
is never sent and never edited per job.

Subcommands:
    list   - every cv/base_<family>.tex with whether a compiled PDF sits beside it
    slug   - the `<company>_<role>` filename stem, derived by the Subfolder
             naming rule in documents/README.md (lowercase; every character
             that is not a letter, digit or underscore dropped; runs of
             underscores collapsed; leading/trailing underscores trimmed)
    stamp  - copy cv/base_<family><ext> to cv/main_<slug><ext>, replacing
             every `[EMPLOYER]` token with the employer's name

Output is one compact JSON value on stdout; human-readable errors go to
stderr. Exit 0 on success, 1 on a data error (missing family file, no
`[EMPLOYER]` token, target exists without --force, empty slug), 2 on a usage
error.

Usage examples:
    python tools/base_cv.py list
    python tools/base_cv.py slug --company "Novo Nordisk A/S" --role "Data Scientist"
    python tools/base_cv.py stamp --family data_science --company Acme --role "ML Engineer" --employer "Acme Corp"
    python tools/base_cv.py stamp --family data_science --company Acme --role "ML Engineer" --employer "Acme Corp" --force
    python tools/base_cv.py --cv-dir /tmp/cv list
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CV_DIR = REPO_ROOT / "cv"

EMPLOYER_TOKEN = "[EMPLOYER]"
BASE_PREFIX = "base_"
MAIN_PREFIX = "main_"
DEFAULT_EXT = ".tex"

# LaTeX special characters an employer name may carry (05-cv-templates.md,
# "LaTeX Special Characters"). `\` itself is left alone so an already
# escaped `\&` passes through unchanged.
_LATEX_SPECIALS = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


class BaseCvError(Exception):
    """A data error the caller should fix (exit 1)."""


def slugify(company, role):
    """The Subfolder naming rule from documents/README.md, executed exactly as
    documented: `<company>_<role>` lowercased, spaces to underscores, every
    character that is not a letter, digit or underscore dropped, runs of
    underscores collapsed, leading and trailing underscores trimmed. Returns
    "" when nothing survives (the caller must stop, never write a file).
    `\\w` is Unicode-aware in Python 3, so Danish letters survive."""
    name = f"{company}_{role}".lower().replace(" ", "_")
    name = re.sub(r"[^\w]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def family_slug(role_type):
    """A role family name: the role type lowercased, every run of
    non-alphanumeric characters replaced by `_`, ends trimmed. The same
    normalisation is applied to `--family` so `"Data Science / ML"` and
    `data_science_ml` name the same base file and no path separator can
    reach the filesystem. Alphanumeric is Unicode-aware, like `slugify`
    and the family names `list` reads back from `cv/base_<family>.tex`,
    so `Økonomi` names `base_økonomi.tex` rather than `base_konomi.tex`."""
    name = re.sub(r"[^\w]+", "_", role_type.lower())
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def latex_escape(text):
    """Escape LaTeX specials in an employer name, leaving already escaped
    sequences (`\\&`) untouched."""
    out = []
    for i, ch in enumerate(text):
        if ch in _LATEX_SPECIALS and not (i > 0 and text[i - 1] == "\\"):
            out.append(_LATEX_SPECIALS[ch])
        else:
            out.append(ch)
    return "".join(out)


def _normalise_ext(ext):
    ext = ext or DEFAULT_EXT
    if not ext.startswith("."):
        ext = "." + ext
    if "/" in ext or "\\" in ext:
        raise BaseCvError(f"--ext must be a file extension, got {ext!r}")
    return ext


def list_bases(cv_dir):
    """Every base CV in `cv_dir`, sorted by family."""
    cv_dir = Path(cv_dir)
    entries = []
    if not cv_dir.is_dir():
        return entries
    for path in sorted(cv_dir.glob(f"{BASE_PREFIX}*{DEFAULT_EXT}")):
        if not path.is_file():
            continue
        family = path.name[len(BASE_PREFIX):-len(DEFAULT_EXT)]
        if not family:
            continue
        pdf = path.with_suffix(".pdf")
        entries.append(
            {"family": family, "path": str(path), "compiled_pdf": pdf.is_file()}
        )
    return entries


def stamp(cv_dir, family, company, role, employer, ext=DEFAULT_EXT, force=False, escape=True):
    """Copy cv/base_<family><ext> to cv/main_<slug><ext> replacing every
    `[EMPLOYER]` token. Returns the JSON-able result dict."""
    cv_dir = Path(cv_dir)
    ext = _normalise_ext(ext)
    normalised_family = family_slug(family)
    if not normalised_family:
        raise BaseCvError(
            f"--family {family!r} contains no letter or digit; a family is a role "
            "type lowercased with non-alphanumeric runs replaced by '_'"
        )
    base = cv_dir / f"{BASE_PREFIX}{normalised_family}{ext}"
    if not base.is_file():
        known = ", ".join(e["family"] for e in list_bases(cv_dir)) or "none"
        raise BaseCvError(
            f"no base CV for family {normalised_family!r}: {base} does not exist "
            f"(known families: {known}; build one with `/quick-apply init`)"
        )
    slug = slugify(company, role)
    if not slug:
        raise BaseCvError(
            f"company {company!r} and role {role!r} derive an empty filename under "
            "the Subfolder naming rule in documents/README.md; supply a company or "
            "role containing at least one letter or digit"
        )
    employer = employer.strip()
    if not employer:
        raise BaseCvError("--employer must not be empty")
    try:
        text = base.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BaseCvError(
            f"{base} is not UTF-8 text ({exc.reason} at byte {exc.start}); a base "
            "CV is a UTF-8 .tex file (05-cv-templates.md, 'Base CVs for the quick tier')"
        ) from None
    count = text.count(EMPLOYER_TOKEN)
    if count == 0:
        raise BaseCvError(
            f"{base} contains no {EMPLOYER_TOKEN} token: a base CV must end its "
            f"profile statement with the literal token {EMPLOYER_TOKEN} so the "
            "stamped copy can name the employer (05-cv-templates.md, 'Base CVs "
            "for the quick tier')"
        )
    target = cv_dir / f"{MAIN_PREFIX}{slug}{ext}"
    if target.exists() and not force:
        raise BaseCvError(
            f"{target} already exists; pass --force to overwrite it (a per-job CV "
            "the model has already edited would be lost)"
        )
    replacement = latex_escape(employer) if escape else employer
    stamped = text.replace(EMPLOYER_TOKEN, replacement)
    cv_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(stamped, encoding="utf-8")
    return {
        "cv_file": str(target),
        "family": normalised_family,
        "slug": slug,
        "replaced": count,
    }


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def cmd_list(args):
    emit(list_bases(args.cv_dir))
    return 0


def cmd_slug(args):
    slug = slugify(args.company, args.role)
    if not slug:
        raise BaseCvError(
            f"company {args.company!r} and role {args.role!r} derive an empty "
            "filename under the Subfolder naming rule in documents/README.md; "
            "supply a company or role containing at least one letter or digit"
        )
    emit({"slug": slug})
    return 0


def cmd_stamp(args):
    result = stamp(
        args.cv_dir,
        args.family,
        args.company,
        args.role,
        args.employer,
        ext=args.ext,
        force=args.force,
        escape=not args.raw,
    )
    emit(result)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="base_cv.py",
        description="List, name and stamp base CVs per role family (cv/base_<family>.tex).",
    )
    parser.add_argument(
        "--cv-dir",
        default=str(DEFAULT_CV_DIR),
        help=f"directory holding base_*.tex and main_*.tex (default: {DEFAULT_CV_DIR})",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("list", help="list base CVs and whether each has a compiled PDF")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "slug",
        help="derive the <company>_<role> filename stem (Subfolder naming rule)",
    )
    p.add_argument("--company", required=True)
    p.add_argument("--role", required=True)
    p.set_defaults(func=cmd_slug)

    p = sub.add_parser(
        "stamp",
        help="copy cv/base_<family><ext> to cv/main_<slug><ext>, replacing [EMPLOYER]",
    )
    p.add_argument("--family", required=True, help="role family (cv/base_<family>.tex)")
    p.add_argument("--company", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--employer", required=True, help="text that replaces every [EMPLOYER] token")
    p.add_argument("--ext", default=DEFAULT_EXT, help=f"file extension to copy (default: {DEFAULT_EXT})")
    p.add_argument("--force", action="store_true", help="overwrite an existing target file")
    p.add_argument(
        "--raw",
        action="store_true",
        help="insert --employer verbatim instead of escaping LaTeX specials (& %% $ # _ ~ ^)",
    )
    p.set_defaults(func=cmd_stamp)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BaseCvError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
