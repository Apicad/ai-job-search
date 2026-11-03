import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.verify_pdf import (
    VerificationError,
    extract_pages,
    extract_text_layer,
    parse_page_count,
    run_tool,
    verify_pdf,
)


def make_minimal_pdf(path, page_texts):
    """Write a tiny multi-page PDF (Helvetica, newline-separated lines per page).

    Hand-assembled with a valid xref so both pypdf and Poppler read it; used
    to exercise extract_pages on a real PDF without a LaTeX toolchain. Keep
    lines short: Poppler drops text drawn beyond the page's right edge.
    """
    objects = []  # (object number, bytes body)
    page_ids = []
    font_id = 3 + 2 * len(page_texts)
    for index, text in enumerate(page_texts):
        page_id = 3 + 2 * index
        content_id = page_id + 1
        page_ids.append(page_id)
        ops = []
        for line in text.split("\n"):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        stream = ("BT /F1 12 Tf 14 TL 72 720 Td " + " ".join(ops) + " ET").encode("latin-1")
        objects.append((page_id, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")))
        objects.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.insert(0, (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")))
    objects.insert(0, (1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objects.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_pos = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    Path(path).write_bytes(bytes(out))


class ParsePageCountTests(unittest.TestCase):
    def test_parses_pdfinfo_page_count(self):
        self.assertEqual(parse_page_count("Title: Example\nPages:          2\n"), 2)

    def test_rejects_output_without_page_count(self):
        with self.assertRaisesRegex(VerificationError, "did not contain a page count"):
            parse_page_count("Title: Example\n")


class VerifyPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pdf = Path(self.temp_dir.name) / "example.pdf"
        self.pdf.touch()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_accepts_expected_pages_and_text(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = [
            "Professional\nExperience   [your.email@example.com]\n",
            "Pages:          2\n",
        ]

        verify_pdf(
            self.pdf,
            expected_pages=2,
            min_chars=20,
            required_text=("Professional Experience", "[your.email@example.com]"),
        )

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_rejects_wrong_page_count(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = ["ok", "Pages:          3\n"]

        with self.assertRaisesRegex(VerificationError, "expected 2 page.*found 3"):
            verify_pdf(self.pdf, expected_pages=2)

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_rejects_too_little_extractable_text(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = ["short", "Pages:          1\n"]

        with self.assertRaisesRegex(VerificationError, "expected at least 20"):
            verify_pdf(self.pdf, min_chars=20)

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_rejects_missing_required_text(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = [
            "Readable text, but not the expected section.",
            "Pages:          1\n",
        ]

        with self.assertRaisesRegex(VerificationError, "Professional Experience"):
            verify_pdf(self.pdf, required_text=("Professional Experience",))

    def test_rejects_missing_pdf(self):
        with self.assertRaisesRegex(VerificationError, "PDF does not exist"):
            verify_pdf(Path(self.temp_dir.name) / "missing.pdf")

    @patch("tools.verify_pdf._extract_pypdf", return_value=("Hello ATS body", 1))
    def test_pypdf_is_preferred_over_poppler(self, _pypdf):
        text, pages, extractor = extract_text_layer(self.pdf)
        self.assertEqual(extractor, "pypdf")
        self.assertEqual(text, "Hello ATS body")
        self.assertEqual(pages, 1)

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_falls_back_to_pdftotext(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = ["poppler text", "Pages:          2\n"]
        text, pages, extractor = extract_text_layer(self.pdf)
        self.assertEqual(extractor, "pdftotext")
        self.assertEqual(text, "poppler text")
        self.assertEqual(pages, 2)
        self.assertEqual(mock_run_tool.call_args_list[0][0][0][:3], ["pdftotext", "-layout", "-enc"])


class ExtractPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pdf = Path(self.temp_dir.name) / "example.pdf"
        self.pdf.touch()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("tools.verify_pdf._extract_pypdf_pages", return_value=["page one", "page two"])
    def test_pypdf_pages_are_preferred(self, _pypdf):
        pages, extractor = extract_pages(self.pdf)
        self.assertEqual(extractor, "pypdf")
        self.assertEqual(pages, ["page one", "page two"])

    @patch("tools.verify_pdf._extract_pypdf_pages", return_value=["x", "y"])
    def test_joined_extraction_is_built_from_pages(self, _pypdf):
        text, pages, extractor = extract_text_layer(self.pdf)
        self.assertEqual((text, pages, extractor), ("x\ny", 2, "pypdf"))

    @patch("tools.verify_pdf._extract_pypdf_pages", return_value=None)
    @patch("tools.verify_pdf.run_tool", return_value="page one\fpage two\f")
    def test_falls_back_to_pdftotext_split_on_form_feed(self, mock_run_tool, _pypdf):
        pages, extractor = extract_pages(self.pdf)
        self.assertEqual(extractor, "pdftotext")
        self.assertEqual(pages, ["page one", "page two"])
        self.assertEqual(
            mock_run_tool.call_args_list[0][0][0][:4],
            ["pdftotext", "-layout", "-enc", "UTF-8"],
        )

    @patch("tools.verify_pdf._extract_pypdf_pages", return_value=None)
    @patch("tools.verify_pdf.run_tool", return_value="p1\f\fp3\f")
    def test_blank_middle_page_keeps_its_index(self, _run_tool, _pypdf):
        pages, _extractor = extract_pages(self.pdf)
        self.assertEqual(pages, ["p1", "", "p3"])

    @patch("tools.verify_pdf._extract_pypdf", return_value=None)
    @patch("tools.verify_pdf.run_tool")
    def test_legacy_pypdf_seam_still_drives_fallback(self, mock_run_tool, _pypdf):
        mock_run_tool.side_effect = ["poppler text", "Pages:          1\n"]
        text, pages, extractor = extract_text_layer(self.pdf)
        self.assertEqual((text, pages, extractor), ("poppler text", 1, "pdftotext"))

    @patch("tools.verify_pdf._extract_pypdf_pages", return_value=None)
    @patch("tools.verify_pdf.subprocess.run", side_effect=FileNotFoundError)
    def test_reports_missing_extractors(self, _run, _pypdf):
        with self.assertRaisesRegex(VerificationError, "pip install pypdf"):
            extract_pages(self.pdf)

    def test_real_pdf_round_trip(self):
        make_minimal_pdf(self.pdf, ["Alpha page", "Beta page"])
        try:
            pages, extractor = extract_pages(self.pdf)
        except VerificationError as exc:
            self.skipTest(f"no PDF extractor available: {exc}")
        self.assertIn(extractor, ("pypdf", "pdftotext"))
        self.assertEqual(len(pages), 2)
        self.assertIn("Alpha page", pages[0])
        self.assertIn("Beta page", pages[1])


class RunToolTests(unittest.TestCase):
    @patch("tools.verify_pdf.subprocess.run", side_effect=FileNotFoundError)
    def test_reports_missing_poppler_command(self, _mock_run):
        with self.assertRaisesRegex(VerificationError, "pip install pypdf"):
            run_tool(["pdftotext", "example.pdf", "-"])

    @patch("tools.verify_pdf.subprocess.run")
    def test_reports_unreadable_pdf(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["pdfinfo", "example.pdf"], stderr="invalid PDF"
        )

        with self.assertRaisesRegex(VerificationError, "invalid PDF"):
            run_tool(["pdfinfo", "example.pdf"])


if __name__ == "__main__":
    unittest.main()
