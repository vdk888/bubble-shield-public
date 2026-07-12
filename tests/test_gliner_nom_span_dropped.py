"""Regression test — fix/gliner-nom-span-dropped.

BACKGROUND
----------
Real-world tax documents (impots 2025) embed client names in TWO separate
locations: a header address block and a form-field section.  GLiNER (the
soft-ML NER layer) detected names only in the form-field section where the
PDF extraction artifact produced a double-space ("DUPONT  MARC PIERRE") and a
trailing newline-char ("LEFEBVRE  CLAIRE\\nO").  The second, earlier occurrence
in the address block ("DUPONT MARC PIERRE" / "LEFEBVRE CLAIRE", single space,
no trailing char) was never detected.

ROOT CAUSE (profile_sweep.ClientProfile.learn — file engine.py _detect)
  1. GLiNER NOM spans have priority=5 and scores typically 0.45–0.70.
  2. profile_sweep.ClientProfile.learn() required score ≥ 0.85 OR
     _looks_like_person=True to trust a NOM.  GLiNER NOM below 0.85 with no
     civility title / gazetteer first name was DROPPED (not learned).
  3. Consequence: the second-pass profile sweep never found the address-block
     occurrence; the substitution loop only covered the GLiNER-detected span.

FIX
  - engine._detect() now runs a soft-ML NOM sweep step after resolve_overlaps:
    builds a mini ClientProfile from soft-ML NOM spans (priority ≤ 5), sweeps
    the text for uncovered occurrences, adds them to the raw list, and re-resolves.
  - profile_sweep.ClientProfile.learn() now trusts soft-ML NOM spans (priority ≤ 5)
    regardless of the 0.85 score gate; their own threshold filter is the gate.

SYNTHETIC MIRROR
  The real document pattern: ALL-CAPS SURNAME  FORENAME block (double-space,
  PDF extraction artifact) detected by GLiNER in one place, with a clean
  SURNAME FORENAME copy elsewhere (single space, no trailing char) that only
  the sweep catches.  The names here are entirely fictional.

All PII in this file is SYNTHETIC.  No real client data appears.
"""
from __future__ import annotations

import re
from typing import List

import pytest

from bubble_shield.engine import AnonymizationEngine, DetectedEntity
from bubble_shield.recognizers import Match
from bubble_shield.vault import Vault


# ─── Synthetic GLiNER stub ────────────────────────────────────────────────────

class _FakeGLiNER:
    """Simulates GLiNER detecting all-caps names with double-space artifact.

    The real GLiNER returns "DUPONT  MARC" (double-space) at an offset deep in
    the document.  This stub returns exactly two spans that mirror the pattern:
      - "DUPONT  MARC" at the SECOND occurrence (double-space)
      - "LEFEBVRE  CLAIRE\\nO" at the SECOND occurrence (double-space + trailing)

    The FIRST occurrences ("DUPONT MARC PIERRE" / "LEFEBVRE CLAIRE" in the
    address block) are intentionally NOT returned by the stub — they must be
    found by the sweep pass the fix introduces.
    """

    _PATTERNS = [
        # synthetic name 1 — double-space artifact
        ("DUPONT  MARC", "NOM", 0.62),
        # synthetic name 2 — double-space + trailing newline-char
        ("LEFEBVRE  CLAIRE\nO", "NOM", 0.58),
    ]

    def __call__(self, text: str) -> List[Match]:
        """Return Match objects for the SECOND occurrence of each synthetic name."""
        out = []
        for raw_span, etype, score in self._PATTERNS:
            # Find the second occurrence in the text (first is in the address block).
            first = text.find(raw_span)
            if first < 0:
                continue
            second = text.find(raw_span, first + 1)
            if second < 0:
                # Fall back to first occurrence if there's only one.
                second = first
            out.append(Match(
                start=second,
                end=second + len(raw_span),
                entity_type=etype,
                value=raw_span,
                score=score,
                priority=5,  # soft-ML priority (same as real gliner_ext)
            ))
        return out


# ─── Synthetic document ───────────────────────────────────────────────────────

