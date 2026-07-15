"""
tests/test_574_2ddoc_strip.py — strip the 2D-DOC barcode block by default (#574, child of #547).

French tax notices carry a machine-decodable ANSSI 2D-DOC barcode that encodes identity +
amounts — a COVERT PII channel that survives even when the visible text is tokenized. Per
Joris's #547 decision (barcode-first), we strip the block before tokenization and leave a
marker so the reader knows a block was removed. Amounts in the VISIBLE text stay untouched.

SYNTHETIC ONLY — the fixture below is a fabricated DC-header + fake payload, NEVER a real
2D-DOC barcode or real document.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugin" / "bubble-shield" / "scripts"))

import bubble_shield_extract as ex  # noqa: E402

# Fabricated 2D-DOC-shaped block: DC + version + fake CA id + fake payload. NOT real.
_SYNTH_2DDOC = "DC04FR000001ABCDEF0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ234567ABCDEFGHIJ"


def test_barcode_block_stripped_marker_present():
    doc = f"Avis 2024.\n{_SYNTH_2DDOC}\nMonsieur Jean Dupont."
    out = ex.strip_2ddoc_barcodes(doc)
    assert ex._2DDOC_MARKER in out, "a marker must replace the stripped block (not silent drop)"
    assert "ABCDEFGHIJKLMNOP" not in out, "the barcode payload must be gone from output"


def test_amounts_and_real_text_untouched():
    """Regression guard (#547): amounts stay in clear; surrounding real text intact."""
    doc = (f"Montant: 45 000 EUR. Revenu fiscal 78 900 EUR.\n{_SYNTH_2DDOC}\n"
           f"Monsieur Jean Dupont demeurant a Paris.")
    out = ex.strip_2ddoc_barcodes(doc)
    assert "45 000 EUR" in out and "78 900 EUR" in out, "amounts must NOT be masked (#547)"
    assert "Jean Dupont" in out and "Paris" in out, "surrounding real text must be untouched"


def test_idempotent():
    doc = f"x\n{_SYNTH_2DDOC}\ny"
    once = ex.strip_2ddoc_barcodes(doc)
    assert ex.strip_2ddoc_barcodes(once) == once, "the marker itself must never re-match"


def test_normal_uppercase_text_not_stripped():
    """The detector must NOT false-strip normal uppercase prose/headings (they have
    spaces; the 40+-char CONTIGUOUS payload requirement is what distinguishes a barcode)."""
    for neg in ["DOCUMENT DE CONSEIL FINANCIER POUR LE CLIENT DUPONT",
                "MONSIEUR JEAN DUPONT DEMEURANT A PARIS 75008",
                "MONTANT TOTAL 45 000 EUR SIRET 123 456 789 00011",
                "DECLARATION DE REVENUS 2024 REFERENCE ABC123"]:
        assert ex.strip_2ddoc_barcodes(neg) == neg, f"must not strip normal text: {neg!r}"


def test_extract_text_applies_strip():
    """extract_text (the single dispatch) must apply the strip on the plain-text branch."""
    raw = f"Avis.\n{_SYNTH_2DDOC}\nfin.".encode("utf-8")
    out = ex.extract_text("notice.txt", raw)
    assert ex._2DDOC_MARKER in out and "ABCDEFGHIJKLMNOP" not in out


def test_strip_failure_returns_original(monkeypatch):
    """A strip error must never lose the document (best-effort)."""
    import re
    monkeypatch.setattr(ex, "_2DDOC_RE", None)  # .sub on None → AttributeError
    doc = "some text"
    assert ex.strip_2ddoc_barcodes(doc) == doc, "on error, return the original text unchanged"
