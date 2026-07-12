"""test_280_filename_footer.py — fix #280: filename-seeded footer/boilerplate leak.

ISSUE: Every signed PDF of the CGP type ends with boilerplate:
    "Page de signatures complémentaire au document DURAND Théophile - DER 012026..."
The client's name is embedded verbatim in a quoted filename fragment. No content
recognizer catches this (no label, no civic-context, just a quoted path).

FIX (rev 3 — self-corroboration loop closed):
  Layer 1 — filename_footer_matches(): positional, emits NOM for filename name-tokens
    exactly where they appear in footer boilerplate ("au document …"). Covers D1
    (footer only, no body corroboration) without body-wide seeding.
    Returns (matches, footer_nom_spans) so footer spans can be excluded from Layer 2.
  Layer 2 — doc_level_person_repetition_matches(): promotes a filename candidate to a
    body-wide seed ONLY when corroborated by an INDEPENDENT body-recognizer NOM
    (civility "M. DUPONT", form-label, signataire, etc.). Footer NOMs from Layer 1
    are EXCLUDED from the corroboration pool (footer_nom_spans parameter). This closes
    the self-corroboration loop: brand/insurer names in the footer no longer self-seed.

Test cases (synthetic PII only):
  D1   Footer boilerplate with client name → MASKS
  D2   Body occurrence of same name also masks when independently corroborated
  D3   Doc-type tokens from filename (DER, 012026, CONVENTION) do NOT over-mask
  D4   Company-only filename → NO spurious person seed (RAISON_SOCIALE path unaffected)
  D5   De-anon round-trip: masked output restores to original
  D6   extract_person_tokens_from_filename() unit tests — stop-list precision
  D7   Date patterns stripped from filename tokens
  D8   Filename with no person name (all-stoplist) → empty token list
  D9   Common surname in filename is seeded (bypass guard) → body occurrence masks
  D10  Two-token pair seed (SURNAME FORENAME) masks footer, both orderings work

  --- Over-mask ship-blockers (added rev 2, extended rev 3) ---
  D11  PREDICA DUPONT.pdf → DUPONT masks, PREDICA does NOT mask in body
  D12  HELVETIA Pierre.pdf → Pierre handled, HELVETIA not masked in body
  D13  PEA Bourse.pdf → BOURSE not masked
  D14  Liasse fiscale SELARL DURANTON.pdf → LIASSE/FISCALE not masked; DURANTON masks
  D15  Pure footer: name only in "au document" line → positional pass masks it
  D16  No-self-corroboration (rev3): footer NOM does NOT self-seed body occurrences
       when no independent body NOM exists for the token.
       Regression tests:
         D16a ZEPHYRA DUPONT - DER.pdf: ZEPHYRA (insurer) body-survives (≥2 occurrences
              are NOT masked); DUPONT still masks via "M. DUPONT" → independent NOM.
         D16b KORRIGAN Martin.pdf: KORRIGAN (invented brand) body-survives.
         D16c TESTONI with "M. TESTONI" in body → independent NOM → body-wide mask.
         D16d TESTONI footer-only (no independent signal) → footer masks, body survives.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

# Ensure the local bubble_shield package is on the path
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

import pytest
from bubble_shield.structured_ext import (
    extract_person_tokens_from_filename,
    filename_footer_matches,
    make_structured_detector,
    doc_level_person_repetition_matches,
    _person_name_seeds,
)
from bubble_shield.recognizers import Match


# ── D6: extract_person_tokens_from_filename unit tests ───────────────────────

class TestExtractPersonTokensFromFilename:
    """Unit tests for the filename token extractor (fix #280)."""

    def test_basic_surname_forename(self):
        """'TESTONI Prénomtest - DER 012026.pdf' → ['TESTONI', 'PRÉNOMTEST'] (accents preserved in upper)"""
        tokens = extract_person_tokens_from_filename("TESTONI Prénomtest - DER 012026.pdf")
        assert "TESTONI" in tokens
        # Accented upper-case is preserved: 'é' → 'É' in NFC
        assert any("NOMTEST" in t for t in tokens), (
            f"Expected a token containing 'NOMTEST' (forename part), got: {tokens}")

    def test_doc_type_stripped(self):
        """DER, RTO, CONVENTION must NOT appear in tokens."""
        tokens = extract_person_tokens_from_filename("DUPONT Jean - CONVENTION RTO - 2026-01-15.pdf")
        assert "DER" not in tokens
        assert "RTO" not in tokens
        assert "CONVENTION" not in tokens

    def test_date_stripped(self):
        """Date patterns like '012026', '2026-02-18' must be stripped."""
        tokens = extract_person_tokens_from_filename("DURAND Theophile - DER 012026 - 2026-02-18.pdf")
        assert "012026" not in tokens
        assert "2026" not in tokens
        # But the name tokens survive
        assert "DURAND" in tokens

    def test_extension_stripped(self):
        """Extension '.pdf', '.docx' must not appear as a token."""
        tokens = extract_person_tokens_from_filename("MARTIN Emilie.pdf")
        assert "PDF" not in tokens

    def test_firm_product_stripped(self):
        """Firm/product words like GEFINEO, CORUM must be stripped."""
        tokens = extract_person_tokens_from_filename("MARTIN Emilie - SCPI CORUM - DCC.pdf")
        assert "GEFINEO" not in tokens
        assert "CORUM" not in tokens
        assert "SCPI" not in tokens
        assert "DCC" not in tokens

    # D8
    def test_all_stoplist_returns_empty(self):
        """Filename with only stoplist tokens → empty list."""
        tokens = extract_person_tokens_from_filename("DER RTO CONVENTION SIGNE.pdf")
        assert tokens == []

    def test_company_only_filename_no_person(self):
        """'SELARL DU DOCTEUR... .pdf' → no person tokens (all stripped by RAISON_SOCIALE_PREFIXES)."""
        tokens = extract_person_tokens_from_filename("SELARL DU DOCTEUR FAKECOMPANY - DA.pdf")
        # SELARL, DU, DOCTEUR are in _RAISON_SOCIALE_PREFIXES / stop-list
        # FAKECOMPANY may survive — it's not in any stop-list; that's expected
        # The key test: SELARL, DU, DOCTEUR are NOT in tokens
        assert "SELARL" not in tokens
        assert "DU" not in tokens
        assert "DOCTEUR" not in tokens

    # D7
    def test_various_date_formats_stripped(self):
        """Different date formats are all stripped."""
        tokens = extract_person_tokens_from_filename("LECLERC Henri - 2025-12-01 - 12012025.pdf")
        assert "2025" not in tokens
        assert "12012025" not in tokens
        assert "LECLERC" in tokens


# ── D1/D2: Footer and body masking ───────────────────────────────────────────

class TestFooterMasking:
    """Integration tests: filename-seeded tokens mask footer boilerplate + body."""

    def _run_detector(self, text: str, basename: str):
        detector = make_structured_detector(filename_basename=basename)
        return detector(text)

    # D1: footer boilerplate masks
    def test_footer_name_masked(self):
        """Footer 'complémentaire au document TESTONI Prénomtest' → NOM match."""
        text = (
            "Contrat de retraite complémentaire.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        matches = self._run_detector(text, basename)
        nom_values = [m.value for m in matches if m.entity_type == "NOM"]
        # At least one of the surname or the pair must appear
        matched_upper = [v.upper() for v in nom_values]
        assert any("TESTONI" in v for v in matched_upper), (
            f"Expected TESTONI in NOM matches, got: {nom_values}")

    # D2: body occurrence also masks
    def test_body_occurrence_also_masked(self):
        """Body occurrence 'TESTONI a signé...' also masks when seeded from filename."""
        text = (
            "M. TESTONI a signé le présent contrat.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        matches = self._run_detector(text, basename)
        nom_values = [m.value for m in matches if m.entity_type == "NOM"]
        matched_upper = [v.upper() for v in nom_values]
        assert any("TESTONI" in v for v in matched_upper), (
            f"Expected TESTONI in body NOM matches, got: {nom_values}")

    # D10: pair seed both orderings
    def test_pair_seed_both_orderings(self):
        """Both 'TESTONI <FORENAME>' and '<FORENAME> TESTONI' orderings generate seeds."""
        tokens = extract_person_tokens_from_filename("TESTONI Prénomtest - DER 012026.pdf")
        assert len(tokens) >= 2, f"Expected >=2 tokens, got: {tokens}"
        seeds = _person_name_seeds(tokens, bypass_common_surname_guard=True)
        # Both pair orderings must be present (order-insensitive check)
        pair_seeds = [s for s in seeds if " " in s]
        assert len(pair_seeds) >= 2, (
            f"Expected >=2 pair seeds (both orderings), got: {seeds}"
        )
        # The two pair seeds should be reverses of each other
        first_words = {s.split()[0] for s in pair_seeds}
        assert len(first_words) == 2, (
            f"Expected both orderings in pair seeds, got: {pair_seeds}"
        )


# ── D3: Precision — doc-type tokens do NOT over-mask ─────────────────────────

class TestPrecisionDocTypeNotMasked:
    """Doc-type and date tokens from filename must NOT cause over-masking."""

    def test_doc_type_tokens_not_seeded(self):
        """DER, 012026, CONVENTION must not appear as seeds."""
        tokens = extract_person_tokens_from_filename("TESTONI Prénomtest - DER 012026 - CONVENTION.pdf")
        # None of the stop-list words should survive
        tokens_upper = [t.upper() for t in tokens]
        for bad in ["DER", "012026", "CONVENTION", "PDF"]:
            assert bad not in tokens_upper, f"Stop-list word {bad!r} leaked into tokens: {tokens}"

    def test_doc_type_word_in_body_not_masked(self):
        """'DER' appearing in the document body is NOT masked (not a seed)."""
        text = (
            "Ce document DER décrit les engagements.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)
        # No match should cover the standalone "DER" that is not a name token
        # Find the span of "DER" in "Ce document DER décrit"
        der_pos = text.find("document DER") + len("document ")
        der_end = der_pos + 3  # len("DER")
        covering_matches = [
            m for m in matches
            if m.start <= der_pos and m.end >= der_end
        ]
        assert not covering_matches, (
            f"'DER' in body was masked — should NOT be. Covering matches: {covering_matches}")


# ── D4: Company-only filename — no spurious person seed ───────────────────────

class TestCompanyOnlyFilename:
    """Company-only filenames must not inject spurious person seeds."""

    def test_selarl_filename_no_person_seed(self):
        """'SELARL CABINET GEFINEO - DA.pdf' → no person seeds injected."""
        basename = "SELARL CABINET GEFINEO - DA.pdf"
        detector = make_structured_detector(filename_basename=basename)
        # Text that would match if spurious seeds were created
        text = (
            "La société SELARL CABINET gère les dossiers.\n"
            "Dénomination ou raison sociale : SELARL CABINET GEFINEO\n"
        )
        matches = detector(text)
        # RAISON_SOCIALE match IS expected (from the labeled line), but
        # the NOM match from the filename seed should NOT add tokens for
        # "CABINET", "SELARL", "GEFINEO" as lone person-name seeds
        # (they are all in _FORME_JURIDIQUE_SET / _RAISON_SOCIALE_PREFIXES / stop-list)
        nom_matches = [m for m in matches if m.entity_type == "NOM"]
        # None of the NOM matches should be purely "SELARL" or "CABINET"
        bad_names = {"SELARL", "CABINET", "GEFINEO", "DA"}
        nom_values_upper = [m.value.strip().upper() for m in nom_matches]
        for bad in bad_names:
            assert bad not in nom_values_upper, (
                f"Spurious NOM match for {bad!r} from company-only filename: {nom_matches}")


# ── D9: Common surname in filename IS seeded (bypass guard) ──────────────────

class TestCommonSurnameFilenameBypass:
    """A common surname in the filename must still be seeded (bypass_common_surname_guard)."""

    def test_common_surname_seeded_from_filename(self):
        """'DUPONT Jean - DER.pdf' → DUPONT IS in seeds despite being common."""
        # DUPONT is in _COMMON_FRENCH_SURNAMES; the bypass must include it
        tokens = extract_person_tokens_from_filename("DUPONT Jean - DER.pdf")
        seeds = _person_name_seeds(tokens, bypass_common_surname_guard=True)
        seeds_upper = [s.upper() for s in seeds]
        assert any("DUPONT" in s for s in seeds_upper), (
            f"DUPONT should be seeded via bypass but is missing from: {seeds}")

    def test_common_surname_body_masked_via_filename(self):
        """Body occurrence of a common surname IS masked when anchored via filename."""
        text = (
            "DUPONT Jean a souscrit au contrat.\n"
            "Page de signatures complémentaire au document DUPONT Jean - DER 012026\n"
        )
        basename = "DUPONT Jean - DER 012026.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)
        nom_values = [m.value.upper() for m in matches if m.entity_type == "NOM"]
        assert any("DUPONT" in v for v in nom_values), (
            f"DUPONT should be masked via filename seed, got NOM matches: {nom_values}")


# ── D5: De-anonymisation round-trip ──────────────────────────────────────────

class TestDeanonRoundTrip:
    """Tokens produced by filename-seeded masking must round-trip correctly."""

    def test_roundtrip_via_engine(self):
        """Anonymise with filename seed → de-anonymise → original text recovered.

        Uses "M. TESTONI" so TESTONI has an independent body NOM (civility recognizer),
        enabling body-wide corroborated masking (required after rev3 fix).
        """
        from bubble_shield import AnonymizationEngine, Vault
        from bubble_shield import policy as _policy

        # Include "M. TESTONI" so the civility recognizer provides an independent NOM
        # → corroborated → body-wide mask (rev3: footer NOM alone is not enough).
        text = "M. TESTONI a signé. Page de signatures complémentaire au document TESTONI Prénomtest."
        basename = "TESTONI Prénomtest - DER 012026.pdf"

        detector = make_structured_detector(filename_basename=basename)
        vault = Vault(mission="test-280-roundtrip")
        # Use default_policy() (not load_policy()) so the test is hermetic and
        # never reads the developer's live ~/.bubble_shield/policy.json, which may
        # have every type set to KEEP (false) and would suppress all NOM matches.
        engine = AnonymizationEngine(
            extra_detectors=[detector],
            vault=vault,
            match_filter=_policy.make_match_filter(_policy.default_policy()),
        )

        result = engine.anonymize(text)
        anon = result.anonymized
        # Name must be replaced (both body and footer)
        assert "TESTONI" not in anon, f"Name leaked in anonymised output: {anon!r}"
        # Round-trip: de-anonymise must recover the original
        restored = engine.deanonymize(anon)
        assert "TESTONI" in restored, f"Name not restored in de-anon: {restored!r}"
        # The de-anonymised text should match the original (modulo spacing artifacts)
        assert restored.replace(" ", "") == text.replace(" ", ""), (
            f"Round-trip mismatch.\nOriginal: {text!r}\nRestored: {restored!r}")


# ── D11-D16: Over-mask ship-blockers (rev 2) ─────────────────────────────────

class TestOverMaskShipBlockers:
    """Adversarial brand-filename tests: insurer/product names in filename must NOT
    over-mask body text. These are the reviewer-proven ship-blockers fixed in rev 2
    via the corroboration + positional approach."""

    # D11
    def test_predica_dupont_no_body_overmask(self):
        """'PREDICA DUPONT.pdf' → DUPONT masks, PREDICA does NOT mask in body."""
        text = (
            "M. DUPONT a souscrit un contrat PREDICA.\n"
            "La compagnie PREDICA est votre assureur de référence.\n"
            "Page de signatures complémentaire au document PREDICA DUPONT - DER 012026\n"
        )
        basename = "PREDICA DUPONT.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        # DUPONT must be masked (corroborated by "M. DUPONT" → civility NOM)
        dupont_matches = [m for m in matches if "DUPONT" in m.value.upper()]
        assert dupont_matches, f"DUPONT should be masked but was not. Matches: {matches}"

        # PREDICA must NOT be masked in the body (not corroborated as a name)
        # Find where the body PREDICA occurrences are (before the footer line)
        footer_start = text.find("Page de signatures")
        predica_body_matches = [
            m for m in matches
            if "PREDICA" in m.value.upper() and m.start < footer_start
        ]
        assert not predica_body_matches, (
            f"PREDICA in body should NOT be masked (insurer name), "
            f"but got: {predica_body_matches}"
        )

    # D12
    def test_helvetia_pierre_no_body_overmask(self):
        """'HELVETIA Pierre.pdf' → Pierre handled, HELVETIA not masked in body."""
        text = (
            "M. Pierre a souscrit.\n"
            "Le contrat HELVETIA est géré par votre assureur HELVETIA.\n"
            "Page de signatures complémentaire au document HELVETIA Pierre - DER\n"
        )
        basename = "HELVETIA Pierre.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        # Pierre must be masked (corroborated by "M. Pierre")
        pierre_matches = [m for m in matches if "PIERRE" in m.value.upper()]
        assert pierre_matches, f"Pierre should be masked, got: {matches}"

        # HELVETIA must NOT be masked in body
        footer_start = text.find("Page de signatures")
        helvetia_body = [
            m for m in matches
            if "HELVETIA" in m.value.upper() and m.start < footer_start
        ]
        assert not helvetia_body, (
            f"HELVETIA in body should NOT be masked, but got: {helvetia_body}"
        )

    # D13
    def test_bourse_not_masked(self):
        """'PEA Bourse.pdf' → BOURSE not seeded, not masked."""
        # BOURSE is in _FILENAME_STOP_TOKENS (belt-and-suspenders)
        tokens = extract_person_tokens_from_filename("TESTONI Jean - PEA Bourse.pdf")
        tokens_upper = [t.upper() for t in tokens]
        assert "BOURSE" not in tokens_upper, (
            f"BOURSE should not be in filename tokens, got: {tokens}"
        )

        # Integration: BOURSE not masked in body text
        text = (
            "Votre PEA Bourse est actif.\n"
            "M. TESTONI a souscrit.\n"
            "Page de signatures complémentaire au document TESTONI Jean - PEA Bourse 2026\n"
        )
        detector = make_structured_detector(filename_basename="TESTONI Jean - PEA Bourse.pdf")
        matches = detector(text)
        bourse_matches = [m for m in matches if "BOURSE" in m.value.upper()]
        assert not bourse_matches, (
            f"BOURSE should NOT be masked, but got: {bourse_matches}"
        )

    # D14
    def test_liasse_fiscale_not_masked_duranton_masks(self):
        """'Liasse fiscale SELARL DURANTON.pdf' → LIASSE/FISCALE not masked; DURANTON masks."""
        # Stop-list check
        tokens = extract_person_tokens_from_filename("Liasse fiscale SELARL DURANTON.pdf")
        tokens_upper = [t.upper() for t in tokens]
        assert "LIASSE" not in tokens_upper, f"LIASSE should not be in tokens: {tokens}"
        assert "FISCALE" not in tokens_upper, f"FISCALE should not be in tokens: {tokens}"

        # Integration: DURANTON masks via RAISON_SOCIALE, LIASSE/FISCALE do NOT
        text = (
            "Liasse fiscale 2024.\n"
            "Document fiscal: annexe liasse.\n"
            "Raison sociale : SELARL DURANTON\n"
            "Page de signatures complémentaire au document Liasse fiscale SELARL DURANTON\n"
        )
        detector = make_structured_detector(
            filename_basename="Liasse fiscale SELARL DURANTON.pdf")
        matches = detector(text)

        # DURANTON must be masked (via RAISON_SOCIALE path)
        duranton_matches = [m for m in matches if "DURANTON" in m.value.upper()]
        assert duranton_matches, f"DURANTON should be masked, got: {matches}"

        # LIASSE and FISCALE must NOT be masked
        liasse_matches = [m for m in matches if m.value.upper() in ("LIASSE", "FISCALE")]
        assert not liasse_matches, (
            f"LIASSE/FISCALE should NOT be masked, but got: {liasse_matches}"
        )

    # D15
    def test_positional_footer_only_no_body_seed(self):
        """Pure footer case: name in footer via positional pass, not body-wide seeded.

        'TESTONI Prénomtest - DER.pdf' with name only in the footer line.
        The positional layer (filename_footer_matches) must catch it.
        No body-wide seed (no corroboration from body NOM).
        Body text without TESTONI must remain untouched.
        """
        text = (
            "Contrat de retraite complémentaire.\n"
            "Informations générales sur le plan.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        candidates = extract_person_tokens_from_filename(basename)
        # filename_footer_matches now returns (matches, footer_nom_spans) — unpack
        footer_matches, footer_nom_spans = filename_footer_matches(text, candidates)
        testoni_footer = [m for m in footer_matches if "TESTONI" in m.value.upper()]
        assert testoni_footer, (
            f"Positional footer pass should emit NOM for TESTONI, got: {footer_matches}"
        )
        # The footer match must be within the footer line
        footer_line_start = text.find("Page de signatures")
        for m in testoni_footer:
            assert m.start >= footer_line_start, (
                f"TESTONI match {m} should be within the footer line "
                f"(start >= {footer_line_start})"
            )
        # footer_nom_spans must include those spans (rev3 regression check)
        for m in testoni_footer:
            assert (m.start, m.end) in footer_nom_spans, (
                f"footer_nom_spans missing span for {m}"
            )

    # D16 — rev3: no-self-corroboration regression + proper corroboration tests

    def test_no_self_corroboration_zephyra(self):
        """D16a: ZEPHYRA (insurer brand in filename) does NOT self-seed body-wide.

        Bug (rev2): "ZEPHYRA DUPONT - DER.pdf" → Layer 1 emits NOM(ZEPHYRA) in footer
        → Layer 2 sees ZEPHYRA in nom_detected_words (self-corroboration) → ZEPHYRA
        seeded body-wide → every body mention of the insurer masked (over-mask).

        Fix (rev3): footer NOMs excluded from corroboration pool → ZEPHYRA has no
        independent body NOM → NOT corroborated → NOT seeded body-wide → survives.
        DUPONT IS corroborated via "M. DUPONT" → still masks body-wide (correct recall).
        """
        text = (
            "Contrat ZEPHYRA Multisupport souscrit par M. DUPONT.\n"
            "ZEPHYRA est l'assureur référence de votre contrat.\n"
            "Informations complémentaires sur votre contrat ZEPHYRA.\n"
            "Page de signatures complémentaire au document ZEPHYRA DUPONT - DER 012026\n"
        )
        basename = "ZEPHYRA DUPONT - DER.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        footer_start = text.find("Page de signatures")

        # DUPONT must be masked (corroborated by "M. DUPONT" → civility NOM)
        dupont_body = [
            m for m in matches
            if "DUPONT" in m.value.upper() and m.start < footer_start
        ]
        assert dupont_body, (
            f"DUPONT should be masked in body (M. DUPONT → independent NOM), got: {matches}"
        )

        # ZEPHYRA must NOT be masked in the body (no independent NOM for it)
        # It appears 3 times in the body; at least 2 must survive unmasked.
        zephyra_body_positions = [
            m.start for m in matches
            if "ZEPHYRA" in m.value.upper() and m.start < footer_start
        ]
        # Count body occurrences of ZEPHYRA in the original text (before footer)
        body_text = text[:footer_start]
        import re as _re
        total_body_zephyra = len(_re.findall(r"ZEPHYRA", body_text))
        masked_body_zephyra = len(zephyra_body_positions)
        surviving_zephyra = total_body_zephyra - masked_body_zephyra
        assert surviving_zephyra >= 2, (
            f"ZEPHYRA should survive in body (insurer name, no independent NOM). "
            f"Total body occurrences: {total_body_zephyra}, masked: {masked_body_zephyra}, "
            f"surviving: {surviving_zephyra}. Matches: {matches}"
        )

    def test_no_self_corroboration_korrigan(self):
        """D16b: KORRIGAN (invented brand not in stop-list) does NOT self-seed body.

        Even a brand name that is NOT on the stop-list must not self-corroborate.
        The corroboration rule (not the stop-list) is the real fix.
        """
        text = (
            "Le contrat KORRIGAN Flex est en vigueur.\n"
            "Votre assureur KORRIGAN gère ce produit.\n"
            "Page de signatures complémentaire au document KORRIGAN Martin - DER 012026\n"
        )
        basename = "KORRIGAN Martin.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        footer_start = text.find("Page de signatures")
        korrigan_body_matches = [
            m for m in matches
            if "KORRIGAN" in m.value.upper() and m.start < footer_start
        ]
        body_text = text[:footer_start]
        import re as _re
        total_korrigan = len(_re.findall(r"KORRIGAN", body_text))
        masked_korrigan = len(korrigan_body_matches)
        surviving = total_korrigan - masked_korrigan
        assert surviving >= 2, (
            f"KORRIGAN should survive in body (brand, no independent NOM). "
            f"Total: {total_korrigan}, masked: {masked_korrigan}, surviving: {surviving}. "
            f"Matches: {matches}"
        )

    def test_independent_body_nom_enables_body_wide_mask(self):
        """D16c: Client name in footer + independent "M. TESTONI" body signal → body masks.

        When a body recognizer independently detects TESTONI (via civility "M. TESTONI"),
        that NOM is NOT in footer_nom_spans → it IS counted for corroboration →
        TESTONI IS seeded body-wide → all body occurrences mask.
        """
        text = (
            "M. TESTONI a souscrit le contrat.\n"
            "TESTONI est le bénéficiaire désigné.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        footer_start = text.find("Page de signatures")
        testoni_body = [
            m for m in matches
            if "TESTONI" in m.value.upper() and m.start < footer_start
        ]
        testoni_footer = [
            m for m in matches
            if "TESTONI" in m.value.upper() and m.start >= footer_start
        ]
        assert testoni_footer, (
            f"Footer TESTONI should be masked (positional pass), got: {matches}"
        )
        assert testoni_body, (
            f"Body TESTONI should be masked (corroborated via 'M. TESTONI' → independent NOM), "
            f"got: {matches}"
        )

    def test_footer_only_name_survives_in_body(self):
        """D16d: Name in footer but no independent body NOM signal → footer masks, body survives.

        After the rev3 fix: TESTONI appears in footer (Layer 1 masks it positionally),
        but also appears as a bare word in the body with NO civility/label signal.
        Since there is no independent body NOM, TESTONI is NOT corroborated → NOT seeded
        body-wide → bare body occurrences survive.
        """
        text = (
            "TESTONI souscrit le contrat.\n"
            "Contrat de retraite TESTONI complémentaire.\n"
            "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026\n"
        )
        basename = "TESTONI Prénomtest - DER 012026.pdf"
        detector = make_structured_detector(filename_basename=basename)
        matches = detector(text)

        footer_start = text.find("Page de signatures")
        testoni_footer = [
            m for m in matches
            if "TESTONI" in m.value.upper() and m.start >= footer_start
        ]
        testoni_body_masked = [
            m for m in matches
            if "TESTONI" in m.value.upper() and m.start < footer_start
        ]

        # Footer must still mask (Layer 1 positional)
        assert testoni_footer, (
            f"Footer TESTONI should be masked (positional pass), got: {matches}"
        )
        # Body occurrences must survive (no independent corroboration)
        import re as _re
        body_text = text[:footer_start]
        total_body = len(_re.findall(r"TESTONI", body_text))
        masked_body = len(testoni_body_masked)
        surviving = total_body - masked_body
        assert surviving >= 1, (
            f"Body TESTONI should survive (no independent NOM signal for corroboration). "
            f"Total body: {total_body}, masked: {masked_body}, surviving: {surviving}. "
            f"Matches: {matches}"
        )