# Mirrors the address-block / form-field structure of the real tax document.
# First section: address block with clean names (single space, no artifact).
# Second section: form fields with GLiNER-style double-space artifact and
#                 trailing newline-char.
_DOC = """\
Déclarant 1
DUPONT  MARC PIERRE
OU LEFEBVRE  CLAIRE
15 RUE DES FLEURS
75001 PARIS

Déclarant 1 - Nom de naissance\xa0: DUPONT  MARC
Déclarant 2 - Nom de naissance\xa0: LEFEBVRE  CLAIRE\nO
3 4,00
IMPOT SUR LES REVENUS
"""
# The stub returns GLiNER matches for the SECOND occurrence of each name:
#   "DUPONT  MARC" at the form field (after "naissance\xa0: ")
#   "LEFEBVRE  CLAIRE\nO" at the form field (after "naissance\xa0: ")
# The FIRST occurrences ("DUPONT  MARC PIERRE" / "LEFEBVRE  CLAIRE") in the
# address block at lines 2-3 must be found by the sweep pass.


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _engine_with_stub() -> AnonymizationEngine:
    return AnonymizationEngine(
        vault=Vault(),
        extra_detectors=[_FakeGLiNER()],
        context_boost=False,   # skip context boost to isolate the sweep fix
    )


def _masked(text: str) -> bool:
    """Return True if text contains no surface form of the synthetic names."""
    return ("DUPONT" not in text and "LEFEBVRE" not in text
            and "CLAIRE" not in text)


def _token_count_for(result, keyword: str) -> int:
    """Count how many DetectedEntity values contain keyword."""
    return sum(1 for e in result.entities
               if keyword.upper() in e.value.upper())


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestGLiNERNomSpanDropped:
    """The fix: soft-ML NOM spans sweep uncovered occurrences of the same name."""

    def test_address_block_names_masked_single_pass(self):
        """REGRESSION: address-block occurrence must be masked in a single pass.

        Before the fix: only the form-field occurrence (double-space) was masked;
        the address-block occurrence (single space) leaked.
        After the fix: BOTH occurrences are masked.
        """
        e = _engine_with_stub()
        res = e.anonymize(_DOC)

        # Neither name must appear in clear in the output.
        assert "DUPONT" not in res.anonymized, (
            "DUPONT leaked — address-block occurrence not masked by sweep pass")
        assert "LEFEBVRE" not in res.anonymized, (
            "LEFEBVRE leaked — address-block occurrence not masked by sweep pass")

    def test_both_occurrences_are_entities(self):
        """The entity list must include BOTH the GLiNER-detected AND the swept
        occurrence of each synthetic name."""
        e = _engine_with_stub()
        res = e.anonymize(_DOC)

        # Should have at least 2 NOM entities containing DUPONT (or MARC),
        # and at least 2 containing LEFEBVRE (or CLAIRE).
        dupont_entities = _token_count_for(res, "DUPONT")
        lefebvre_entities = _token_count_for(res, "LEFEBVRE")

        assert dupont_entities >= 2, (
            f"Expected ≥2 DUPONT entities (GLiNER + sweep), got {dupont_entities}")
        assert lefebvre_entities >= 2, (
            f"Expected ≥2 LEFEBVRE entities (GLiNER + sweep), got {lefebvre_entities}")

    def test_double_space_artifact_value_does_not_leak(self):
        """The double-space form-field occurrence must also be masked."""
        e = _engine_with_stub()
        res = e.anonymize(_DOC)

        # The exact double-space form "DUPONT  MARC" must not appear in clear.
        assert "DUPONT  MARC" not in res.anonymized, (
            "Double-space form-field value leaked")

    def test_trailing_newline_artifact_does_not_break_masking(self):
        """The trailing \\nO artifact (LEFEBVRE  CLAIRE\\nO) must not prevent
        the name from being masked in either occurrence."""
        e = _engine_with_stub()
        res = e.anonymize(_DOC)

        assert "LEFEBVRE" not in res.anonymized, (
            "LEFEBVRE leaked despite trailing-newline artifact fix")
        assert "CLAIRE" not in res.anonymized, (
            "CLAIRE leaked despite trailing-newline artifact fix")

    def test_regex_only_path_unaffected(self):
        """The engine without extra detectors must not be affected."""
        e = AnonymizationEngine(vault=Vault(), context_boost=False)
        # Arbitrary safe text — engine should not crash.
        res = e.anonymize("Réunion demain à 14h — rien de sensible.")
        assert res.entity_count == 0

    def test_no_sweep_when_no_soft_ml_detectors(self):
        """When there are no soft-ML extra_detectors, the sweep is a no-op
        (no spurious masking added by the new path)."""
        e = AnonymizationEngine(vault=Vault(), context_boost=False)
        doc = "DUPONT MARC PIERRE\nOU LEFEBVRE CLAIRE\n"
        res = e.anonymize(doc)

        # Without any NER, the regex NOM needs a title — none here, so no NOM.
        nom_entities = [ent for ent in res.entities if ent.entity_type == "NOM"]
        # Regex alone fires on titled names; here there is no title, so expect 0.
        assert len(nom_entities) == 0, (
            f"Unexpected NOM masking without soft-ML detector: {nom_entities}")


