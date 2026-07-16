"""
test_659_no_nested_tokens.py — #659: Gemma passes must never corrupt existing
mask tokens (nested-token double-wrap).

FOUND LIVE (2026-07-16, #554 flow verification): the Gemma additive/second
passes run `out.replace(val, token)` on ALREADY-MASKED text. Gemma sees the
token markers and extracts their innards ("NOM_0002") as PII spans — live
gemmad reproduced: {"spans":[{"type":"NOM","text":"NOM_0002"}]}. The blind
replace then rewrites INSIDE the existing token: ⟦NOM_0002⟧ → ⟦⟦NOM_0004⟧⟧
(nested), which breaks restore reversibility.

Fix under test: `_replace_outside_tokens` — replacement only applies in text
segments OUTSIDE existing ⟦…⟧ token spans, so a span whose only occurrence is
inside a token is a no-op (and doesn't count as "applied").

Synthetic values only.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "scripts"))
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))

import pytest

import bubble_shield_mcp as mcp
from bubble_shield.vault import TOKEN_RE

NESTED = re.compile(r"⟦[A-Z_]*⟦")   # any token-opener inside a token → corruption


# ── the helper itself ─────────────────────────────────────────────────────────

def test_helper_replaces_outside_tokens_only():
    text = "dossier de ⟦NOM_0001⟧ chez Legrand, ref NOM_0001 en clair"
    out, n = mcp._replace_outside_tokens(text, "Legrand", "⟦NOM_0002⟧")
    assert n == 1
    assert "⟦NOM_0002⟧" in out
    assert "⟦NOM_0001⟧" in out            # existing token untouched
    assert not NESTED.search(out)


def test_helper_token_innards_are_noop():
    # Gemma's live failure shape: it extracts the innards of an existing token.
    text = "⟦NOM_0002⟧, vous trouverez le dossier de ⟦NOM_0001⟧."
    for val in ("NOM_0002", "0002", "NOM_0001"):
        out, n = mcp._replace_outside_tokens(text, val, "⟦NOM_0009⟧")
        assert n == 0, f"{val!r} lives only inside a token — must be a no-op"
        assert out == text
        assert not NESTED.search(out)


def test_helper_value_present_both_inside_and_outside():
    # "0002" appears inside a token AND in clear → only the clear one is replaced.
    text = "⟦NOM_0002⟧ facture no 0002 du client"
    out, n = mcp._replace_outside_tokens(text, "0002", "⟦NUM_0003⟧")
    assert n == 1
    assert out == "⟦NOM_0002⟧ facture no ⟦NUM_0003⟧ du client"


# ── the two Gemma passes, with a stubbed Gemma returning the poison spans ─────

class _Res:
    def __init__(self, anonymized, entity_count=1, has_residual=False):
        self.anonymized = anonymized
        self.entity_count = entity_count
        self.has_residual = has_residual
        self.original = anonymized
        self.verdict_state = "masked_ok"


class _Vault:
    def __init__(self):
        self.n = 8
    def token_for(self, val, typ):
        self.n += 1
        return f"⟦{typ}_{self.n:04d}⟧"


class _Engine:
    def __init__(self):
        self.vault = _Vault()


def test_additive_pass_ignores_token_innards(monkeypatch):
    masked = "⟦NOM_0002⟧, vous trouverez le dossier de ⟦NOM_0001⟧. Le taux est de 3,2 %."
    res = _Res(masked)
    # exactly what live gemmad returned on masked text (2026-07-16)
    monkeypatch.setattr(mcp, "_gemma_extract_call",
                        lambda text: [{"type": "NOM", "text": "NOM_0002"},
                                      {"type": "NOM", "text": "NOM_0001"}])
    monkeypatch.setattr(mcp, "_finalise_anonymised", lambda res, out: out)
    out = mcp._gemma_additive_pass(res, _Engine())
    assert out == masked                   # nothing legitimately new → unchanged
    assert not NESTED.search(out)


def test_additive_pass_still_masks_genuine_new_span(monkeypatch):
    masked = "⟦NOM_0002⟧ habite chez Mme Lenoir a Nantes."
    res = _Res(masked)
    monkeypatch.setattr(mcp, "_gemma_extract_call",
                        lambda text: [{"type": "NOM", "text": "Lenoir"},
                                      {"type": "NOM", "text": "NOM_0002"}])
    monkeypatch.setattr(mcp, "_finalise_anonymised", lambda res, out: out)
    out = mcp._gemma_additive_pass(res, _Engine())
    assert "Lenoir" not in out             # genuine miss got masked
    assert "⟦NOM_0002⟧" in out             # existing token intact
    assert not NESTED.search(out)


def test_second_pass_token_innards_dont_count_as_applied(monkeypatch):
    # A form where the fast pass already masked real PII: Gemma returning ONLY
    # token-innards must count as "nothing applied" → verified-clean branch
    # (entity_count>0, no residual) returns the body WITHOUT nesting.
    masked = "Formulaire: ⟦NOM_0001⟧ ⟦SECU_0002⟧ case 7AB"
    res = _Res(masked, entity_count=2, has_residual=False)
    monkeypatch.setattr(mcp, "_gemma_extract_call",
                        lambda text: [{"type": "NOM", "text": "NOM_0001"}])
    monkeypatch.setattr(mcp, "_extract_window_count", lambda n: 1)
    out = mcp._gemma_second_pass(res, _Engine())
    assert not NESTED.search(out)
    assert "⟦NOM_0001⟧" in out and "⟦SECU_0002⟧" in out


def test_second_pass_zero_detection_still_fails_closed(monkeypatch):
    # Guarantee preserved: fast pass found nothing on a form + Gemma returns only
    # token-innards (nothing applied) → still fail closed (#589-B).
    masked = "Formulaire fiscal avec du contenu substantiel " * 5
    res = _Res(masked, entity_count=0, has_residual=False)
    monkeypatch.setattr(mcp, "_gemma_extract_call",
                        lambda text: [{"type": "NOM", "text": "NOM_0001"}])
    monkeypatch.setattr(mcp, "_extract_window_count", lambda n: 1)
    with pytest.raises(Exception):
        mcp._gemma_second_pass(res, _Engine())
