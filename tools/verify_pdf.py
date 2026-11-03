#!/usr/bin/env python3
"""Verify that a generated PDF has the expected pages and extractable text.

Text-layer extraction tries pypdf (BSD, optional `pip install pypdf`) first,
then Poppler `pdftotext` if pypdf is missing, raises, or returns zero
extractable characters. Poppler remains the fallback.

`extract_pages(pdf_path)` exposes the same extraction one string per page for
tools that need page positions (tools/check_cv.py uses it to spot orphaned
entry titles).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


class VerificationError(Exception):
    """Raised when a generated PDF does not satisfy its checks."""


def run_tool(command):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except FileNotFoundError as exc:
        raise VerificationError(
            f"required command '{command[0]}' was not found. "
            "Install pypdf (`pip install pypdf`) or poppler-utils "
            "(macOS: brew install poppler, Debian/Ubuntu: apt install poppler-utils, "
            "Windows: choco install poppler)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        detail = detail or "command failed"
        raise VerificationError(f"{command[0]} could not read the PDF: {detail}") from exc


def parse_page_count(pdfinfo_output):
    match = re.search(r"^Pages:\s+(\d+)\s*$", pdfinfo_output, re.MULTILINE)
    if not match:
        raise VerificationError("pdfinfo output did not contain a page count")
    return int(match.group(1))


def normalize_text(text):
    return " ".join(text.split())


def _extract_pypdf_pages(pdf_path):
    """Return one text string per page, or None if pypdf is unavailable,
    raises, or yields no text at all (so the caller falls back to Poppler)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception:
        return None
    # Harden: treat empty/degraded extraction as failure so we fall back
    if len(normalize_text("\n".join(pages))) == 0:
        return None
    return pages


def _extract_pypdf(pdf_path):
    """Return (text, pages) or None if pypdf is unavailable, raises, or yields no text."""
    pages = _extract_pypdf_pages(pdf_path)
    if pages is None:
        return None
    return "\n".join(pages), len(pages)


def _extract_pdftotext(pdf_path):
    text = run_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"])
    # Always call pdfinfo here so the fallback path returns a page count
    # even when the caller did not request --pages (same Poppler package).
    pages = parse_page_count(run_tool(["pdfinfo", str(pdf_path)]))
    return text, pages


def extract_text_layer(pdf_path):
    """Extract ATS-readable text. Returns (text, pages, extractor_name)."""
    pypdf_result = _extract_pypdf(pdf_path)
    if pypdf_result is not None:
        text, pages = pypdf_result
        return text, pages, "pypdf"
    text, pages = _extract_pdftotext(pdf_path)
    return text, pages, "pdftotext"


def _split_pdftotext_pages(output):
    """Split `pdftotext` output on its form-feed page separators.

    Poppler ends every page, including the last, with a form feed, so a
    trailing empty element is dropped. Any other empty element is a genuinely
    blank page and is kept so page indexes stay aligned with the PDF.
    """
    pages = output.split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def extract_pages(pdf_path):
    """Extract the text layer page by page. Returns (pages, extractor_name).

    `pages` is one string per PDF page: pypdf's `page.extract_text()` when
    pypdf is available and yields text, otherwise `pdftotext -layout -enc UTF-8`
    output split on form feeds. Raises VerificationError when no extractor can
    read the PDF (same install hint as `run_tool`).
    """
    pages = _extract_pypdf_pages(pdf_path)
    if pages is not None:
        return pages, "pypdf"
    output = run_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"])
    return _split_pdftotext_pages(output), "pdftotext"


def verify_pdf(pdf_path, expected_pages=None, min_chars=1, required_text=(), dump_text=None):
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise VerificationError(f"PDF does not exist: {pdf_path}")

    extracted_text, actual_pages, extractor = extract_text_layer(pdf_path)

    # Write dump *before* the checks so a failed verification still leaves a .txt
    if dump_text is not None:
        dump_path = Path(dump_text)
        try:
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text(
                extracted_text if extracted_text.endswith("\n") else extracted_text + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise VerificationError(
                f"could not write --dump-text to {dump_path}: {exc}"
            ) from exc

    if expected_pages is not None and actual_pages != expected_pages:
        raise VerificationError(
            f"expected {expected_pages} page(s), found {actual_pages} (extractor: {extractor})"
        )

    normalized = normalize_text(extracted_text)
    if len(normalized) < min_chars:
        raise VerificationError(
            f"text layer has {len(normalized)} character(s); expected at least {min_chars} "
            f"(extractor: {extractor})"
        )

    for required in required_text:
        if normalize_text(required) not in normalized:
            raise VerificationError(
                f"text layer is missing required text: {required!r} (extractor: {extractor})"
            )
    return extractor, extracted_text, actual_pages


def build_parser():
    parser = argparse.ArgumentParser(
        description="Verify a PDF's page count and ATS-readable text layer."
    )
    parser.add_argument("pdf", type=Path, help="PDF file to verify")
    parser.add_argument("--pages", type=int, help="required exact page count")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=1,
        help="minimum non-whitespace text-layer characters (default: 1)",
    )
    parser.add_argument(
        "--contains",
        action="append",
        default=[],
        help="text that must appear after whitespace normalization; repeatable",
    )
    parser.add_argument(
        "--dump-text",
        type=Path,
        help="write the extracted text layer to this path (UTF-8)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        extractor, text, pages = verify_pdf(
            args.pdf,
            args.pages,
            args.min_chars,
            args.contains,
            dump_text=args.dump_text,
        )
    except VerificationError as exc:
        print(f"Error: {args.pdf}: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {args.pdf} (extractor: {extractor}, pages: {pages})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
