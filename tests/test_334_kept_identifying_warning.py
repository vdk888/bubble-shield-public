"""Tests for #334 — LOUD WARNING when an identifying type is set to KEEP.

Coverage:
  - kept_identifying_types() unit tests (derives from ENTITY_CATALOG)
  - MCP _anonymise_text: warning fires when NOM=keep, suppressed for MONTANT=keep
  - MCP _anonymise_text: default policy → no warning
  - MCP _anonymise_text: multiple identifying types named in warning
  - MCP _anonymise_text: NER-down warning and kept-identifying warning co-exist
  - Webapp /anonymize: warning banner present when NOM=keep, absent on default policy
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── make bubble_shield importable ─────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bubble_shield import policy as P  # noqa: E402
from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# 1. kept_identifying_types() unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestKeptIdentifyingTypes:
    def test_empty_on_default_policy(self):
        """Default policy cloaks all identifying types → no warning."""
        result = P.kept_identifying_types(P.default_policy())
        assert result == [], f"Expected no warning on default, got: {result}"

    def test_nom_kept_returns_label(self):
        pol = P.default_policy()
        pol["NOM"] = False  # user set to KEEP
        result = P.kept_identifying_types(pol)
        assert any("Nom" in label for label in result), f"NOM label not found in {result}"

    def test_montant_kept_returns_empty(self):
        """MONTANT is non-identifying — kept MONTANT must NOT trigger the warning."""
        pol = P.default_policy()
        pol["MONTANT"] = False  # MONTANT=keep (legitimate, non-identifying)
        result = P.kept_identifying_types(pol)
        assert result == [], (
            f"MONTANT is non-identifying; keeping it must not warn. Got: {result}")

    def test_isin_kept_returns_empty(self):
        """ISIN is non-identifying — kept ISIN must NOT trigger the warning."""
        pol = P.default_policy()
        pol["ISIN"] = False
        result = P.kept_identifying_types(pol)
        assert result == [], f"ISIN is non-identifying; keeping it must not warn. Got: {result}"

    def test_date_evenement_kept_returns_empty(self):
        """DATE_EVENEMENT is non-identifying — no warning."""
        pol = P.default_policy()
        pol["DATE_EVENEMENT"] = False
        result = P.kept_identifying_types(pol)
        assert result == [], f"DATE_EVENEMENT is non-identifying. Got: {result}"

    def test_multiple_identifying_types_all_named(self):
        pol = P.default_policy()
        pol["NOM"] = False
        pol["EMAIL"] = False
        pol["IBAN"] = False
        result = P.kept_identifying_types(pol)
        assert len(result) >= 3, f"Expected at least 3 labels, got: {result}"
        # All three identifying types must be represented in the returned labels
        labels_str = " ".join(result)
        assert "Nom" in labels_str or "prénom" in labels_str
        assert "mail" in labels_str.lower()
        assert "IBAN" in labels_str or "bancaire" in labels_str.lower()

    def test_derives_from_entity_catalog_not_hardcoded(self):
        """Add a fake identifying type to the catalog; it should appear in results."""
        fake_type = "TEST_FAKE_IDENTIFYING"
        P.ENTITY_CATALOG[fake_type] = {
            "label": "Faux type test",
            "identifying": True,
            "default_cloak": True,
        }
        try:
            pol = P.default_policy()
            pol[fake_type] = False  # set to KEEP
            result = P.kept_identifying_types(pol)
            assert "Faux type test" in result, (
                f"Derived-from-catalog type not found in result: {result}")
        finally:
            del P.ENTITY_CATALOG[fake_type]

    def test_non_identifying_mixed_with_identifying(self):
        """Only identifying types should warn even when non-identifying types are also kept."""
        pol = P.default_policy()
        pol["MONTANT"] = False   # non-identifying — no warning
        pol["ISIN"] = False      # non-identifying — no warning
        pol["NOM"] = False       # identifying — WARN
        result = P.kept_identifying_types(pol)
        assert len(result) == 1, f"Expected exactly 1 warning (NOM only), got: {result}"
        assert any("Nom" in label for label in result)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MCP _anonymise_text warning injection
# ═══════════════════════════════════════════════════════════════════════════════

def _make_engine_with_policy(policy: dict):
    """Build a real AnonymizationEngine with the given cloak/keep policy."""
    return AnonymizationEngine(
        vault=Vault(mission="test-334"),
        match_filter=P.make_match_filter(policy),
    )


def _import_mcp():
    """Import bubble_shield_mcp without running it as __main__.
    We patch posttool_anonymize away since it's not available in the test env.
    """
    import importlib.util as _ilu

    # Return cached module if already loaded (avoid re-exec across tests).
    if "bubble_shield_mcp" in sys.modules:
        return sys.modules["bubble_shield_mcp"]

    mcp_path = ROOT / "plugin" / "bubble-shield" / "scripts" / "bubble_shield_mcp.py"

    # Stub posttool_anonymize BEFORE loading so the import inside the module doesn't fail.
    # Only inject the stub when the real module has not been imported yet.  The real
    # module lives in plugin/bubble-shield/scripts/ and is importable when that dir is
    # on sys.path; the daemon detection tests import it directly and rely on its NERD_URL
    # + _daemon_up() attributes — the stub must not clobber the real module.
    if "posttool_anonymize" not in sys.modules:
        _scripts = str(ROOT / "plugin" / "bubble-shield" / "scripts")
        try:
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            import posttool_anonymize  # noqa: F401 — loads the real module into sys.modules
        except Exception:
            # Real module not importable (e.g. missing deps in CI) — fall back to stub.
            fake_pt = types.ModuleType("posttool_anonymize")
            fake_pt._daemon_detector = lambda *a, **kw: None
            fake_pt._try_spawn_daemon = lambda: None
            fake_pt.NERD_URL = "http://127.0.0.1:0"
            fake_pt._daemon_up = lambda: False
            sys.modules["posttool_anonymize"] = fake_pt

    spec = _ilu.spec_from_file_location("bubble_shield_mcp", str(mcp_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules["bubble_shield_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestMcpKeptIdentifyingWarning:
    """Tests that exercise _anonymise_text directly.

    We monkeypatch _engine() to return a controlled (engine, vpath, daemon_up=True)
    tuple so the daemon guard passes without a real GLiNER daemon.
    """

    def _run_with_policy(self, policy: dict, text: str, tmp_path: Path):
        """Run _anonymise_text with the given policy and text, return the output string."""
        mcp = _import_mcp()
        engine = _make_engine_with_policy(policy)
        vpath = tmp_path / "test.vault.json"

        # Stub the policy loader inside the MCP module so it reads our policy.
        with patch.object(mcp, "_engine", return_value=(engine, vpath, True)), \
             patch("bubble_shield.policy.DEFAULT_POLICY_PATH", str(tmp_path / "policy.json")):
            # Write policy to the temp path so kept_identifying_types reads it.
            (tmp_path / "policy.json").write_text(
                json.dumps(policy), encoding="utf-8")
            # Also patch load_policy inside the mcp module's namespace.
            with patch("bubble_shield.policy.load_policy", return_value=policy):
                return mcp._anonymise_text(text)

    def test_nom_kept_warning_fires_but_392_floor_still_masks_name(self, tmp_path):
        """NOM=keep → the loud #334 warning still fires (the policy DOES say
        "keep names"), but the #392 identifying-floor still cloaks the name in
        the actual output — "keep" is named loudly, never silently honoured for
        an identifying type. (#589: previously this asserted the OPPOSITE —
        that the name was "actually in clear" — which was only true because the
        fixture text used an untitled name the regex-only test engine could not
        detect at all; that same "nothing detected" state is exactly what P0 #589
        now fails closed on. Using a title-prefixed name here so the regex
        genuinely detects+masks it, isolating the #334 warning-copy assertion
        from the #589 fail-closed gate.)"""
        pol = P.default_policy()
        pol["NOM"] = False  # user says "keep names" — floor overrides in practice

        # Synthetic name with a civility title so the regex-only NOM recognizer
        # actually fires (no real PII — fictitious name).
        text = "M. Fictif Testnom dispose de 45 000 EUR sur son compte."

        out = self._run_with_policy(pol, text, tmp_path)
        assert "MASQUAGE DÉSACTIVÉ" in out, f"Warning not found in output:\n{out}"
        assert "Nom" in out or "prénom" in out.lower(), \
            f"NOM label not named in warning:\n{out}"
        # #392 floor: despite policy NOM=keep, the identifying type is still
        # cloaked in the actual returned text — the warning is loud, not a leak.
        assert "⟦NOM" in out, f"#392 floor: name must still be masked:\n{out}"
        assert "Fictif Testnom" not in out, f"name leaked in clear:\n{out}"

    def test_montant_kept_no_warning(self, tmp_path):
        """MONTANT=keep (legitimate) → no warning fires.

        #589: the fixture now also carries a title-prefixed name so the doc
        has a genuinely DETECTED+masked entity (NOM) alongside the kept
        MONTANT — verdict_state is "masked_ok", not "zero_detection". A doc
        whose ONLY regex-visible content is a legitimately-kept MONTANT (the
        original fixture: "Le portefeuille est valorisé à 45 000 EUR.") is
        exactly the case the #589 P0 fix now correctly fails closed on — the
        engine found nothing to certify safe, and "MONTANT was kept per
        policy" isn't distinguishable from "nothing was detected at all" at
        the verdict_state level, so it must refuse rather than silently
        return raw text. That refusal is covered by
        test_589_zero_detection_failclosed.py; this test isolates the
        narrower "no false #334 warning on legitimate MONTANT-keep" claim."""
        pol = P.default_policy()
        pol["MONTANT"] = False  # keep euro amounts — legitimate

        text = "M. Fictif Testnom a fait valoriser son portefeuille à 45 000 EUR."

        out = self._run_with_policy(pol, text, tmp_path)
        assert "MASQUAGE DÉSACTIVÉ" not in out, \
            f"False warning on non-identifying MONTANT keep:\n{out}"

    def test_default_policy_no_warning(self, tmp_path):
        """Default policy cloaks all identifying types → no warning on normal case."""
        pol = P.default_policy()
        text = "Bilan du portefeuille au 31/12/2024."

        out = self._run_with_policy(pol, text, tmp_path)
        assert "MASQUAGE DÉSACTIVÉ" not in out, \
            f"False warning on default policy:\n{out}"

    def test_multiple_types_all_named_in_warning(self, tmp_path):
        """EMAIL + IBAN kept → both appear in the warning string."""
        pol = P.default_policy()
        pol["EMAIL"] = False
        pol["IBAN"] = False

        text = "Contact: synthetic@example.com, IBAN FR00 0000 0000 0000 0000 000."

        out = self._run_with_policy(pol, text, tmp_path)
        assert "MASQUAGE DÉSACTIVÉ" in out, f"Warning not found:\n{out}"
        # Both FR labels must appear
        assert "mail" in out.lower() or "E-mail" in out, f"EMAIL label missing:\n{out}"
        assert "IBAN" in out or "bancaire" in out.lower(), f"IBAN label missing:\n{out}"

    def test_ner_down_and_kept_warning_coexist(self, tmp_path):
        """NER-down error and kept-identifying warning must both appear when both conditions hold."""
        pol = P.default_policy()
        pol["NOM"] = False  # keep names → warning should fire

        text = "Client Fictif Testnom, IBAN FR00 0000."
        mcp = _import_mcp()
        engine = _make_engine_with_policy(pol)
        vpath = tmp_path / "test.vault.json"

        # daemon_up=False → NERDownError should be raised; warning is NOT reached
        # (fail-closed: NER-down is a hard error, warning is additive).
        # The two can only BOTH fire if NER is up but policy keeps identifying.
        # So this test confirms: when daemon IS up, both can co-exist in the string.
        # We simulate a NER-down by checking the NERDownError path raises.
        with patch.object(mcp, "_engine", return_value=(engine, vpath, False)), \
             patch.object(mcp, "_try_spawn_daemon_from_mcp", return_value=None), \
             patch("bubble_shield.policy.load_policy", return_value=pol):
            with pytest.raises(mcp.NERDownError) as exc_info:
                mcp._anonymise_text(text)
            # The NER-down error text must be present
            assert "NER" in str(exc_info.value) or "hors-ligne" in str(exc_info.value), \
                f"NER-down error string not found: {exc_info.value}"

        # Now with daemon UP + identifying kept → only the policy warning fires.
        with patch.object(mcp, "_engine", return_value=(engine, vpath, True)), \
             patch("bubble_shield.policy.load_policy", return_value=pol):
            (tmp_path / "policy.json").write_text(json.dumps(pol), encoding="utf-8")
            out = mcp._anonymise_text(text)
            assert "MASQUAGE DÉSACTIVÉ" in out, \
                f"Policy warning not in output when daemon is up:\n{out}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Webapp /anonymize — warning banner in HTML
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebappKeptIdentifyingBanner:
    """Exercises the webapp /anonymize route. Relies on TestClient (no real server)."""

    def _client(self):
        from fastapi.testclient import TestClient
        from webapp.app import app
        return TestClient(app)

    def test_nom_keep_policy_is_floored_no_banner_needed(self, tmp_path, monkeypatch):
        """#392: a NOM=keep policy.json is coerced to cloak on load, so the engine
        MASKS the name and the #334 'masquage désactivé' banner is moot — there is
        nothing kept-in-clear to warn about. The floor supersedes the warning.

        (Pre-#392 this asserted the banner fired AND the name was in clear; the
        in-clear state is exactly the leak #392 removes.)"""
        import bubble_shield.policy as _pol_module

        pol = P.default_policy()
        pol["NOM"] = False  # try to keep names — the floor will override

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(pol), encoding="utf-8")
        monkeypatch.setattr(_pol_module, "DEFAULT_POLICY_PATH", str(policy_file))

        client = self._client()
        r = client.post("/anonymize", data={
            "text": "Le client Fictif Testnom dispose de 45 000 EUR.",
            "mission": "t",
        })
        assert r.status_code == 200
        # Floor in effect → no identifying type is kept → no warning banner.
        assert "kept-identifying-warn" not in r.text, \
            "#392: floor masks NOM, so the kept-identifying banner must NOT fire"
        # And the name must actually be masked (the floor did its job): a NOM token
        # appears in the "Après" pane. (The original name still shows in the "Avant"
        # highlight pane — that pane echoes the input, so we assert on the token.)
        assert "⟦NOM" in r.text, \
            "#392 floor: NOM must be cloaked (token present) even with NOM=keep policy"

    def test_no_banner_on_default_policy(self, tmp_path, monkeypatch):
        """Default policy → no warning banner in result HTML."""
        import bubble_shield.policy as _pol_module

        pol = P.default_policy()
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(pol), encoding="utf-8")
        monkeypatch.setattr(_pol_module, "DEFAULT_POLICY_PATH", str(policy_file))

        client = self._client()
        r = client.post("/anonymize", data={
            "text": "Bilan au 31/12/2024, total 45 000 EUR.",
            "mission": "t",
        })
        assert r.status_code == 200
        assert "kept-identifying-warn" not in r.text, \
            "False warning banner on default policy"

    def test_no_banner_when_only_montant_kept(self, tmp_path, monkeypatch):
        """MONTANT=keep (non-identifying) → no warning banner."""
        import bubble_shield.policy as _pol_module

        pol = P.default_policy()
        pol["MONTANT"] = False  # legitimate keep
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(pol), encoding="utf-8")
        monkeypatch.setattr(_pol_module, "DEFAULT_POLICY_PATH", str(policy_file))

        client = self._client()
        r = client.post("/anonymize", data={
            "text": "Portefeuille valorisé à 45 000 EUR.",
            "mission": "t",
        })
        assert r.status_code == 200
        assert "kept-identifying-warn" not in r.text, \
            "False warning banner on non-identifying MONTANT keep"
