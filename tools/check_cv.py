#!/usr/bin/env python3
"""Mechanical layout and ATS checks for a compiled CV or cover letter PDF.

Replaces the hand-run parts of /apply Step 5 (page count, text-layer
parseability, contact details as literal text, ASCII-hyphen date ranges,
orphaned `\\cventry` titles, cover-letter fonts, keyword coverage) with one
deterministic pass. Text extraction comes from tools/verify_pdf.extract_pages
(pypdf first, Poppler `pdftotext -layout -enc UTF-8` as the fallback).

The tool prints ONE JSON object on stdout and exits 0 when every check passed,
1 when a check failed (or the PDF could not be read), 2 on a usage error.
`needs_visual_review` is advisory: it asks the model to Read the PDF because
the mechanical pass could not settle something (unparsed source, an orphan
suspect, a skipped font check); it never fails the run by itself.

Usage examples:

    python tools/check_cv.py cv --pdf cv/main_acme_ds.pdf --source cv/main_acme_ds.tex
    python tools/check_cv.py cv --pdf cv/main_acme_ds.pdf --source cv/main_acme_ds.tex \\
        --keywords /tmp/keywords.json --expect-pages 2
    python tools/check_cv.py cover --pdf cover_letters/cover_acme_ds.pdf \\
        --source cover_letters/cover_acme_ds.tex --allowed-font Raleway \\
        --contains "Kind regards"

The --keywords file is a JSON array of
{"term": "Python", "priority": "required"|"nice", "synonyms": ["Python 3"]}.
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    from tools.verify_pdf import VerificationError, extract_pages, normalize_text
except ImportError:  # run as `python tools/check_cv.py`: put the repo root on the path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.verify_pdf import VerificationError, extract_pages, normalize_text

EXTRACTOR_HINT = (
    "No PDF text extractor is available. Install pypdf (`pip install pypdf`) "
    "or poppler-utils (macOS: brew install poppler, Debian/Ubuntu: apt install "
    "poppler-utils, Windows: choco install poppler)"
)

# LaTeX commands whose argument is layout-only and must not reach the text.
_DROP_ARG_COMMANDS = (
    "vspace", "hspace", "vskip", "needspace", "enlargethispage", "label",
    "phantom", "hphantom", "vphantom", "fontsize", "fontspec", "color",
    "textcolor", "pagestyle", "thispagestyle", "usepackage", "documentclass",
)


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

def loose_normalize(text):
    """Lowercase, fold ligatures/compatibility forms, keep only word characters.

    Used to locate titles and body phrases on a page regardless of quote
    style, dash rendering or extractor spacing.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


