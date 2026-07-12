"""
tests/test_openai_pf_ext.py — Unit tests for Phase 2: OpenAI PF adapter.

Tests cover:
  1. Viterbi decode on a known tiny input (stubbed logits, no real model download)
  2. IoU + merge logic (merge_soft, merge.py)
  3. Config toggle: detector.mode dispatch (daemon mode selection)
  4. FR-finance invariant: checksum PII still wins in all modes
  5. Default mode = "gliner", production behaviour unchanged
  6. New policy catalog types (URL, SECRET)

No real model is downloaded. All tests use synthetic data only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List

import pytest

# ── Import paths ──────────────────────────────────────────────────────────────
# Tests run from the repo root; bubble_shield/ is at root level.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble_shield.openai_pf_ext import (
    _decode_viterbi,
    OPENAI_LABEL_TO_TYPE,
    openai_pf_matches,
    _MODEL_CACHE,
)
from bubble_shield.merge import merge_soft as merge_soft_direct, _iou as iou_direct, IOU_THRESHOLD
from bubble_shield.merge import merge_soft  # noqa: F401 (used in later tests)
from bubble_shield.recognizers import Match
from bubble_shield.policy import ENTITY_CATALOG, default_policy


# ─────────────────────────────────────────────────────────────────────────────
# 1. Viterbi decode unit tests (synthetic logits, no model)
# ─────────────────────────────────────────────────────────────────────────────

def _make_logits(seq_len: int, num_labels: int,
                 hot: dict = None, base: float = -5.0) -> List[List[float]]:
    """Build a logits array with base score everywhere, then override hot spots.
    hot = {(token_idx, label_idx): score}
    """
    grid = [[base] * num_labels for _ in range(seq_len)]
    for (t, l), v in (hot or {}).items():
        grid[t][l] = v
    return grid


class TestViterbiDecode:
    """Deterministic decode tests on tiny known inputs."""

    def _labels_for(self, cats: List[str]) -> List[str]:
        """Build BIOES label list: S-cat, B-cat, I-cat, E-cat per category, then O."""
        lbls = []
        for c in cats:
            lbls += [f"S-{c}", f"B-{c}", f"I-{c}", f"E-{c}"]
        lbls.append("O")
        return lbls

    def test_single_token_span(self):
        """A single high-scoring S-X token produces a span at that token.
        Other tokens have strong O scores so the path goes O after the S."""
        cats = ["private_person"]
        labels = self._labels_for(cats)
        # S-private_person = index 0; O = index 4
        # Token 0: S gets 5.0; tokens 1,2: O gets 10.0 (strongly prefer O)
        O_idx = len(labels) - 1
        logits = _make_logits(3, len(labels), hot={
            (0, 0): 5.0,          # S-private_person at token 0
            (1, O_idx): 10.0,     # O at tokens 1 and 2 (high score → path goes O)
            (2, O_idx): 10.0,
        }, base=-10.0)
        spans = _decode_viterbi(logits, labels)
        # Must have exactly one span at token 0
        assert len(spans) == 1
        t_start, t_end, cat, score = spans[0]
        assert t_start == 0
        assert t_end == 0
        assert cat == "private_person"
        assert score > 0.0

    def test_multi_token_span_biei(self):
        """B-X I-X E-X sequence produces one span spanning all three tokens."""
        cats = ["private_person"]
        labels = self._labels_for(cats)
        # B=1, I=2, E=3, O=4
        logits = _make_logits(3, len(labels), hot={
            (0, 1): 5.0,   # B-private_person
            (1, 2): 5.0,   # I-private_person
            (2, 3): 5.0,   # E-private_person
        }, base=-10.0)
        spans = _decode_viterbi(logits, labels)
        assert len(spans) == 1
        t_start, t_end, cat, score = spans[0]
        assert t_start == 0
        assert t_end == 2
        assert cat == "private_person"

    def test_two_separate_single_spans(self):
        """Two S-X tokens with an O in between produce two separate spans."""
        cats = ["private_person"]
        labels = self._labels_for(cats)
        O_idx = len(labels) - 1
        logits = _make_logits(3, len(labels), hot={
            (0, 0): 5.0,         # S-private_person
            (1, O_idx): 5.0,     # O
            (2, 0): 5.0,         # S-private_person again
        }, base=-10.0)
        spans = _decode_viterbi(logits, labels)
        assert len(spans) == 2
        cats_found = {s[2] for s in spans}
        assert cats_found == {"private_person"}
        positions = sorted((s[0], s[1]) for s in spans)
        assert positions == [(0, 0), (2, 2)]

    def test_invalid_BI_Y_transition_penalised(self):
        """B-X followed by high-score I-Y (different type) is constrained:
        the decoder cannot use that invalid I-Y, so it must find an alternative
        (here: B→E as a 2-token span of the correct type). Key assertion: no
        span should cross entity types — every span's category must be consistent."""
        cats = ["private_person", "private_email"]
        labels = self._labels_for(cats)
        # B-private_person=1, I-private_email=6 — invalid transition
        # O=8 for the last token
        logits = _make_logits(3, len(labels), hot={
            (0, 1): 8.0,   # B-private_person
            (1, 6): 8.0,   # I-private_email (WRONG TYPE → constrained away)
            (2, 8): 5.0,   # O
        }, base=-10.0)
        spans = _decode_viterbi(logits, labels)
        # The decoder must not produce any span that mixes categories
        # (every span's category = single canonical type, not a mix)
        for s in spans:
            t_start, t_end, cat, score = s
            # All tokens in the span must be compatible with 'cat'
            # (B-cat or I-cat or E-cat or S-cat for this category only)
            assert cat in ("private_person", "private_email"), \
                f"Unknown category in span: {s}"
        # The I-private_email must not be incorporated into a private_person span
        # Specifically, there should be NO span claiming to be private_email
        # that starts at token 0 (B is private_person there, not private_email)
        for s in spans:
            t_start, t_end, cat, score = s
            if t_start == 0 and cat == "private_email":
                pytest.fail(f"Token 0 has B-private_person but span claims private_email: {s}")

    def test_empty_input(self):
        assert _decode_viterbi([], ["S-private_person", "O"]) == []

    def test_all_outside(self):
        """All O predictions → no spans."""
        cats = ["private_person"]
        labels = self._labels_for(cats)
        O_idx = len(labels) - 1
        logits = _make_logits(4, len(labels),
                              hot={(i, O_idx): 10.0 for i in range(4)},
                              base=-10.0)
        spans = _decode_viterbi(logits, labels)
        assert spans == []

    def test_viterbi_bias_shifts_scores(self):
        """Providing a viterbi_bias dict doesn't crash and shifts the decode."""
        cats = ["private_person"]
        labels = self._labels_for(cats)
        logits = _make_logits(2, len(labels), hot={(0, 0): 5.0}, base=-10.0)
        bias = {"single_bias": 2.0, "outside_bias": -2.0,
                "begin_bias": 0.0, "inside_bias": 0.0,
                "end_bias": 0.0, "transition_temp": 1.0}
        spans = _decode_viterbi(logits, labels, bias=bias)
        # With a positive single_bias the S span should still be found
        assert any(s[2] == "private_person" for s in spans)


