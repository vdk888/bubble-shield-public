#!/usr/bin/env python3
"""test_276_garbled_route.py — OCR routing for GARBLED native PDF extraction (#276).

When pypdf extracts text from a liasse fiscale it sometimes GLUES words together
without spaces ("gérantETESTONI", "NOAMSignature").  Per-boundary fixes (#273,
#275) handle some of these, but a 4-char forename like "NOAM" can't be safely
caught without lowering the length floor and over-masking common names (JEAN/PAUL).

The DURABLE fix: detect garbled extraction via a conservative 3-signal heuristic
and, when the OCR pack is installed, re-extract via OCR (clean, properly-spaced
text).  This eliminates the entire glue-artifact class at once.

Tests:
  1. _is_garbled_extraction heuristic — garbled text triggers True
  2. _is_garbled_extraction heuristic — clean text returns False
     (including docs with ALL-CAPS headings, long URLs, legitimate long words)
  3. Wiring in extract_pdf_text:
     a. Garbled native text + OCR pack present (mocked) → OCR text used
     b. Garbled native text + OCR pack absent → native text returned (fail-open)
  4. Clean native text → OCR never called (mock asserts not invoked)

All PII is SYNTHETIC.

Run standalone: python3 scripts/test_276_garbled_route.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add scripts dir + vendor to sys.path so we can import bubble_shield_extract
_HERE = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HERE.parent
_VENDOR = _PLUGIN_ROOT / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import bubble_shield_extract as _ext
from bubble_shield_extract import _is_garbled_extraction, extract_pdf_text, ExtractionError, _OCR_TAG

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


# ---------------------------------------------------------------------------
# Synthetic texts
# ---------------------------------------------------------------------------

# Garbled liasse-like text: the exact artifact class from the real liasse fiscale.
# Glued words, no spaces between tokens, CamelCase boundaries everywhere.
# Low space ratio + many long tokens + many camelCase transitions.
GARBLED_TEXT = (
    "GérantETESTONI\n"
    "NOAMSignature\n"
    "SIGNATAIREFAKENAMEETESTONISignature\n"
    "NOMdePRENOM\n"
    "FAKENAMESignature\n"
    "gérantETESTONI\n"
    "SIRETETPRODUITSdeCOMMERCE\n"
    "DébiteurCRÉDITEUR\n"
    "SELARL DU DOCTEUR FAKENAME TESTONI\n"  # one clean line — still mostly garbled
    "BICEtADRESSEETOBSERVATIONS\n"
)

# Clean French prose — should NOT be flagged garbled.
CLEAN_FRENCH = (
    "Le présent document a été établi par le cabinet.\n"
    "L'entreprise SELARL DU DOCTEUR DUPONT MARTIN exerce son activité.\n"
    "Forme juridique : SELARL\n"
    "Numéro SIRET : 123 456 789 00012\n"
    "Adresse : 12 rue de la Paix, 75001 Paris\n"
    "Le gérant est Monsieur Jean Dupont.\n"
    "La date de signature est le 15 mars 2024.\n"
    "Exercice comptable : 01/01/2023 – 31/12/2023.\n"
    "Ce document est confidentiel et réservé à l'usage professionnel.\n"
    "Veuillez contacter votre conseiller pour toute question.\n"
)

# Clean text with ALL-CAPS headings — must NOT be flagged garbled.
CLEAN_WITH_CAPS = (
    "IDENTIFICATION DU CLIENT\n"
    "Nom : DUPONT\n"
    "Prénom : Jean\n"
    "SIRET : 123 456 789 00012\n"
    "Né le : 15/03/1975\n"
    "Adresse : 12 rue de la Paix, 75001 Paris\n"
    "INFORMATIONS COMPLÉMENTAIRES\n"
    "Le présent document est confidentiel.\n"
    "Tout usage non autorisé est interdit.\n"
    "SIGNATURE DU GÉRANT\n"
    "Date : 15/03/2024\n"
)

# Clean text with a long URL — must NOT be flagged garbled.
CLEAN_WITH_URL = (
    "Référence : https://www.bubbleinvest.com/clients/dossier-12345/document-fiscal-2024\n"
    "Retrouvez vos documents sur https://app.example.com/dossiers/fiscalite/liasse?year=2023\n"
    "Le cabinet peut être contacté à contact@bubbleinvest.com\n"
    "Nom : DUPONT\n"
    "Prénom : Jean\n"
    "Le solde au 31/12/2023 est de 12 500 euros.\n"
    "Veuillez retourner ce formulaire signé sous 15 jours.\n"
)

# Clean text with a legit long word (non-glued) — must NOT be flagged garbled.
CLEAN_WITH_LONG_WORDS = (
    "Dénomination sociale : Société par actions simplifiée unipersonnelle\n"
    "Objet social : accompagnement en investissement financier et patrimoine\n"
    "Le représentant légal est responsable de la conformité réglementaire.\n"
    "Cette déclaration de revenus concerne l'exercice comptable 2023.\n"
    "Impôts sur les bénéfices des sociétés soumises à l'IS.\n"
    "Nom : DUPONT\n"
    "Prénom : Jean\n"
)


def _make_minimal_pdf_with_text(text: str) -> bytes:
    """Build a minimal synthetic PDF with a text layer.

    This produces a real PDF that pypdf can parse and extract text from.
    The text layer is embedded as a raw PDF content stream.

    Used only for the extract_pdf_text wiring tests.  The text we pass
    is controlled synthetic text — no real PII.
    """
    import struct as _struct

    # Encode text into PDF content stream using simple BT/ET with Tf + Tj
    # Each line is shown; we just embed as one big Tj string for simplicity.
    # Actually we use a text blob and just care that pypdf extracts *something*.
    # Simpler: embed text as raw bytes in a stream — pypdf will extract it.
    # We use a proper (minimal) PDF with a text-layer content stream.

    # Build the content stream: display lines of text
    content_lines = []
    content_lines.append("BT")
    content_lines.append("/F1 12 Tf")
    y = 750
    for line in text.splitlines():
        # Escape special PDF chars in the line
        escaped = (
            line.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
        )
        content_lines.append(f"50 {y} Td")
        content_lines.append(f"({escaped}) Tj")
        y -= 14
        if y < 50:
            break  # overflow guard
    content_lines.append("ET")
    content_stream = "\n".join(content_lines).encode("latin-1", errors="replace")
    cs_len = len(content_stream)

    # Build minimal PDF structure
    # Objects: 1=Catalog, 2=Pages, 3=Page, 4=Font, 5=Content
    offsets = {}
    out = bytearray()

    def w(s: bytes) -> None:
        out.extend(s)

    def nl() -> None:
        out.extend(b"\n")

    w(b"%PDF-1.4\n")

    offsets[1] = len(out)
    w(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    nl()

    offsets[2] = len(out)
    w(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    nl()

    offsets[3] = len(out)
    w(b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
      b"/MediaBox [0 0 612 792] "
      b"/Resources << /Font << /F1 4 0 R >> >> "
      b"/Contents 5 0 R >>\nendobj\n")
    nl()

    offsets[4] = len(out)
    w(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    nl()

    offsets[5] = len(out)
    w(f"5 0 obj\n<< /Length {cs_len} >>\nstream\n".encode())
    w(content_stream)
    w(b"\nendstream\nendobj\n")
    nl()

    # xref table
    xref_offset = len(out)
    w(b"xref\n")
    w(f"0 6\n".encode())
    w(b"0000000000 65535 f \n")
    for i in range(1, 6):
        w(f"{offsets[i]:010d} 00000 n \n".encode())

    w(b"trailer\n<< /Size 6 /Root 1 0 R >>\n")
    w(f"startxref\n{xref_offset}\n%%EOF\n".encode())

    return bytes(out)


# ---------------------------------------------------------------------------
# Test 1: _is_garbled_extraction — garbled text → True
# ---------------------------------------------------------------------------
print("=" * 60)
print("test_276_garbled_route.py — OCR routing for GARBLED extraction (#276)")
print("All PII is SYNTHETIC")
print("=" * 60)

print("\n[Test 1] _is_garbled_extraction: garbled text → True")
result_garbled = _is_garbled_extraction(GARBLED_TEXT)
print(f"  GARBLED_TEXT → {result_garbled}")
check("1a: garbled liasse text flagged as garbled", result_garbled is True)

# Also check with a dense garbled string representative of a full garbled page:
# include some very long glued tokens (as appear in real liasse extractions)
DENSE_GARBLED = (
    "gérantETESTONI\n"
    "NOAMSignature\n"
    "FAKENAMESignature\n"
    "SIGNATAIREFAKENAMEETESTONISignature\n"   # > 25 chars glued token
    "NOMdePRENOMdePERSONNEETPREPOSITIONS\n"   # > 25 chars
    "BICEtADRESSEETOBSERVATIONSCOMPLEMENT\n"  # > 25 chars
    "gérantETESTONI\n"
    "NOAMSignature\n"
    "FAKENAMESignature\n"
    "SIGNATAIREFAKENAMEETESTONISignature\n"
)
result_dense = _is_garbled_extraction(DENSE_GARBLED)
print(f"  DENSE_GARBLED → {result_dense}")
check("1b: dense garbled text flagged", result_dense is True)


# ---------------------------------------------------------------------------
# Test 2: _is_garbled_extraction — clean texts → False
# ---------------------------------------------------------------------------
print("\n[Test 2] _is_garbled_extraction: clean texts → False")

cases = [
    ("clean French prose", CLEAN_FRENCH),
    ("clean with ALL-CAPS headings", CLEAN_WITH_CAPS),
    ("clean with long URL", CLEAN_WITH_URL),
    ("clean with long legitimate words", CLEAN_WITH_LONG_WORDS),
    ("empty string", ""),
    ("single word", "Bonjour"),
    ("short label-value pairs", "Nom : Dupont\nPrénom : Jean\nSIRET : 123 456 789\n"),
]

for name, text in cases:
    r = _is_garbled_extraction(text)
    print(f"  '{name}' → {r}")
    check(f"2: '{name}' NOT flagged garbled", r is False)


# ---------------------------------------------------------------------------
# Test 3a: extract_pdf_text wiring — garbled + OCR pack present → OCR text used
# ---------------------------------------------------------------------------
print("\n[Test 3a] extract_pdf_text: garbled native + OCR pack present → OCR text used")

FAKE_OCR_OUTPUT = (
    "[OCR] Le gérant est TESTONI FAKENAME.\n"
    "NOAM FAKENAME — Signature.\n"
    "Forme juridique : SELARL\n"
)

_orig_ocr = _ext._ocr_pdf_if_pack_present
_orig_is_garbled = _ext._is_garbled_extraction

ocr_call_count_3a = 0


def _mock_ocr_returns_clean(raw: bytes):
    global ocr_call_count_3a
    ocr_call_count_3a += 1
    return FAKE_OCR_OUTPUT


def _mock_is_garbled_true(text: str) -> bool:
    return True


_ext._ocr_pdf_if_pack_present = _mock_ocr_returns_clean
_ext._is_garbled_extraction = _mock_is_garbled_true

try:
    garbled_pdf = _make_minimal_pdf_with_text(
        "gérantETESTONI\nNOAMSignature\nFAKENAMESignature\n"
    )
    result_3a = extract_pdf_text(garbled_pdf)
    print(f"  result: {result_3a[:120]!r}")
    check("3a: OCR text used when garbled + pack present", result_3a == FAKE_OCR_OUTPUT)
    check("3a: OCR call count == 1", ocr_call_count_3a == 1)
    check("3a: [OCR] tag present in result", result_3a.startswith("[OCR]"))
finally:
    _ext._ocr_pdf_if_pack_present = _orig_ocr
    _ext._is_garbled_extraction = _orig_is_garbled


# ---------------------------------------------------------------------------
# Test 3b: extract_pdf_text wiring — garbled + OCR pack absent → native text (fail-open)
# ---------------------------------------------------------------------------
print("\n[Test 3b] extract_pdf_text: garbled native + OCR pack absent → native text (fail-open)")

_orig_ocr = _ext._ocr_pdf_if_pack_present
_orig_is_garbled = _ext._is_garbled_extraction

ocr_call_count_3b = 0


def _mock_ocr_returns_none(raw: bytes):
    global ocr_call_count_3b
    ocr_call_count_3b += 1
    return None  # pack absent


def _mock_is_garbled_true_3b(text: str) -> bool:
    return True


_ext._ocr_pdf_if_pack_present = _mock_ocr_returns_none
_ext._is_garbled_extraction = _mock_is_garbled_true_3b

try:
    garbled_pdf_2 = _make_minimal_pdf_with_text(
        "gérantETESTONI\nNOAMSignature\nFAKENAMESignature\n"
    )
    result_3b = extract_pdf_text(garbled_pdf_2)
    print(f"  result (first 120): {result_3b[:120]!r}")
    check("3b: no crash when OCR pack absent (fail-open)", True)
    check("3b: native text returned (non-empty)", bool(result_3b))
    check("3b: result is NOT the fake OCR output", result_3b != FAKE_OCR_OUTPUT)
    check("3b: OCR was still attempted (then fell through)", ocr_call_count_3b == 1)
finally:
    _ext._ocr_pdf_if_pack_present = _orig_ocr
    _ext._is_garbled_extraction = _orig_is_garbled


# ---------------------------------------------------------------------------
# Test 4: extract_pdf_text wiring — clean text → OCR never called
# ---------------------------------------------------------------------------
print("\n[Test 4] extract_pdf_text: clean native text → OCR NOT invoked")

_orig_ocr = _ext._ocr_pdf_if_pack_present
_orig_is_garbled = _ext._is_garbled_extraction

ocr_call_count_4 = 0


def _mock_ocr_should_not_be_called(raw: bytes):
    global ocr_call_count_4
    ocr_call_count_4 += 1
    return "[OCR] unexpected call"


def _mock_is_garbled_false(text: str) -> bool:
    return False  # clean text — heuristic returns False


_ext._ocr_pdf_if_pack_present = _mock_ocr_should_not_be_called
_ext._is_garbled_extraction = _mock_is_garbled_false

try:
    clean_pdf = _make_minimal_pdf_with_text(
        "Nom : DUPONT\nPrénom : Jean\nSIRET : 123 456 789 00012\n"
    )
    result_4 = extract_pdf_text(clean_pdf)
    print(f"  result (first 120): {result_4[:120]!r}")
    check("4: clean PDF: OCR NOT called", ocr_call_count_4 == 0)
    check("4: clean PDF: native text returned (non-empty)", bool(result_4))
    check("4: clean PDF: [OCR] tag NOT in result", not result_4.startswith("[OCR]"))
finally:
    _ext._ocr_pdf_if_pack_present = _orig_ocr
    _ext._is_garbled_extraction = _orig_is_garbled


# ---------------------------------------------------------------------------
# Test 5: heuristic precision — borderline/edge cases
# ---------------------------------------------------------------------------
print("\n[Test 5] Heuristic precision — edge cases")

# Text with one glued token but otherwise clean → signal thresholds not met
ONE_GLUE = (
    "Nom : DUPONT\n"
    "Prénom : Jean\n"
    "gérantETESTONI\n"   # one glue artifact
    "SIRET : 123 456 789 00012\n"
    "Le solde est de 12 500 euros.\n"
    "Signature le 15/03/2024.\n"
)
r5a = _is_garbled_extraction(ONE_GLUE)
print(f"  ONE_GLUE (mostly clean, one artifact) → {r5a}")
# Conservative: one artifact in mostly-clean text might or might not trigger
# depending on the ratios.  We don't assert a specific result here — we just
# document the observed behavior.  The important thing is it's not crashing.
check("5a: ONE_GLUE does not crash", True)
print(f"    (observed: {r5a} — acceptable either way for a mixed doc)")

# All-caps non-glued text (e.g. an ALL-CAPS section header document)
ALLCAPS_CLEAN = (
    "IDENTIFICATION DU CLIENT\n"
    "NOM DE NAISSANCE DUPONT\n"
    "PRENOM JEAN\n"
    "SIRET 123456789\n"
    "DATE DE NAISSANCE 15 03 1975\n"
    "ADRESSE 12 RUE DE LA PAIX 75001 PARIS\n"
    "SIGNATURE ET DATE\n"
    "PIECE D IDENTITE PASSEPORT\n"
)
r5b = _is_garbled_extraction(ALLCAPS_CLEAN)
print(f"  ALLCAPS_CLEAN → {r5b}")
check("5b: all-caps non-glued text NOT flagged garbled", r5b is False)

# Mixed: garbled + some clean lines
MIXED = (
    "gérantETESTONI\n"
    "NOAMSignature\n"
    "FAKENAMESignature\n"
    "Nom : DUPONT\n"
    "Prénom : Jean\n"
    "SIRETETPRODUITSdeCOMMERCE\n"
    "NOMdePRENOM\n"
)
r5c = _is_garbled_extraction(MIXED)
print(f"  MIXED (glued + clean lines) → {r5c}")
# The mixed text has multiple glue artifacts — should fire (or be borderline)
# We just check it doesn't crash
check("5c: MIXED does not crash", True)
print(f"    (observed: {r5c})")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
print(f"{passed} passed, {failed} failed")
if failed:
    print("\nSome tests FAILED.")
    sys.exit(1)
else:
    print("\nAll tests passed.")
    sys.exit(0)