def keyword_normalize(text):
    """Lowercase, collapse whitespace, drop spaces adjacent to punctuation.

    "CI / CD" == "CI/CD" and "Node . js" == "Node.js" after this, which is
    the spacing drift seen between pypdf and Poppler.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = " ".join(text.split())
    text = re.sub(r"\s*([^\w\s])\s*", r"\1", text)
    return text


def nospace(text):
    return re.sub(r"\s+", "", text or "")


def digits_only(text):
    return re.sub(r"\D", "", text or "")


# ---------------------------------------------------------------------------
# LaTeX source parsing
# ---------------------------------------------------------------------------

def strip_comments(source):
    """Remove `%` comments (not `\\%`) up to end of line, keeping the newline."""
    return re.sub(r"(?<!\\)%[^\n]*", "", source)


def read_brace_group(source, pos):
    """Read a `{...}` group starting at source[pos] == '{'.

    Returns (inner_text, end_pos) where end_pos is the index after the closing
    brace, or None if the braces do not balance. Backslash escapes the next
    character, so `\\{`, `\\}` and `\\\\` never count as group delimiters.
    """
    if pos >= len(source) or source[pos] != "{":
        return None
    depth = 0
    i = pos
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[pos + 1:i], i + 1
        i += 1
    return None


def read_command_args(source, pos, nargs):
    """Read `nargs` brace arguments following a command at `pos`.

    Skips whitespace, an optional `*` and `[...]` optional arguments. Returns
    (args, end_pos) or None when fewer than nargs groups follow.
    """
    i = pos
    if i < len(source) and source[i] == "*":
        i += 1
    args = []
    while len(args) < nargs:
        while i < len(source) and source[i] in " \t\r\n":
            i += 1
        if i < len(source) and source[i] == "[":
            end = source.find("]", i)
            if end == -1:
                return None
            i = end + 1
            continue
        group = read_brace_group(source, i)
        if group is None:
            return None
        inner, i = group
        args.append(inner)
    return args, i


def find_commands(source, name, nargs):
    """Return the argument lists of every `\\name{...}...` occurrence."""
    found = []
    for match in re.finditer(r"\\" + re.escape(name) + r"(?![A-Za-z@])", source):
        parsed = read_command_args(source, match.end(), nargs)
        if parsed is not None:
            found.append(parsed[0])
    return found


def strip_latex(text):
    """Reduce LaTeX markup to its visible text (best effort, no macro expansion)."""
    text = strip_comments(text or "")
    # Protect escaped specials before the brace/command passes.
    protect = {
        "\\{": "\x00LB\x00", "\\}": "\x00RB\x00", "\\%": "%", "\\&": "&",
        "\\$": "$", "\\#": "#", "\\_": "_",
    }
    for src, dst in protect.items():
        text = text.replace(src, dst)
    text = re.sub(r"\\(begin|end)\s*\{[^}]*\}", " ", text)
    text = re.sub(
        r"\\(" + "|".join(_DROP_ARG_COMMANDS) + r")\*?\s*(\[[^\]]*\])?\s*\{[^}]*\}",
        " ",
        text,
    )
    text = re.sub(r"\\href\s*\{[^}]*\}", "", text)  # keep the link text group
    text = re.sub(r"\\item\s*(\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\\\\*?(\[[^\]]*\])?|\\newline|\\linebreak", " ", text)
    text = text.replace("~", " ")
    text = re.sub(r"\\[A-Za-z@]+\*?\s*(\[[^\]]*\])?\s*", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")
    text = text.replace("``", '"').replace("''", '"')
    return " ".join(text.split())


def parse_cv_source(source):
    """Return (entries, sections) in document order.

    entries: [{"date", "title", "employer", "body", "section"}] from each
    6-argument `\\cventry`; sections: [heading, ...] from `\\section{...}`.
    """
    text = strip_comments(source)
    entries = []
    sections = []
    current_section = None
    for match in re.finditer(r"\\(section|cventry)(?![A-Za-z@])", text):
        name = match.group(1)
        if name == "section":
            parsed = read_command_args(text, match.end(), 1)
            if parsed is None:
                continue
            heading = strip_latex(parsed[0][0])
            sections.append(heading)
            current_section = heading
        else:
            parsed = read_command_args(text, match.end(), 6)
            if parsed is None:
                continue
            args = parsed[0]
            entries.append(
                {
                    "date": strip_latex(args[0]),
                    "date_raw": " ".join(args[0].split()),
                    "title": strip_latex(args[1]),
                    "employer": strip_latex(args[2]),
                    "body": strip_latex(args[5]),
                    "section": current_section,
                }
            )
    return entries, sections


def parse_contact(source):
    """Return {"email": str|None, "phone": str|None} from moderncv preamble macros."""
    text = strip_comments(source)
    email = None
    phone = None
    emails = find_commands(text, "email", 1)
    if emails:
        email = strip_latex(emails[0][0]) or None
    for macro in ("phone", "mobile"):
        phones = find_commands(text, macro, 1)
        if phones:
            phone = strip_latex(phones[0][0]) or None
            break
    return {"email": email, "phone": phone}


def body_lead_phrase(body, letter_words=4):
    """First tokens of the body through the 4th word that contains a letter."""
    tokens = []
    seen = 0
    for token in (body or "").split():
        tokens.append(token)
        if re.search(r"[^\W\d_]", token):
            seen += 1
            if seen >= letter_words:
                break
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_page_count(pages, expected):
    actual = len(pages)
    if not expected:
        return {"ok": True, "expected": None, "actual": actual}
    return {"ok": actual == expected, "expected": expected, "actual": actual}


def check_text_layer(pages):
    text = "\n".join(pages)
    chars = len(normalize_text(text))
    cid = text.count("(cid:")
    replacement = text.count("\ufffd")
    return {
        "ok": chars > 0 and cid == 0 and replacement == 0,
        "chars": chars,
        "cid_markers": cid,
        "replacement_chars": replacement,
    }


def _literal_present(value, text):
    """How `value` appears in `text`: 'exact', 'nospace', 'digits' or None."""
    if not value:
        return None
    norm_value = normalize_text(value)
    norm_text = normalize_text(text)
    if norm_value and norm_value in norm_text:
        return "exact"
    if nospace(norm_value) and nospace(norm_value) in nospace(norm_text):
        return "nospace"
    return None


def check_contact(pages, email, phone):
    text = "\n".join(pages)
    result = {"ok": True}
    for label, value in (("email", email), ("phone", phone)):
        if not value:
            result[label] = {"value": None, "found": False, "matched": None,
                             "note": "no value given and none parsed from source"}
            result["ok"] = False
            continue
        matched = _literal_present(value, text)
        if matched is None and label == "phone":
            digits = digits_only(value)
            if len(digits) >= 6 and digits in digits_only(text):
                matched = "digits"
        result[label] = {"value": value, "found": matched is not None, "matched": matched}
        if matched is None:
            result["ok"] = False
    return result


def check_dates(pages, entries):
    text = "\n".join(pages)
    norm_text = normalize_text(text)
    squeezed_text = nospace(norm_text)
    items = []
    ok = True
    for entry in entries:
        raw = entry["date_raw"]
        shown = entry["date"]
        double_hyphen = "--" in raw
        unicode_dash = re.search(r"[\u2010-\u2015\u2212]", shown)
        is_range = bool(re.search(r"\S\s*-\s*\S", shown)) and not double_hyphen
        found_ascii = False
        if is_range:
            found_ascii = (normalize_text(shown) in norm_text) or (
                nospace(shown) in squeezed_text
            )
        rendered = shown.replace("---", "\u2014").replace("--", "\u2013")
        found_rendered = (normalize_text(rendered) in norm_text) or (
            nospace(rendered) in squeezed_text
        )
        item = {
            "title": entry["title"],
            "section": entry["section"],
            "date": raw,
            "double_hyphen": double_hyphen,
            "ascii_range": is_range,
            "found_as_ascii_range": found_ascii,
            "found_in_text": found_rendered or found_ascii,
        }
        if double_hyphen:
            item["problem"] = "'--' renders as an en-dash (U+2013); write a single ASCII hyphen"
        elif unicode_dash and not is_range:
            item["problem"] = (
                f"contains a non-ASCII dash (U+{ord(unicode_dash.group(0)):04X}); "
                "write a single ASCII hyphen"
            )
        elif not is_range:
            item["problem"] = "not a start-end range separated by an ASCII hyphen (bare year gives the parser no end date)"
        elif not found_ascii:
            item["problem"] = "range not found in the text layer as ASCII-hyphen text"
        if "problem" in item:
            ok = False
        items.append(item)
    return {"ok": ok, "entries": items}


def _pages_containing(loose_pages, needle):
    """Indexes of pages holding `needle` (already loose-normalized).

    pypdf sometimes drops the spaces inside a bold run, so a title that is
    missing as spaced text is retried with every space removed on both sides.
    """
    if not needle:
        return []
    found = [i for i, page in enumerate(loose_pages) if needle in page]
    if found:
        return found
    squeezed = nospace(needle)
    return [i for i, page in enumerate(loose_pages) if squeezed in nospace(page)]


def check_orphans(pages, entries, sections):
    loose_pages = [loose_normalize(page) for page in pages]
    suspects = []
    unlocated = []
    located = []
    for entry in entries:
        title = loose_normalize(entry["title"])
        phrase = loose_normalize(body_lead_phrase(entry["body"]))
        title_pages = _pages_containing(loose_pages, title)
        body_pages = _pages_containing(loose_pages, phrase)
        if not title_pages:
            unlocated.append({"title": entry["title"], "reason": "title not found on any page"})
            continue
        if not body_pages:
            unlocated.append({"title": entry["title"], "reason": "body not found on any page",
                              "body_phrase": body_lead_phrase(entry["body"])})
            continue
        shared = sorted(set(title_pages) & set(body_pages))
        if shared:
            located.append({"title": entry["title"], "page": shared[0]})
        else:
            suspects.append({
                "title": entry["title"],
                "title_page": title_pages[0],
                "body_page": body_pages[0],
            })
    for index, page in enumerate(pages):
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        if not lines:
            continue
        last = loose_normalize(lines[-1])
        for heading in sections:
            if last and last == loose_normalize(heading):
                suspects.append({"section": heading, "page": index,
                                 "reason": "section heading is the last text on the page"})
    return {
        "ok": not suspects,
        "suspects": suspects,
        "unlocated": unlocated,
        "located": located,
        "entries_parsed": len(entries),
        "sections_parsed": len(sections),
    }


def _resolve(obj):
    getter = getattr(obj, "get_object", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return obj


def _collect_fonts_from_resources(resources, names, seen, depth=0):
    resources = _resolve(resources)
    if not isinstance(resources, dict) or depth > 4:
        return
    fonts = _resolve(resources.get("/Font"))
    if isinstance(fonts, dict):
        for value in fonts.values():
            font = _resolve(value)
            if isinstance(font, dict):
                base = _resolve(font.get("/BaseFont"))
                if base is not None:
                    names.add(str(base).lstrip("/"))
    xobjects = _resolve(resources.get("/XObject"))
    if isinstance(xobjects, dict):
        for value in xobjects.values():
            key = getattr(value, "idnum", None)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            xobject = _resolve(value)
            if isinstance(xobject, dict):
                _collect_fonts_from_resources(xobject.get("/Resources"), names, seen, depth + 1)


def collect_fonts(reader):
    """Return the sorted /BaseFont names used on every page of `reader`."""
    names = set()
    seen = set()
    for page in reader.pages:
        page = _resolve(page)
        if isinstance(page, dict):
            _collect_fonts_from_resources(page.get("/Resources"), names, seen)
    return sorted(names)


def _open_reader(pdf_path):
    """Return a pypdf PdfReader or None when pypdf is unavailable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return PdfReader(str(pdf_path))