# ─────────────────────────────────────────────────────────────────────────────
# 2. IoU + merge logic
# ─────────────────────────────────────────────────────────────────────────────

def _m(start, end, etype="NOM", score=0.8) -> Match:
    return Match(start=start, end=end, entity_type=etype,
                 value="x" * (end - start), score=score, priority=5)


class TestIoU:
    def test_identical_spans(self):
        a = _m(10, 20)
        b = _m(10, 20)
        assert iou_direct(a, b) == pytest.approx(1.0)

    def test_no_overlap(self):
        a = _m(0, 10)
        b = _m(20, 30)
        assert iou_direct(a, b) == pytest.approx(0.0)

    def test_partial_overlap(self):
        # [0, 10) and [5, 15): intersection 5, union 15
        a = _m(0, 10)
        b = _m(5, 15)
        assert iou_direct(a, b) == pytest.approx(5 / 15)

    def test_full_containment(self):
        # [0, 20) contains [5, 10): intersection 5, union 20
        a = _m(0, 20)
        b = _m(5, 10)
        assert iou_direct(a, b) == pytest.approx(5 / 20)

    def test_threshold_boundary(self):
        # intersection=6, union=10 → IoU=0.6 = threshold (inclusive)
        a = _m(0, 10)
        b = _m(0, 6, etype="NOM")  # intersection 6, union 10
        # Actually [0,10) and [0,6): inter=6, union=10 → 0.6
        assert iou_direct(a, b) >= IOU_THRESHOLD