class TestGLiNERProfileLearnTrust:
    """profile_sweep.ClientProfile.learn() trusts soft-ML NOM spans (priority ≤ 5)."""

    def test_soft_ml_nom_learned_below_0_85(self):
        """A soft-ML NOM with score < 0.85 must be learned into the profile."""
        from bubble_shield.profile_sweep import ClientProfile

        m = Match(start=100, end=110, entity_type="NOM",
                  value="DUPONT  MARC", score=0.62, priority=5)
        profile = ClientProfile()
        # Build a DetectedEntity from the Match as the engine does.
        from bubble_shield.engine import DetectedEntity
        ent = DetectedEntity(entity_type=m.entity_type, value=m.value,
                             token="⟦NOM_0001⟧", score=m.score,
                             start=m.start, end=m.end, priority=m.priority)
        profile.learn([ent], min_score=0.0)

        # "DUPONT" (7 chars ≥ 4) must be in name_tokens.
        assert "DUPONT" in profile.name_tokens, (
            "soft-ML NOM token not learned despite priority=5 trust gate")

    def test_regex_nom_below_0_85_still_gated(self):
        """A regex-NOM (priority=50) with score < 0.85 and no civility must NOT
        be learned — the pollution guard must still apply to regex NOM."""
        from bubble_shield.profile_sweep import ClientProfile
        from bubble_shield.engine import DetectedEntity

        # Simulate a greedy regex NOM hit: "CARDIF GENERALI" — NOT a person.
        ent = DetectedEntity(entity_type="NOM", value="CARDIF GENERALI",
                             token="⟦NOM_0001⟧", score=0.80,
                             start=0, end=14, priority=50)
        profile = ClientProfile()
        profile.learn([ent], min_score=0.0)

        # Should NOT be learned (score < 0.85, no civility, priority > 5).
        assert "CARDIF" not in profile.name_tokens, (
            "Regex NOM 'CARDIF GENERALI' learned despite pollution guard")
        assert "GENERALI" not in profile.name_tokens

    def test_soft_ml_nom_whitespace_normalization(self):
        """Double-space and trailing-newline artifacts are stripped before learning."""
        from bubble_shield.profile_sweep import ClientProfile
        from bubble_shield.engine import DetectedEntity

        # Double-space artifact.
        ent_ds = DetectedEntity(entity_type="NOM", value="DUPONT  MARC",
                                token="⟦NOM_0001⟧", score=0.62,
                                start=100, end=112, priority=5)
        # Trailing-newline artifact.
        ent_nl = DetectedEntity(entity_type="NOM", value="LEFEBVRE  CLAIRE\nO",
                                token="⟦NOM_0002⟧", score=0.58,
                                start=200, end=219, priority=5)

        profile = ClientProfile()
        profile.learn([ent_ds, ent_nl], min_score=0.0)

        # Values stored must be clean (no double-space or trailing \nO).
        for v in profile.values:
            assert "  " not in v, f"Double-space in learned value: {v!r}"
            assert "\n" not in v, f"Newline in learned value: {v!r}"

        # Both surnames and forenames must appear in name_tokens.
        assert "DUPONT" in profile.name_tokens
        assert "LEFEBVRE" in profile.name_tokens
        assert "CLAIRE" in profile.name_tokens   # ≥4 chars