def check_fonts(pdf_path, allowed):
    try:
        reader = _open_reader(pdf_path)
    except Exception as exc:  # unreadable PDF: report, do not crash the run
        return {"ok": False, "fonts": [], "error": f"pypdf could not open the PDF: {exc}"}
    if reader is None:
        return {"ok": True, "skipped": "pypdf not installed", "fonts": []}
    fonts = collect_fonts(reader)
    result = {"ok": True, "fonts": fonts, "allowed": list(allowed or [])}
    if allowed:
        lowered = [a.lower() for a in allowed]
        disallowed = [f for f in fonts if not any(a in f.lower() for a in lowered)]
        result["disallowed"] = disallowed
        result["ok"] = not disallowed and bool(fonts)
        if not fonts:
            result["error"] = "no fonts found in the PDF"
    return result


def _term_found(term, norm_text):
    norm_term = keyword_normalize(term)
    if not norm_term:
        return False
    # Word boundaries only where the term itself starts/ends with a word
    # character: the normalization glues "C++ code" into "c++code", so a
    # trailing boundary after "+" would hide a genuine match.
    prefix = r"(?<!\w)" if re.match(r"\w", norm_term) else ""
    suffix = r"(?!\w)" if re.search(r"\w$", norm_term) else ""
    pattern = prefix + re.escape(norm_term) + suffix
    return re.search(pattern, norm_text) is not None