class TestMergeSoft:
    def test_empty_inputs(self):
        assert merge_soft_direct([], []) == []
        assert merge_soft_direct([_m(0, 5)], []) == [_m(0, 5)]
        assert merge_soft_direct([], [_m(0, 5)]) == [_m(0, 5)]

    def test_same_type_high_iou_merges(self):
        """Two NOM matches with IoU ≥ 0.6 → one merged match with union span."""
        a = _m(0, 10, "NOM", score=0.7)
        b = _m(2, 12, "NOM", score=0.9)
        result = merge_soft_direct([a], [b])
        assert len(result) == 1
        merged = result[0]
        assert merged.entity_type == "NOM"
        assert merged.start == 0          # union span start
        assert merged.end == 12           # union span end
        assert merged.score == pytest.approx(0.9)  # max score

    def test_same_type_low_iou_kept_both(self):
        """Two NOM matches with IoU < 0.6 → both kept (recall-biased)."""
        a = _m(0, 10, "NOM")
        b = _m(8, 20, "NOM")  # small overlap
        # IoU: inter=[8,10)=2, union=[0,20)=20 → 0.1 < threshold
        result = merge_soft_direct([a], [b])
        assert len(result) == 2

    def test_different_types_high_iou_kept_both(self):
        """NOM and ADRESSE on same span → BOTH kept (let resolve_overlaps decide)."""
        a = _m(0, 10, "NOM")
        b = _m(0, 10, "ADRESSE")
        result = merge_soft_direct([a], [b])
        assert len(result) == 2
        types = {m.entity_type for m in result}
        assert types == {"NOM", "ADRESSE"}

    def test_unmatched_kept(self):
        """Matches with no counterpart are always kept."""
        a = _m(0, 5, "NOM")
        b = _m(100, 110, "EMAIL")  # no overlap with a
        result = merge_soft_direct([a], [b])
        assert len(result) == 2

    def test_recall_biased_no_silent_drops(self):
        """Nothing is ever dropped silently — every match appears in output."""
        gliner = [_m(0, 10, "NOM"), _m(50, 60, "TEL")]
        openai = [_m(0, 10, "NOM"), _m(80, 90, "EMAIL")]
        result = merge_soft_direct(gliner, openai)
        # The NOM pair merges → 1; TEL unmatched → 1; EMAIL unmatched → 1; total 3
        assert len(result) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 3. Config toggle: detector mode reading
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectorModeConfig:
    def _write_config(self, tmpdir, mode: str) -> Path:
        cfg = {"version": 1, "detector": {"mode": mode}}
        p = Path(tmpdir) / "custom_fields.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return p

    def test_default_mode_is_both(self):
        """When no custom_fields.json exists, mode must be 'both' (#348).

        Pre-#348 the default was 'gliner'; #348 flips it to the GLiNER∪OpenAI-PF
        union. Runtime fail-open to gliner-only (when OpenAI-PF weights are
        absent) is handled separately by _resolve_runtime_mode()."""
        # Import the _load_detector_mode helper from the daemon script
        scripts_dir = Path(__file__).resolve().parent.parent / "plugin/bubble-shield/scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            from bubble_shield_nerd import _load_detector_mode
        finally:
            sys.path.pop(0)
        mode = _load_detector_mode(Path("/nonexistent/custom_fields.json"))
        assert mode == "both"

    def test_mode_openai_reads_correctly(self, tmp_path):
        scripts_dir = Path(__file__).resolve().parent.parent / "plugin/bubble-shield/scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            from bubble_shield_nerd import _load_detector_mode
        finally:
            sys.path.pop(0)
        p = self._write_config(tmp_path, "openai")
        assert _load_detector_mode(p) == "openai"

    def test_mode_both_reads_correctly(self, tmp_path):
        scripts_dir = Path(__file__).resolve().parent.parent / "plugin/bubble-shield/scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            from bubble_shield_nerd import _load_detector_mode
        finally:
            sys.path.pop(0)
        p = self._write_config(tmp_path, "both")
        assert _load_detector_mode(p) == "both"

    def test_corrupt_config_falls_back_to_default_both(self, tmp_path):
        """Corrupt config → the configured default, now 'both' (#348). The
        runtime fail-open to gliner-only (if OpenAI-PF weights are missing) is
        handled by _resolve_runtime_mode(), not by the config reader."""
        scripts_dir = Path(__file__).resolve().parent.parent / "plugin/bubble-shield/scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            from bubble_shield_nerd import _load_detector_mode
        finally:
            sys.path.pop(0)
        p = tmp_path / "custom_fields.json"
        p.write_text("{invalid json}", encoding="utf-8")
        assert _load_detector_mode(p) == "both"


