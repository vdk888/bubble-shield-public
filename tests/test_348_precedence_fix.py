"""
test_348_precedence_fix.py — #348 SAFETY BLOCKER fixes (two linked PII-leak bugs).

BUG 1 — precedence gap: the gazetteer-always-wins exemption was only applied in
the safe-list step, so the org allowlist and common-word steps could DROP
(un-mask) a value that is in the gazetteer (confirmed PII). Spec rule: a value
in the gazetteer must STAY MASKED regardless of ANY negative list.

BUG 2 — substring over-suppression: short org tokens in PUBLIC_THIRD_PARTIES
(e.g. "eres", "axa", "corum") matched INSIDE real surnames ("Eres Martin"),
un-masking real client names. Short/single-token allowlist phrases must match a
WHOLE token, not an arbitrary substring; multi-word firm/regulator phrases keep
substring/phrase matching.

All fixtures SYNTHETIC. Tests pass a temp gazetteer path + tmp BUBBLE_SHIELD_HOME
so the real ~/.bubble_shield/ store is NEVER polluted.
"""
import sys
from pathlib import Path

import pytest

# the composed match_filter lives in the MCP daemon script
_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402
from bubble_shield import known_pii_store as kps  # noqa: E402
from bubble_shield.allowlist import Allowlist, PUBLIC_THIRD_PARTIES, is_allowlisted  # noqa: E402
from bubble_shield.recognizers import Match  # noqa: E402


def _m(entity_type, value):
    return Match(start=0, end=len(value), entity_type=entity_type, value=value)


# ── BUG 1 — gazetteer wins across ALL THREE negative filters ────────────────

def test_bug1_gazetteer_value_survives_allowlist_step(tmp_path, monkeypatch):
    """A NOM whose value is on the org-allowlist ("Axa") BUT is also in the
    gazetteer (confirmed client PII) must be KEPT (masked) after the full
    composed negative-filter chain. Pre-fix: the allowlist step dropped it."""
    gaz = tmp_path / "known_pii.json"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    kps.add_confirmed_pii("Axa", "NOM", path=str(gaz))

    al = PUBLIC_THIRD_PARTIES  # contains "axa"
    matches = [_m("NOM", "Axa")]
    kept = mcp._apply_negative_filters(matches, allowlist=al, known_pii_path=str(gaz))
    assert [m.value for m in kept] == ["Axa"], "gazetteer value un-masked by allowlist step"


def test_bug1_gazetteer_value_survives_common_word_step(tmp_path, monkeypatch):
    """A NOM that is also a curated common word BUT is in the gazetteer must
    stay masked. Pre-fix: the common-word step dropped it."""
    from bubble_shield import common_words as cw
    common = next(iter(cw._COMMON))  # a token the common-word filter would drop
    gaz = tmp_path / "known_pii.json"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    kps.add_confirmed_pii(common, "NOM", path=str(gaz))

    matches = [_m("NOM", common)]
    kept = mcp._apply_negative_filters(matches, allowlist=None, known_pii_path=str(gaz))
    assert [m.value for m in kept] == [common], "gazetteer value un-masked by common-word step"


def test_bug1_gazetteer_value_survives_safe_list_step(tmp_path, monkeypatch):
    """Regression guard: gazetteer value also safe-listed → still kept (this was
    the only step that already had the exemption; it must keep working)."""
    from bubble_shield import safe_words as sw
    val = "Provence"
    gaz = tmp_path / "known_pii.json"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    kps.add_confirmed_pii(val, "NOM", path=str(gaz))
    monkeypatch.setattr(sw, "load_safe", lambda: {val.lower()})

    matches = [_m("NOM", val)]
    kept = mcp._apply_negative_filters(matches, allowlist=None, known_pii_path=str(gaz))
    assert [m.value for m in kept] == [val], "gazetteer value un-masked by safe-list step"


def test_bug1_non_gazetteer_org_still_dropped(tmp_path, monkeypatch):
    """Sanity: a pure org token NOT in the gazetteer is still dropped by the
    allowlist (the fix must not disable legitimate allowlisting)."""
    gaz = tmp_path / "known_pii.json"  # empty gazetteer
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    matches = [_m("NOM", "AMF")]
    kept = mcp._apply_negative_filters(matches, allowlist=PUBLIC_THIRD_PARTIES, known_pii_path=str(gaz))
    assert kept == [], "legitimate org token should still be allowlisted away"


# ── BUG 2 — word-boundary matching, no substring leak of real surnames ──────

def test_bug2_short_org_token_does_not_match_inside_surname():
    assert is_allowlisted("Eres Martin") is False       # "eres" ⊂ value, not whole token
    assert is_allowlisted("Aprilia Durand") is False    # "april" ⊂ "aprilia"
    assert is_allowlisted("Corumbel Dupont") is False   # "corum" ⊂ "corumbel"
    assert is_allowlisted("Marie-Axandre") is False     # "axa" ⊂ "axandre"


def test_bug2_standalone_org_token_still_allowlisted():
    assert is_allowlisted("ERES") is True
    assert is_allowlisted("AMF") is True
    assert is_allowlisted("Axa") is True
    # standalone token embedded in surrounding context still matches
    assert is_allowlisted("souscription ERES") is True


def test_bug2_multiword_phrase_keeps_substring_matching():
    # multi-word regulator/firm phrases keep phrase/substring matching
    assert is_allowlisted("Autorité des marchés financiers") is True
    assert is_allowlisted("BNP Paribas") is True