def check_keywords(pages, keywords):
    norm_text = keyword_normalize("\n".join(pages))
    covered, synonym_only, missing, required_missing = [], [], [], []
    for item in keywords:
        term = item.get("term", "")
        priority = str(item.get("priority", "") or "")
        synonyms = item.get("synonyms", []) or []
        if _term_found(term, norm_text):
            covered.append(term)
            continue
        matched = [s for s in synonyms if _term_found(s, norm_text)]
        if matched:
            synonym_only.append({"term": term, "matched": matched, "priority": priority})
            continue
        missing.append({"term": term, "priority": priority})
        if priority.lower() == "required":
            required_missing.append(term)
    return {
        "ok": True,
        "covered": covered,
        "synonym_only": synonym_only,
        "missing": missing,
        "required_missing": required_missing,
    }


def check_contains(pages, required, signature_terms):
    norm_text = normalize_text("\n".join(pages))
    missing = [r for r in required if normalize_text(r) not in norm_text]
    sig_missing = [s for s in signature_terms if normalize_text(s) not in norm_text]
    return {
        "ok": not missing and not sig_missing,
        "required": list(required),
        "missing": missing,
        "signature": {"terms": list(signature_terms), "missing": sig_missing},
    }


def load_keywords(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("keywords file must be a JSON array of objects")
    cleaned = []
    for item in data:
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict):
            raise ValueError("every keyword entry must be an object with a 'term'")
        term = item.get("term")
        if not isinstance(term, str) or not term.strip():
            raise ValueError("every keyword entry needs a non-empty string 'term'")
        synonyms = item.get("synonyms") or []
        if isinstance(synonyms, str):
            synonyms = [synonyms]
        if not isinstance(synonyms, list) or not all(isinstance(x, str) for x in synonyms):
            raise ValueError(f"keyword {term!r}: 'synonyms' must be a list of strings")
        priority = item.get("priority")
        cleaned.append({
            "term": term.strip(),
            "priority": "" if priority is None else str(priority),
            "synonyms": [x.strip() for x in synonyms if x.strip()],
        })
    return cleaned


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _skipped(reason):
    return {"ok": True, "skipped": reason}