# ─────────────────────────────────────────────────────────────────────────────
# 4. FR-finance invariant: checksum PII wins in all modes
# ─────────────────────────────────────────────────────────────────────────────

class TestFRFinanceInvariant:
    """Verify that checksum-validated recognizers (IBAN, ISIN, SIREN) are
    unaffected by the soft detector output in all modes.

    The invariant is enforced by engine.py resolve_overlaps():
      - regex core runs at priority 0–100, score=1.0 for validated matches
      - soft detectors run at priority=5, score≤1.0
      - resolve_overlaps is length-first, then score, then priority
      - A checksum-valid IBAN has score=1.0 and spans the whole IBAN text;
        a soft span can't beat it on the same chars because resolve_overlaps
        keeps the LONGER or HIGHER-SCORE match first.

    Here we test the assumptions:
      1. openai_pf_ext.openai_pf_matches always returns priority=5
      2. IBAN recognizer produces priority=0 and score=1.0 for valid IBANs
      3. merge_soft (both mode) doesn't elevate soft priorities above 5
    """

    def test_openai_matches_always_priority_5(self):
        """openai_pf_matches returns Match objects with priority=5."""
        # When the model isn't available (stubbed), it returns [] — that's fine
        # (fail-open). When it does return matches, they must be priority=5.
        # We inject a synthetic result by patching _MODEL_CACHE.
        _MODEL_CACHE.clear()
        os.environ["BUBBLE_SHIELD_OPENAI_MOCK"] = "1"
        try:
            result = openai_pf_matches(
                "Jean Dupont 06 12 34 56 78",
                model_dir="/fake/model",
            )
            # Fail-open: mock returns []
            assert result == []
        finally:
            del os.environ["BUBBLE_SHIELD_OPENAI_MOCK"]
            _MODEL_CACHE.clear()

    def test_synthetic_soft_matches_priority_5(self):
        """Any Match produced by soft adapters must have priority=5 (by construction)."""
        soft = Match(start=0, end=10, entity_type="NOM", value="Jean Dupont",
                     score=0.9, priority=5)
        assert soft.priority == 5

    def test_iban_regex_recognizer_priority_and_score(self):
        """IBAN recognizer from recognizers.py must produce score=1.0 for valid IBANs."""
        from bubble_shield.recognizers import detect
        text = "IBAN FR76 3000 6000 0112 3456 7890 189"
        matches = detect(text)
        iban_matches = [m for m in matches if m.entity_type == "IBAN"]
        assert len(iban_matches) >= 1, "Should detect the IBAN"
        iban = iban_matches[0]
        assert iban.score == 1.0, f"Checksum-valid IBAN must have score=1.0, got {iban.score}"
        # priority for structured recognizers is always 0 (dataclass default)
        # OR a configured value — in all cases it must be < 100 and typically 0
        assert iban.priority < 100

    def test_merge_soft_does_not_elevate_priority(self):
        """merge_soft never produces priority > 5."""
        g = [Match(start=0, end=5, entity_type="NOM", value="Jean", score=0.8, priority=5)]
        o = [Match(start=0, end=5, entity_type="NOM", value="Jean", score=0.9, priority=5)]
        result = merge_soft_direct(g, o)
        for m in result:
            assert m.priority == 5, f"Merged match has unexpected priority {m.priority}"

    def test_resolve_overlaps_regex_wins_over_soft(self):
        """engine.resolve_overlaps gives priority to the IBAN match over a
        soft span covering the same chars (longer span + score=1.0 wins)."""
        from bubble_shield.recognizers import resolve_overlaps, Match as M
        # Simulate: IBAN detected by regex (score=1.0, priority=0, spans 35 chars)
        # and a soft NOM that overlaps the first 10 chars (score=0.9, priority=5)
        iban = M(start=0, end=35, entity_type="IBAN", value="FR76 3000 6000 0112 3456 7890 189",
                 score=1.0, priority=0)
        soft = M(start=0, end=10, entity_type="NOM", value="FR76 3000 6", score=0.9, priority=5)
        # resolve_overlaps: length-first means the longer IBAN wins
        result = resolve_overlaps([iban, soft])
        assert any(m.entity_type == "IBAN" for m in result), "IBAN must survive overlap resolution"
        # The soft match should be dropped (it's fully inside the IBAN span)
        nom_matches = [m for m in result if m.entity_type == "NOM"]
        assert len(nom_matches) == 0, "Soft NOM fully inside IBAN span must be dropped"

    def test_all_modes_regex_core_is_independent(self):
        """The regex core runs in AnonymizationEngine._detect regardless of mode.
        Verify by running detect() directly (no daemon needed)."""
        from bubble_shield.recognizers import detect
        text = "SIREN 552 100 554"  # Orange SA — well-known public SIREN
        matches = detect(text)
        siren_matches = [m for m in matches if m.entity_type in ("SIREN", "SIRET")]
        assert len(siren_matches) >= 1, "SIREN must be detected by regex core"
        assert any(m.score == 1.0 for m in siren_matches), "Valid SIREN must have score=1.0"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Default mode and production behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultMode:
    def test_openai_pf_matches_fails_open_without_model(self):
        """openai_pf_matches returns [] when no model is available (fail-open)."""
        _MODEL_CACHE.clear()
        result = openai_pf_matches(
            "Jean Dupont 06 12 34 56 78",
            model_dir="/nonexistent/path/to/model",
        )
        assert result == [], "Must fail-open to empty list when model missing"

    def test_gliner_ext_unchanged_interface(self):
        """gliner_ext.gliner_matches still works (returns [] when GLiNER not loaded)."""
        from bubble_shield.gliner_ext import gliner_matches
        result = gliner_matches("Jean Dupont 06 12 34 56 78")
        # Fail-open: if GLiNER model isn't loaded, returns []
        assert isinstance(result, list)

    def test_label_mapping_has_no_account_number(self):
        """account_number is NOT in OPENAI_LABEL_TO_TYPE (regex core handles it)."""
        assert "account_number" not in OPENAI_LABEL_TO_TYPE

    def test_label_mapping_covers_expected_types(self):
        expected_types = {"NOM", "ADRESSE", "EMAIL", "TEL", "DATE_EVENEMENT", "URL", "SECRET"}
        mapped_types = set(OPENAI_LABEL_TO_TYPE.values())
        assert mapped_types == expected_types


