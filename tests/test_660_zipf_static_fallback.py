"""
test_660_zipf_static_fallback.py — #660: the de-pollution zipf junk-lane must
work WITHOUT wordfreq installed.

VERIFIED GAP (2026-07-16): wordfreq is not installed in the prod app venv (nor
ml-env) → _max_zipf returned 0.0 → triage() sent EVERY entry to the Gemma lane
(~6s serial call each). The Rule-A fast lane was inert since it shipped.

FIX under test: a vendored static word set (bubble_shield/data/
common_words_zipf4.txt — every fr/en wordform with zipf >= 4.0, generated from
wordfreq 3.1.1, 12,655 words, 99KB) used as a fallback when wordfreq is absent.
Verdict-equivalent to wordfreq for the Rule-A check: membership ⇔ zipf >= 4.0.

Synthetic values only.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))

import pytest

from bubble_shield import depollute as dp


@pytest.fixture()
def no_wordfreq(monkeypatch):
    """Simulate the prod venv: wordfreq is not importable."""
    import builtins
    real_import = builtins.__import__
    def _imp(name, *a, **k):
        if name == "wordfreq":
            raise ImportError("no wordfreq (prod venv simulation)")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)


def test_static_set_ships_and_loads():
    words = dp._common_words_static()
    assert len(words) > 10000              # the vendored list is real, not a stub
    assert "conseiller" in words and "taux" in words and "vous" in words
    assert "lenoir" not in words           # rare surname stays out (zipf 3.1)


def test_junk_lane_works_without_wordfreq(no_wordfreq):
    # THE prod bug: these returned 'uncertain' and burned Gemma calls.
    assert dp.triage("conseiller") == "junk"
    assert dp.triage("taux") == "junk"
    assert dp.triage("vous") == "junk"


def test_capitalized_and_rare_still_uncertain_without_wordfreq(no_wordfreq):
    assert dp.triage("Bonjour") == "uncertain"      # islower() gate unchanged
    assert dp.triage("lenoir") == "uncertain"       # rare word → Gemma judges
    assert dp.triage("Madame Sylvie FONTAINE") == "uncertain"
    assert dp.triage("") == "uncertain"


def test_fontaine_semantics_match_wordfreq(no_wordfreq):
    # "fontaine" has fr zipf 4.2 → wordfreq WOULD junk it when lowercase.
    # The static set must match (verdict-equivalence), and the protection for
    # the real surname comes from capitalization (gazetteer stores it cased).
    assert dp.triage("fontaine") == "junk"
    assert dp.triage("Fontaine") == "uncertain"
