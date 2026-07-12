"""Tests for the gemma_mode config toggle + mode-aware Gemma gate (#589-B preserving).

The KEY safety proof is `test_gate_decision_all_six_cases`: it exercises the pure
gate-decision helper for all/hard/off x form/prose (6 cases) WITHOUT a live daemon.
The security-critical assertion is off+form -> "fail_closed" (never "skip"): an
off-mode structured form must be REFUSED, not returned unverified (would re-open the
#589-B liasse/CERFA PII leak on weak Macs).
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vendor"))
import bubble_shield_mcp as M  # noqa: E402
from bubble_shield import policy as _P  # noqa: E402
from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


def _make_engine():
    """A real AnonymizationEngine, default (cloak-everything) policy."""
    return AnonymizationEngine(
        vault=Vault(mission="test-gemma-additive"),
        match_filter=_P.make_match_filter(_P.default_policy()),
    )


# A normal PROSE doc with clearly-detectable PII (email + IBAN) — GLiNER+regex
# masks it. Not a structured form. This is the #589 over-refusal fixture.
_PROSE_PII_DOC = (
    "Merci de contacter synthetic.contact@example-test.fr pour toute question. "
    "Le compte de reference est FR7630006000011234567890189 pour le virement. "
    "Nous restons a votre disposition pour convenir d un rendez vous prochainement."
)


# ---------------------------------------------------------------------------
# _gemma_mode() config reader
# ---------------------------------------------------------------------------

def test_gemma_mode_default_all(monkeypatch, tmp_path):
    # no config file at the pointed path -> default "all"
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(tmp_path / "nonexistent.json"))
    assert M._gemma_mode() == "all"


def test_gemma_mode_reads_config_off(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "off"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "off"


def test_gemma_mode_reads_config_hard(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "hard"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "hard"


def test_gemma_mode_reads_config_all(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "all"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "all"


def test_gemma_mode_malformed_defaults_all(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text("{not json")
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "all"


def test_gemma_mode_invalid_value_defaults_all(monkeypatch, tmp_path):
    # a value outside the allowed set -> fail toward more masking ("all")
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "sometimes"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "all"


def test_gemma_mode_missing_key_defaults_all(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"protected_folders": ["~/x"]}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "all"


def test_gemma_mode_non_string_value_defaults_all(monkeypatch, tmp_path):
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": 123}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    assert M._gemma_mode() == "all"


# ---------------------------------------------------------------------------
# _gemma_gate_decision() — the PURE safety helper (the key proof)
# ---------------------------------------------------------------------------

def test_gate_decision_all_eight_cases():
    """Full 8-case matrix: all/hard/off x form/prose (REFINEMENT: 4 outcomes).

    Security invariants:
      - off + form  == fail_closed (never skip -> would re-open the #589-B leak).
      - form + all|hard == run_failclosed (form is verified or refused, never additive).
      - prose + all == run_additive (Gemma ADDS on prose; failure is fail-OPEN, never
        a refusal — the #589 over-refusal bug this fix closes).
    """
    # all-mode: forms are verified-or-fail-closed; prose is additive (fail-open floor)
    assert M._gemma_gate_decision("all", True) == "run_failclosed"
    assert M._gemma_gate_decision("all", False) == "run_additive"
    # hard-mode: forms are verified-or-fail-closed; prose skips Gemma (legacy behavior)
    assert M._gemma_gate_decision("hard", True) == "run_failclosed"
    assert M._gemma_gate_decision("hard", False) == "skip"
    # off-mode: prose skips; FORM MUST FAIL CLOSED (never skip -> would leak #589-B)
    assert M._gemma_gate_decision("off", True) == "fail_closed"
    assert M._gemma_gate_decision("off", False) == "skip"


def test_gate_decision_off_form_is_never_skip():
    """Explicit security assertion: off+form is fail_closed, categorically not skip."""
    decision = M._gemma_gate_decision("off", True)
    assert decision == "fail_closed"
    assert decision != "skip"  # skip here would return an unverified form body = leak


def test_gate_decision_form_never_additive():
    """A structured form is NEVER routed to the additive (fail-open) path — in any mode
    where Gemma runs it must be run_failclosed (verify-or-refuse), preserving #589-B."""
    assert M._gemma_gate_decision("all", True) == "run_failclosed"
    assert M._gemma_gate_decision("hard", True) == "run_failclosed"
    for mode in ("all", "hard", "off"):
        assert M._gemma_gate_decision(mode, True) != "run_additive"


def test_gate_decision_prose_all_is_additive_not_failclosed():
    """prose + all is run_additive (fail-OPEN), NOT run_failclosed — the whole point of
    the #589 over-refusal fix: a Gemma failure on prose must not refuse a masked doc."""
    decision = M._gemma_gate_decision("all", False)
    assert decision == "run_additive"
    assert decision != "run_failclosed"


# ---------------------------------------------------------------------------
# Integration: off-mode form fails closed via _anonymise_text WITHOUT a daemon.
# ---------------------------------------------------------------------------

def test_off_mode_form_fails_closed_integration(monkeypatch, tmp_path):
    """off-mode + structured form -> _anonymise_text RAISES StructuredFormUnverifiedError,
    and _gemma_second_pass is NEVER called (sabotaged to explode if reached)."""
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "off"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    monkeypatch.setattr(M, "_is_structured_form", lambda t: True)

    def _boom(*a, **k):
        raise AssertionError("gemma_second_pass ran in off-mode — must not")

    monkeypatch.setattr(M, "_gemma_second_pass", _boom)

    text = (
        "Formulaire CERFA de souscription. Nom du souscripteur DURAND Theophile "
        "adresse 12 rue des Lilas 75012 Paris montant investi 25000 euros date "
        "de valeur 11 07 2026 signature du client requise avant envoi au service."
    )
    with pytest.raises(M.StructuredFormUnverifiedError):
        M._anonymise_text(text, filename_basename="cerfa.pdf")


# ---------------------------------------------------------------------------
# REFINEMENT (2026-07-11): prose + all mode — Gemma is ADDITIVE, fail-OPEN.
# The #589 over-refusal bug: a normal letter GLiNER+regex already masked, where
# Gemma is unreachable or adds nothing, must be RETURNED (masked), never refused.
# ---------------------------------------------------------------------------


def test_prose_all_gemma_unreachable_returns_masked_not_refused(monkeypatch, tmp_path):
    """prose + all mode + Gemma UNREACHABLE (extract call raises) -> _anonymise_text
    RETURNS the GLiNER+regex-masked body (fail-OPEN), does NOT raise. This is the
    core #589 over-refusal fix: a Gemma outage must never refuse a well-masked doc."""
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "all"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    # Prose, not a form.
    monkeypatch.setattr(M, "_is_structured_form", lambda t: False)

    # Gemma daemon unreachable: the extract call raises (as it would with no daemon).
    def _unreachable(*a, **k):
        raise ConnectionRefusedError("gemma daemon down")

    monkeypatch.setattr(M, "_gemma_extract_call", _unreachable)
    # A form second-pass here would be a bug: sabotage it to explode if reached.
    monkeypatch.setattr(
        M, "_gemma_second_pass",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("second_pass on prose")),
    )

    engine = _make_engine()
    vpath = tmp_path / "vault.json"
    monkeypatch.setattr(M, "_engine", lambda *a, **kw: (engine, vpath, True))

    out = M._anonymise_text(_PROSE_PII_DOC)

    # Not refused: a real masked body came back.
    assert isinstance(out, str)
    # GLiNER+regex floor still masked the PII (email + IBAN) even with Gemma down.
    assert "synthetic.contact@example-test.fr" not in out, f"email leaked:\n{out}"
    assert "FR7630006000011234567890189" not in out, f"IBAN leaked:\n{out}"
    assert "⟦" in out, f"expected masking tokens in fail-open output:\n{out}"


def test_prose_all_gemma_finds_extra_name_masks_it_additively(monkeypatch, tmp_path):
    """prose + all mode + Gemma returns an EXTRA name span (one GLiNER missed) ->
    that name is ALSO masked (additive works: Gemma adds on top of the floor)."""
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "all"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    monkeypatch.setattr(M, "_is_structured_form", lambda t: False)

    engine = _make_engine()
    vpath = tmp_path / "vault.json"
    monkeypatch.setattr(M, "_engine", lambda *a, **kw: (engine, vpath, True))

    # Run the engine once to see what GLiNER+regex leaves in clear, then pick a
    # literal token still present in the masked body for Gemma to "find" as extra.
    probe = _make_engine().anonymize(_PROSE_PII_DOC)
    masked = probe.anonymized
    # Choose a real word verbatim in the masked body that Gemma can additionally mask.
    extra = "disposition"
    assert extra in masked, (
        f"fixture drift: {extra!r} not in masked body; pick another literal:\n{masked}"
    )

    def _fake_extract(text):
        return [{"text": extra, "type": "NOM"}]

    monkeypatch.setattr(M, "_gemma_extract_call", _fake_extract)

    out = M._anonymise_text(_PROSE_PII_DOC)

    # The extra span Gemma found is now masked (additive), and the original PII floor holds.
    assert extra not in out, f"additive: Gemma's extra span not masked:\n{out}"
    assert "synthetic.contact@example-test.fr" not in out, f"email leaked:\n{out}"
    assert "⟦" in out, f"expected masking tokens:\n{out}"


def test_additive_pass_empty_spans_returns_floor_not_refused(monkeypatch, tmp_path):
    """prose + all + Gemma reachable but returns ZERO spans -> fail-OPEN: return the
    GLiNER+regex floor (contrast _gemma_second_pass, which fails CLOSED on zero spans)."""
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "all"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    monkeypatch.setattr(M, "_is_structured_form", lambda t: False)
    monkeypatch.setattr(M, "_gemma_extract_call", lambda text: [])

    engine = _make_engine()
    vpath = tmp_path / "vault.json"
    monkeypatch.setattr(M, "_engine", lambda *a, **kw: (engine, vpath, True))

    out = M._anonymise_text(_PROSE_PII_DOC)  # must NOT raise
    assert isinstance(out, str)
    assert "synthetic.contact@example-test.fr" not in out, f"email leaked:\n{out}"
    assert "⟦" in out, f"expected masking tokens:\n{out}"


def test_form_all_gemma_unreachable_still_fails_closed(monkeypatch, tmp_path):
    """Regression guard: a FORM in all-mode with Gemma unreachable STILL fails closed
    (run_failclosed path unchanged) — the additive fail-OPEN behavior is prose-ONLY."""
    cfg = tmp_path / "bubble-shield.json"
    cfg.write_text(json.dumps({"gemma_mode": "all"}))
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg))
    monkeypatch.setattr(M, "_is_structured_form", lambda t: True)

    def _unreachable(*a, **k):
        raise ConnectionRefusedError("gemma daemon down")

    monkeypatch.setattr(M, "_gemma_extract_call", _unreachable)

    engine = _make_engine()
    vpath = tmp_path / "vault.json"
    monkeypatch.setattr(M, "_engine", lambda *a, **kw: (engine, vpath, True))

    with pytest.raises(M.StructuredFormUnverifiedError):
        M._anonymise_text(_PROSE_PII_DOC, filename_basename="cerfa.pdf")