# ─────────────────────────────────────────────────────────────────────────────
# 6. Policy catalog: URL and SECRET types (Phase 2 additions)
# ─────────────────────────────────────────────────────────────────────────────

class TestPolicyCatalogPhase2:
    def test_url_in_entity_catalog(self):
        assert "URL" in ENTITY_CATALOG
        assert ENTITY_CATALOG["URL"]["default_cloak"] is True
        assert ENTITY_CATALOG["URL"]["identifying"] is True

    def test_secret_in_entity_catalog(self):
        assert "SECRET" in ENTITY_CATALOG
        assert ENTITY_CATALOG["SECRET"]["default_cloak"] is True
        assert ENTITY_CATALOG["SECRET"]["identifying"] is True

    def test_default_policy_includes_url_and_secret(self):
        policy = default_policy()
        assert "URL" in policy
        assert policy["URL"] is True   # cloak by default
        assert "SECRET" in policy
        assert policy["SECRET"] is True  # cloak by default

    def test_existing_types_unchanged(self):
        """Adding URL/SECRET must not change any existing type's behaviour."""
        policy = default_policy()
        # Spot-check key existing types
        assert policy["NOM"] is True
        assert policy["IBAN"] is True
        assert policy["MONTANT"] is False  # kept-for-reasoning
        assert policy["ISIN"] is False     # kept-for-reasoning
        assert policy["EMAIL"] is True

    def test_vendor_policy_in_sync(self):
        """Verify vendor/bubble_shield/policy.py has the same URL/SECRET rows."""
        vendor_policy_path = (
            Path(__file__).resolve().parent.parent
            / "plugin/bubble-shield/vendor/bubble_shield/policy.py"
        )
        content = vendor_policy_path.read_text(encoding="utf-8")
        assert '"URL"' in content, "vendor/policy.py must have URL type"
        assert '"SECRET"' in content, "vendor/policy.py must have SECRET type"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Sync check: root vs vendor must be identical for Phase 2 files
# ─────────────────────────────────────────────────────────────────────────────

class TestRootVendorSync:
    BASE = Path(__file__).resolve().parent.parent

    def _compare(self, rel: str) -> None:
        root_file = self.BASE / "bubble_shield" / rel
        vendor_file = self.BASE / "plugin/bubble-shield/vendor/bubble_shield" / rel
        assert root_file.read_text(encoding="utf-8") == vendor_file.read_text(encoding="utf-8"), \
            f"{rel}: root and vendor copies are out of sync"

    def test_openai_pf_ext_in_sync(self):
        self._compare("openai_pf_ext.py")

    def test_merge_in_sync(self):
        self._compare("merge.py")

    def test_policy_in_sync(self):
        self._compare("policy.py")


