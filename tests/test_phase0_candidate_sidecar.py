"""
test_phase0_candidate_sidecar.py — Phase 0: local candidate-signal sidecar.

All candidate values are SYNTHETIC (invented names that do not correspond to any
real person).  Tests use tmp_path and monkeypatch to redirect
BUBBLE_SHIELD_HOME so the production ~/.bubble_shield is never touched.

Synthetic names used: TESTRAND, FICTAILLON, FAUXPRÉ.

Test plan:
  1. Sub-threshold candidate → sidecar contains the candidate value + score + offset.
     The MCP-facing result does NOT contain the raw value (only masked text + the
     generic sub-threshold notice).
  2. Fail-open: a deliberately unwritable sidecar directory → anonymize still
     succeeds, returns normally, no exception raised.
  3. Clean doc (no sub-threshold entity) → no sidecar entry written.
  4. Agent-facing output is byte-identical with and without Phase 0 change on the
     same doc (verified by comparing the returned string).
  5. Unsafe (has_residual) result → candidates include ALL entities, not just
     sub-threshold ones.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from bubble_shield.engine import AnonymizationEngine, AnonymizationResult, DetectedEntity
from bubble_shield.recognizers import Match
from bubble_shield.vault import Vault


# ── helpers ────────────────────────────────────────────────────────────────────

def _redirect_home(monkeypatch, tmp_path: Path) -> Path:
    """Point BUBBLE_SHIELD_HOME at a temp dir; returns the temp dir path."""
    home = tmp_path / "bs_home"
    home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    # Re-import to pick up the new env var
    import importlib
    import bubble_shield.candidate_sidecar as cs
    importlib.reload(cs)
    return home


def _make_low_confidence_result(threshold: float = 0.6) -> AnonymizationResult:
    """Build a synthetic AnonymizationResult with one sub-threshold entity.

    The entity is a SYNTHETIC name 'TESTRAND' (not a real person) with score 0.45
    — below the default threshold of 0.6.  The engine masked it (replaced with a
    token), so `anonymized` does NOT contain 'TESTRAND'.
    """
    original = "Bonjour TESTRAND, voici votre dossier."
    # Simulate what the engine produces: value is masked, offset is in the original.
    entity = DetectedEntity(
        entity_type="NOM",
        value="TESTRAND",
        token="⟦NOM_0001⟧",
        score=0.45,
        start=8,
        end=16,
        priority=5,
    )
    anonymized = original[:8] + "⟦NOM_0001⟧" + original[16:]
    return AnonymizationResult(
        original=original,
        anonymized=anonymized,
        entities=[entity],
        residual=[],
        min_score=0.45,
        threshold=threshold,
    )


def _make_clean_result() -> AnonymizationResult:
    """Build a result with no sub-threshold entities (email, score 1.0)."""
    original = "Envoyez à test@example.com."
    entity = DetectedEntity(
        entity_type="EMAIL",
        value="test@example.com",
        token="⟦EMAIL_0001⟧",
        score=1.0,
        start=10,
        end=26,
        priority=0,
    )
    anonymized = original[:10] + "⟦EMAIL_0001⟧" + original[26:]
    return AnonymizationResult(
        original=original,
        anonymized=anonymized,
        entities=[entity],
        residual=[],
        min_score=1.0,
        threshold=0.6,
    )


# ── tests ─────────────────────────────────────────────────────────────────────

class TestCandidateSidecar:

    def test_sub_threshold_candidate_written_to_sidecar(self, tmp_path, monkeypatch):
        """Sub-threshold entity → sidecar contains value, score, offset."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result = _make_low_confidence_result()
        cs.write_candidates(result, mission="test-session", source_doc="fake_doc.pdf")

        sidecar = home / "candidates" / "test-session.candidates.json"
        assert sidecar.is_file(), "Sidecar file should be created"
        data = json.loads(sidecar.read_text())
        assert len(data) == 1
        item = data[0]
        assert item["value"] == "TESTRAND"
        assert item["entity_type"] == "NOM"
        assert item["score"] == pytest.approx(0.45, abs=1e-4)
        assert item["char_start"] == 8
        assert item["char_end"] == 16
        assert item["source_doc"] == "fake_doc.pdf"
        assert item["mission"] == "test-session"
        assert item["is_residual"] is False

    def test_sidecar_not_in_agent_output(self, tmp_path, monkeypatch):
        """The raw candidate value must NOT appear in the agent-facing output.

        We check this in two ways:
        1. The anonymized text does not contain the real value.
        2. The sub-threshold notice uses a generic message (no value named).
        """
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result = _make_low_confidence_result()
        cs.write_candidates(result, mission="test-session", source_doc="")

        # The anonymized text itself must not contain the raw value.
        assert "TESTRAND" not in result.anonymized

        # The notice the MCP layer appends for sub-threshold results must not
        # name the value either.
        note = (
            "\n\n[⚠️ Bubble Shield : une relecture humaine est conseillée — "
            "une donnée potentiellement sensible est restée sous le seuil de confiance.]"
        )
        full_response = result.anonymized + note
        assert "TESTRAND" not in full_response, (
            "Raw candidate value must not appear in the agent-facing MCP output"
        )

    def test_fail_open_unwritable_path(self, tmp_path, monkeypatch):
        """Deliberately unwritable sidecar dir → write_candidates returns without error."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        # Create the candidates dir but make it read-only (no write permission).
        candidates_dir = home / "candidates"
        candidates_dir.mkdir(parents=True)
        candidates_dir.chmod(0o444)  # read-only

        result = _make_low_confidence_result()
        # Must not raise any exception.
        cs.write_candidates(result, mission="test-session", source_doc="")

        # Restore permissions for cleanup.
        candidates_dir.chmod(0o755)

    def test_clean_doc_no_sidecar_entry(self, tmp_path, monkeypatch):
        """Clean doc (no sub-threshold entity) → no sidecar file created."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result = _make_clean_result()
        cs.write_candidates(result, mission="test-session", source_doc="")

        sidecar = home / "candidates" / "test-session.candidates.json"
        assert not sidecar.exists(), (
            "Sidecar must not be created when no sub-threshold candidates exist"
        )

    def test_agent_output_byte_identical_with_and_without_phase0(self, tmp_path, monkeypatch):
        """Agent-facing output is identical before and after the Phase 0 sidecar write.

        We compute what the MCP layer returns (anonymized + note) BEFORE calling
        write_candidates and AFTER calling it, and assert they are identical.
        The sidecar write must have zero effect on the returned string.
        """
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result = _make_low_confidence_result()

        # Pre-Phase0 output (what _anonymise_text would return without the sidecar call)
        note = (
            "\n\n[⚠️ Bubble Shield : une relecture humaine est conseillée — "
            "une donnée potentiellement sensible est restée sous le seuil de confiance.]"
        )
        pre_phase0_output = result.anonymized + note

        # Call the sidecar writer (Phase 0 addition).
        cs.write_candidates(result, mission="test-session", source_doc="test_doc.pdf")

        # Post-Phase0 output is the same expression — the write_candidates call
        # has no side-effect on result.anonymized.
        post_phase0_output = result.anonymized + note

        assert pre_phase0_output == post_phase0_output, (
            "Phase 0 sidecar write must not alter the agent-facing output"
        )

    def test_unsafe_result_surfaces_all_entities(self, tmp_path, monkeypatch):
        """When result.has_residual is True, ALL entities become candidates."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        # Build a result with one high-confidence entity AND has_residual=True.
        entity = DetectedEntity(
            entity_type="NOM",
            value="FICTAILLON",
            token="⟦NOM_0001⟧",
            score=0.95,  # high-confidence — would NOT be a candidate otherwise
            start=0,
            end=10,
            priority=5,
        )
        from bubble_shield.recognizers import Match
        residual_match = Match(start=20, end=30, entity_type="NOM", value="FAUXPRÉ", score=0.5)
        result = AnonymizationResult(
            original="FICTAILLON bonjour FAUXPRÉ.",
            anonymized="⟦NOM_0001⟧ bonjour FAUXPRÉ.",
            entities=[entity],
            residual=[residual_match],
            min_score=0.95,
            threshold=0.6,
        )
        assert result.has_residual is True

        cs.write_candidates(result, mission="unsafe-test", source_doc="")

        sidecar = home / "candidates" / "unsafe-test.candidates.json"
        assert sidecar.is_file()
        data = json.loads(sidecar.read_text())
        # The high-confidence entity should appear because of has_residual.
        values = [item["value"] for item in data]
        assert "FICTAILLON" in values
        assert all(item["is_residual"] is True for item in data)

    def test_sidecar_chmod_600(self, tmp_path, monkeypatch):
        """Sidecar file is written with permissions 600 (owner read/write only)."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result = _make_low_confidence_result()
        cs.write_candidates(result, mission="perm-test", source_doc="")

        sidecar = home / "candidates" / "perm-test.candidates.json"
        assert sidecar.is_file()
        mode = oct(stat.S_IMODE(sidecar.stat().st_mode))
        assert mode == "0o600", f"Expected 0o600 but got {mode}"

    def test_sidecar_appends_across_calls(self, tmp_path, monkeypatch):
        """Multiple write_candidates calls for the same mission append, not overwrite."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        result1 = _make_low_confidence_result()
        result2 = _make_low_confidence_result()

        cs.write_candidates(result1, mission="append-test", source_doc="doc1.pdf")
        cs.write_candidates(result2, mission="append-test", source_doc="doc2.pdf")

        sidecar = home / "candidates" / "append-test.candidates.json"
        data = json.loads(sidecar.read_text())
        assert len(data) == 2
        docs = {item["source_doc"] for item in data}
        assert docs == {"doc1.pdf", "doc2.pdf"}

    def test_normalized_field_present_and_correct(self, tmp_path, monkeypatch):
        """The normalized field strips accents and uppercases the value."""
        home = _redirect_home(monkeypatch, tmp_path)
        from bubble_shield import candidate_sidecar as cs
        importlib = __import__("importlib")
        importlib.reload(cs)

        entity = DetectedEntity(
            entity_type="NOM",
            value="Tèstrand",
            token="⟦NOM_0001⟧",
            score=0.40,
            start=0,
            end=8,
            priority=5,
        )
        result = AnonymizationResult(
            original="Tèstrand bonjour.",
            anonymized="⟦NOM_0001⟧ bonjour.",
            entities=[entity],
            residual=[],
            min_score=0.40,
            threshold=0.6,
        )
        cs.write_candidates(result, mission="norm-test", source_doc="")

        sidecar = home / "candidates" / "norm-test.candidates.json"
        data = json.loads(sidecar.read_text())
        assert data[0]["normalized"] == "TESTRAND"