def _read_source(path):
    """Return (text, note). note explains why the source is unusable."""
    if path is None:
        return None, "no --source given"
    path = Path(path)
    if path.suffix.lower() != ".tex":
        return None, f"source is not a .tex file: {path}"
    if not path.is_file():
        return None, f"source does not exist: {path}"
    return path.read_text(encoding="utf-8", errors="replace"), None


def run_cv(pdf, source, expect_pages, keywords_path, email, phone):
    pages, extractor = extract_pages(pdf)
    checks = {}
    problems = []
    needs_visual = False

    source_text, source_note = _read_source(source)
    if source_text is None:
        needs_visual = True
        entries, sections = [], []
        contact = {"email": None, "phone": None}
    else:
        entries, sections = parse_cv_source(source_text)
        contact = parse_contact(source_text)
        if not entries:
            needs_visual = True

    checks["page_count"] = check_page_count(pages, expect_pages)
    if not checks["page_count"]["ok"]:
        problems.append(
            f"page_count: expected {expect_pages} page(s), found {len(pages)}"
        )

    checks["text_layer"] = check_text_layer(pages)
    tl = checks["text_layer"]
    if not tl["ok"]:
        problems.append(
            f"text_layer: {tl['chars']} chars, {tl['cid_markers']} '(cid:' markers, "
            f"{tl['replacement_chars']} U+FFFD replacement characters"
        )

    checks["contact"] = check_contact(pages, email or contact["email"], phone or contact["phone"])
    for label in ("email", "phone"):
        detail = checks["contact"][label]
        if not detail["found"]:
            if detail["value"] is None:
                problems.append(f"contact: no {label} given (--{label}) or parsed from the source")
            else:
                problems.append(f"contact: {label} {detail['value']!r} not found as literal text")

    if source_text is None:
        checks["dates"] = _skipped(source_note)
        checks["orphans"] = _skipped(source_note)
    else:
        checks["dates"] = check_dates(pages, entries)
        for item in checks["dates"]["entries"]:
            if "problem" in item:
                problems.append(
                    f"dates: entry {item['title']!r} date {item['date']!r}: {item['problem']}"
                )
        checks["orphans"] = check_orphans(pages, entries, sections)
        for suspect in checks["orphans"]["suspects"]:
            if "section" in suspect:
                problems.append(
                    f"orphans: section heading {suspect['section']!r} is the last text "
                    f"on page {suspect['page'] + 1}"
                )
            else:
                problems.append(
                    f"orphans: title {suspect['title']!r} is on page "
                    f"{suspect['title_page'] + 1} but its body starts on page "
                    f"{suspect['body_page'] + 1}"
                )
        if checks["orphans"]["suspects"] or checks["orphans"]["unlocated"]:
            needs_visual = True
        if not entries:
            checks["orphans"]["note"] = "0 \\cventry entries parsed from the source"

    checks["fonts"] = _skipped("fonts are checked for cover letters only")

    if keywords_path:
        checks["keywords"] = check_keywords(pages, load_keywords(keywords_path))
    else:
        checks["keywords"] = _skipped("no --keywords file given")

    return {
        "ok": not problems,
        "needs_visual_review": needs_visual,
        "extractor": extractor,
        "pages": len(pages),
        "checks": checks,
        "problems": problems,
    }


