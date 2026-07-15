"""
tests/test_residual_masked_not_reported.py — #589-E/F (2026-07-15).

Live incident (the stuck liasse, 29/30 = 96% forever): the residual scan ran a
WEAKER detector than the masking pass (bare regex, no overlap resolution) AND runs
on the OUTPUT — which masking rearranges (token replacement + the #273 glue-fix
change spacing/boundaries). A real SIREN whose mangled spacing made it unmatchable
on the INPUT became matchable on the rearranged OUTPUT → reported as "residual
leak" → structured form fail-closed EVERY sweep, forever. The masker never saw the
match, so it could never fix it; the reporter saw it every time.

Fixes under test:
  #589-E  _residual_scan uses the SAME detection pipeline as masking
          (self._detect, not bare detect()).
  #589-E  a maskable residual found on the output is MASKED into the same vault
          (bounded loop), not just reported — the leak gets FIXED, not blocked on.
  #589-F  the Gemma /extract_pii timeout is env-tunable (was a hard, marginal 30s).
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.recognizers import Recognizer, RECOGNIZERS  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


def _mk_engine():
    """Real engine, real recognizers, no ML extras (deterministic regex only)."""
    return AnonymizationEngine(vault=Vault(), use_ner=False, use_llm=False,
                               context_boost=False)


# ── the incident shape: the residual scan finds a maskable value on the OUTPUT ──

def test_maskable_residual_is_masked_not_reported():
    """THE FIX: when the residual scan finds a real, locatable value on the output
    (the liasse incident: masking rearranged spacing and revealed a SIREN), that
    value must be MASKED into the vault — NOT reported as residual/'leak'.
    Deterministic: inject the revealed match via the scan itself (first scan finds
    it, the re-scan after masking finds nothing — exactly the incident dynamics)."""
    eng = _mk_engine()
    # the planted value must be UNDETECTABLE by pass 1 (lowercase, not PII-shaped)
    # so only the injected residual scan sees it — isolating the loop under test.
    text = "Monsieur Jean Dupont habite ici. Code interne zzqxsecret fin."
    state = {"n": 0}

    class _M:
        entity_type, score, priority = "SIREN", 0.93, 93
        def __init__(self, value, start, end):
            self.value, self.start, self.end = value, start, end

    def fake_scan(anonymized):
        state["n"] += 1
        i = anonymized.find("zzqxsecret")
        # keeps flagging it while visible; clean once masked — incident dynamics
        return [_M("zzqxsecret", i, i + 10)] if i >= 0 else []

    eng._residual_scan = fake_scan
    res = eng.anonymize(text)
    assert "zzqxsecret" not in res.anonymized, (
        f"the revealed value must be MASKED, not left visible: {res.anonymized!r}")
    assert "⟦SIREN" in res.anonymized, "a token must have been vaulted in"
    assert not res.has_residual, (
        "a maskable revealed value must NOT be reported as residual (old false-block)")
    assert res.verdict_state != "leak"
    assert state["n"] >= 2, "output must be re-scanned after masking the residual"


def test_genuinely_unmaskable_residual_still_reported():
    """The safety net must survive: if the residual scan finds something that
    CANNOT be masked (simulated by a scan that returns an un-locatable match),
    it stays reported → 'leak' → fail-closed. The fix must not fail-open."""
    eng = _mk_engine()
    text = "Monsieur Jean Dupont, IBAN FR7630006000011234567890189, Paris."
    real_scan = eng._residual_scan
    calls = {"n": 0}

    class _FakeMatch:
        # un-applicable: start/end outside the text → mask loop can't apply it
        entity_type, value, score, priority = "IBAN", "FRXX-DOES-NOT-APPEAR", 0.9, 90
        start, end = 10**6, 10**6 + 5

    def fake_scan(anonymized):
        calls["n"] += 1
        return [_FakeMatch()]

    eng._residual_scan = fake_scan
    res = eng.anonymize(text)
    assert res.has_residual, "an un-maskable residual must STILL be reported (fail-closed)"
    assert res.verdict_state == "leak"


def test_residual_scan_uses_same_detector_as_masking():
    """#589-E consistency: _residual_scan must call self._detect (the full masking
    pipeline), not the bare regex detect()."""
    import inspect
    from bubble_shield import engine as engmod
    src = inspect.getsource(engmod.AnonymizationEngine._residual_scan)
    # the CODE line must call the full pipeline (docstring may mention the old form)
    assert "for m in self._detect(anonymized)" in src, \
        "_residual_scan must iterate self._detect(anonymized) — the same pipeline as masking"


def test_clean_doc_unaffected():
    """Normal docs: no residual, verdict as before — the loop is a no-op."""
    eng = _mk_engine()
    res = eng.anonymize("Monsieur Jean Dupont, IBAN FR7630006000011234567890189.")
    assert not res.has_residual
    assert res.entity_count >= 1


# ── #589-F: the Gemma extract timeout knob ────────────────────────────────────

def test_gemma_extract_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv("BUBBLE_SHIELD_GEMMA_EXTRACT_TIMEOUT", raising=False)
    import bubble_shield_mcp as mcp
    mcp = importlib.reload(mcp)
    assert mcp._GEMMA_EXTRACT_TIMEOUT_S == 120.0, "default must be 120s (was a marginal 30s)"
    monkeypatch.setenv("BUBBLE_SHIELD_GEMMA_EXTRACT_TIMEOUT", "45")
    mcp = importlib.reload(mcp)
    assert mcp._GEMMA_EXTRACT_TIMEOUT_S == 45.0, "env override must be honoured"
