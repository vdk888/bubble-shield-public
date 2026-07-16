"""
tests/test_400_tripwire_secu_checksum.py — tripwire SECU needs a mod-97 checksum (#400).

The UserPromptSubmit tripwire's SECU pattern fired on a benign 13-15-digit run (a Google
error-screen number in an OCR'd image Jade pasted) → nudged 'données client brutes'
(numéro de sécurité sociale) that weren't there. Fix: gate the SECU match on the NIR
mod-97 control-key check (mirrors the IBAN path, which already validates). Synthetic
NIRs only — construct a valid key arithmetically, never a real person's number.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugin" / "bubble-shield" / "scripts"))

import tripwire as tw  # noqa: E402


def _valid_nir(body13: str) -> str:
    """Append the correct mod-97 control key to a 13-digit body → a checksum-valid NIR."""
    key = 97 - (int(body13) % 97)
    return f"{body13}{key:02d}"


def test_valid_nir_still_flags():
    nir = _valid_nir("1850578006048")  # synthetic body, valid key appended
    assert "numéro de sécurité sociale" in tw._find_pii(f"Mon numéro : {nir}")


def test_random_15_digits_no_false_positive():
    """The #400 shape: a 15-digit run that does NOT satisfy mod-97 must NOT nudge."""
    assert "numéro de sécurité sociale" not in tw._find_pii("Erreur code 123456789012345")


def test_benign_13_digits_no_false_positive():
    assert "numéro de sécurité sociale" not in tw._find_pii("référence 1234567890123")


def test_secu_valid_helper():
    assert tw._secu_valid(_valid_nir("2920578006123")) is True
    assert tw._secu_valid("123456789012345") is False  # bad checksum
    assert tw._secu_valid("12345") is False             # too short to validate


def test_iban_path_unaffected():
    """Regression: the IBAN validation path is untouched."""
    # a checksum-valid public example IBAN
    assert "IBAN" in tw._find_pii("Virement FR7630006000011234567890189 svp")