def run_cover(pdf, source, expect_pages, contains, allowed_fonts):
    pages, extractor = extract_pages(pdf)
    checks = {}
    problems = []
    needs_visual = False

    signature_terms = []
    source_text, source_note = _read_source(source)
    if source is not None and source_text is None:
        needs_visual = True
    if source_text is not None:
        text = strip_comments(source_text)
        for macro in ("closing", "signature"):
            for args in find_commands(text, macro, 1):
                value = strip_latex(args[0])
                if value:
                    signature_terms.append(value)

    checks["page_count"] = check_page_count(pages, expect_pages)
    if not checks["page_count"]["ok"]:
        problems.append(
            f"page_count: expected {expect_pages} page(s), found {len(pages)}"
        )

    checks["text_layer"] = check_text_layer(pages)
    tl = checks["text_layer"]
    if not tl["ok"]:
        problems.append(
            f"text_layer: {tl['chars']} chars, {tl['cid_markers']} '(cid:' markers, "
            f"{tl['replacement_chars']} U+FFFD replacement characters"
        )

    checks["contact"] = _skipped("contact details are checked for CVs only")
    checks["dates"] = _skipped("dates are checked for CVs only")
    checks["orphans"] = _skipped("entry orphans are checked for CVs only")

    checks["fonts"] = check_fonts(pdf, allowed_fonts)
    if checks["fonts"].get("skipped"):
        needs_visual = True
    elif not checks["fonts"]["ok"]:
        if checks["fonts"].get("error"):
            problems.append(f"fonts: {checks['fonts']['error']}")
        else:
            problems.append(
                "fonts: not in the allowed families "
                f"{list(allowed_fonts)}: {checks['fonts']['disallowed']}"
            )

    checks["keywords"] = _skipped("keywords are checked for CVs only")

    checks["contains"] = check_contains(pages, contains, signature_terms)
    for missing in checks["contains"]["missing"]:
        problems.append(f"contains: required text {missing!r} not found")
    for missing in checks["contains"]["signature"]["missing"]:
        problems.append(
            f"contains: closing/signature text {missing!r} not found in the text layer"
        )

    return {
        "ok": not problems,
        "needs_visual_review": needs_visual,
        "extractor": extractor,
        "pages": len(pages),
        "checks": checks,
        "problems": problems,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Mechanical layout and ATS checks for a compiled CV or cover letter PDF."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cv = sub.add_parser("cv", help="check a CV PDF against its moderncv .tex source")
    cv.add_argument("--pdf", type=Path, required=True, help="compiled CV PDF")
    cv.add_argument("--source", type=Path, required=True, help="the CV .tex source")
    cv.add_argument("--expect-pages", type=int, default=2,
                    help="required exact page count (default: 2; 0 disables)")
    cv.add_argument("--keywords", type=Path,
                    help="JSON array of {term, priority, synonyms} to report coverage for")
    cv.add_argument("--email", help="email that must appear literally (default: parsed from source)")
    cv.add_argument("--phone", help="phone that must appear literally (default: parsed from source)")

    cover = sub.add_parser("cover", help="check a cover letter PDF")
    cover.add_argument("--pdf", type=Path, required=True, help="compiled cover letter PDF")
    cover.add_argument("--source", type=Path, help="the cover letter .tex source (optional)")
    cover.add_argument("--expect-pages", type=int, default=1,
                       help="required exact page count (default: 1; 0 disables)")
    cover.add_argument("--contains", action="append", default=[],
                       help="text that must appear after whitespace normalization; repeatable")
    cover.add_argument("--allowed-font", action="append", default=[],
                       help="font family every embedded font must belong to; repeatable")
    return parser


def _emit_error(message, hint=None):
    print(json.dumps({"ok": False, "error": message}))
    print(f"Error: {message}", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    return 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.expect_pages < 0:
        parser.error("--expect-pages must be 0 (disabled) or a positive page count")
    if not Path(args.pdf).is_file():
        return _emit_error(f"PDF does not exist: {args.pdf}")
    try:
        if args.command == "cv":
            result = run_cv(args.pdf, args.source, args.expect_pages, args.keywords,
                            args.email, args.phone)
        else:
            result = run_cover(args.pdf, args.source, args.expect_pages, args.contains,
                               args.allowed_font)
    except VerificationError as exc:
        message = str(exc)
        if "was not found" in message:
            return _emit_error(f"{args.pdf}: no extractor could read the PDF: {message}",
                               EXTRACTOR_HINT)
        return _emit_error(f"{args.pdf}: {message}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _emit_error(f"{exc}")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
