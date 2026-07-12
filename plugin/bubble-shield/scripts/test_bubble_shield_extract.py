#!/usr/bin/env python3
"""Tests for the bundled bubble_shield_extract.py — run: python3 test_bubble_shield_extract.py

Stdlib only (unittest). Mirrors the webapp extract tests but covers the bundled
single-file copy + its CLI contract, which the skill depends on.
"""
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bubble_shield_extract as ce  # noqa: E402


def _minimal_pdf(text: str = "Monsieur Dupont IBAN FR76") -> bytes:
    """Build a tiny valid one-page PDF containing `text` (no external libs)."""
    content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objs) + 1))
    out.write(b"startxref\n%d\n%%%%EOF" % xref_pos)
    return out.getvalue()


class DispatchTests(unittest.TestCase):
    def test_txt_decodes_directly(self):
        self.assertEqual(ce.extract_text("a.txt", "héllo".encode("utf-8")), "héllo")

    def test_empty_returns_empty(self):
        self.assertEqual(ce.extract_text("a.txt", b""), "")

    def test_csv_and_json_decode(self):
        self.assertIn("a,b", ce.extract_text("x.csv", b"a,b\n1,2"))
        self.assertIn("name", ce.extract_text("x.json", b'{"name":"x"}'))

    def test_looks_like_pdf_by_magic_and_ext(self):
        self.assertTrue(ce.looks_like_pdf("x", b"%PDF-1.4"))
        self.assertTrue(ce.looks_like_pdf("file.PDF", b"junk"))
        self.assertFalse(ce.looks_like_pdf("x.txt", b"plain"))

    def test_bad_pdf_raises_extraction_error(self):
        with self.assertRaises(ce.ExtractionError):
            ce.extract_text("broken.pdf", b"%PDF-not-really")


class PdfTests(unittest.TestCase):
    def setUp(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed")

    def test_real_pdf_text_extracted(self):
        raw = _minimal_pdf("Madame Test Exemple")
        text = ce.extract_text("dossier.pdf", raw)
        self.assertIn("Exemple", text)

    def test_scanned_pdf_no_text_fails_closed(self):
        # a PDF with no text content stream → should raise, not return ""
        raw = _minimal_pdf("")  # empty text op
        with self.assertRaises(ce.ExtractionError):
            ce.extract_text("scan.pdf", raw)


class CliTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name("bubble_shield_extract.py")), *args],
            capture_output=True, text=True,
        )

    def test_cli_txt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("Bonjour Dupont")
            p = f.name
        r = self._run(p)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Dupont", r.stdout)

    def test_cli_check_ok(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("x")
            p = f.name
        r = self._run(p, "--check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("OK", r.stdout)

    def test_cli_bad_pdf_exit_2(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-garbage")
            p = f.name
        r = self._run(p)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.strip())  # human reason on stderr

    def test_cli_no_args_usage(self):
        r = self._run()
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
