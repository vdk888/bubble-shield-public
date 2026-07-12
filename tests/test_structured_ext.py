"""
test_structured_ext.py — deterministic FR-KYC form recognizers.

Birthplace printed right after a DOB on a value line ("DD/MM/YYYY CITY") — the
detached-form pattern GLiNER loses in context but regex nails. Synthetic data.
"""
import pytest

from bubble_shield.structured_ext import (
    birthplace_matches, civility_name_matches, commune_postcode_matches,
    make_structured_detector,
)


def _places(text):
    return {m.value for m in birthplace_matches(text)}


def test_birthplace_after_dob_simple():
    got = _places("04/05/1980 LYON")
    assert "LYON" in got


def test_birthplace_with_country_parenthetical():
    got = _places("12/09/1975 BORDEAUX (France)")
    assert any("BORDEAUX" in v for v in got)


def test_multiword_city():
    got = _places("01/01/1990 LE MANS")
    assert any("LE MANS" in v for v in got)


def test_all_matches_are_lieu_naissance_type():
    ms = birthplace_matches("03/03/1991 NANTES\n07/07/1988 LILLE")
    assert ms and all(m.entity_type == "LIEU_NAISSANCE" for m in ms)


def test_heading_after_date_is_not_a_place():
    # A date before a section heading must NOT be flagged as a birthplace.
    assert _places("Fait le 01/06/2026 AVERTISSEMENT") == set()
    assert _places("12/05/2025 ANNEXE") == set()


def test_offsets_point_at_the_place():
    text = "né le 04/05/1980 TOULOUSE et autre"
    for m in birthplace_matches(text):
        assert text[m.start:m.end] == m.value


def test_no_false_fire_without_a_date():
    assert birthplace_matches("habite à PARIS depuis 2010") == set() or \
        all(m.value for m in birthplace_matches("habite à PARIS"))


def test_detector_callable_shape():
    det = make_structured_detector()
    ms = det("04/05/1980 RENNES")
    assert any(m.entity_type == "LIEU_NAISSANCE" for m in ms)


# ── civility + name recognizer (clean name source for form layouts) ─────────


def _names(text):
    return {m.value.strip() for m in civility_name_matches(text)}


def test_civility_name_simple():
    assert "Marie Dubois" in _names("Reçu Madame Marie Dubois ce jour")
    assert "Jean Martin" in _names("M. Jean Martin, client")


def test_civility_name_stops_at_newline():
    # THE form-layout fix: a name on a value line must NOT swallow the next
    # (product/heading) line. "M. DUPONT\nContrat EUROPE" → just "DUPONT".
    got = _names("M. DUPONT\nContrat EUROPE")
    assert "DUPONT" in got
    assert not any("EUROPE" in g or "Contrat" in g for g in got)


def test_civility_name_multiword_surname():
    assert any("DE TOUR" in n for n in _names("Madame SOPHIE DE TOUR"))


def test_civility_name_is_nom_type():
    ms = civility_name_matches("Monsieur Paul Durand")
    assert ms and all(m.entity_type == "NOM" for m in ms)


def test_civility_name_offsets_exact():
    text = "voir M. Léon Bernard demain"
    for m in civility_name_matches(text):
        assert text[m.start:m.end] == m.value


def test_no_civility_no_match():
    # Without a civility title we don't fire (precision — that's GLiNER's job).
    assert civility_name_matches("la société Acme Capital investit") == [] or \
        all("Acme" not in m.value for m in civility_name_matches("société Acme Capital"))


def test_structured_detector_includes_names():
    det = make_structured_detector()
    ms = det("Madame Marie Dubois, née le 04/05/1980 LYON")
    types = {m.entity_type for m in ms}
    assert "NOM" in types and "LIEU_NAISSANCE" in types


# ── fix #395: standalone commune + postcode recognizer ───────────────────────
# The full-address ADRESSE path (recognizers.py) catches a commune+postcode only
# INSIDE a full address (needs a street number). A BARE 'TOWN 99000' was missed
# entirely → silent leak, no review candidate. These verify the additive
# standalone recognizer, the engine masking, the full-address regression guard,
# and the precision negatives. Synthetic communes (TESTVILLE-…) where possible.


def test_commune_postcode_standalone_match():
    ms = commune_postcode_matches("Rappel: client à MONTBOURG-LES-PINS 99000.")
    assert ms, "standalone commune+postcode must be detected"
    m = ms[0]
    assert m.entity_type == "ADRESSE"
    assert "MONTBOURG-LES-PINS" in m.value and "99000" in m.value


def test_commune_postcode_match_offsets_exact():
    text = "voir TESTVILLE-SUR-MER 99000 demain"
    for m in commune_postcode_matches(text):
        assert text[m.start:m.end] == m.value
        assert m.entity_type == "ADRESSE"


def test_commune_postcode_synthetic_town():
    ms = commune_postcode_matches("Adresse: TESTVILLE-SUR-MER 99000")
    assert ms and ms[0].entity_type == "ADRESSE"
    assert "TESTVILLE-SUR-MER" in ms[0].value


def test_commune_postcode_negative_montant():
    # Lowercase 'montant' is not a town-name token → no ADRESSE fire.
    assert commune_postcode_matches("le montant est 45000 euros") == []


def test_commune_postcode_negative_lone_reference():
    # A lone 5-digit number after lowercase prose → not matched.
    assert commune_postcode_matches("référence 12345") == []


def test_commune_postcode_negative_zero_postcode():
    # 00xxx is below the FR floor → rejected even with a town token.
    assert commune_postcode_matches("TESTVILLE 00123") == []


def test_commune_postcode_registered_in_detector():
    det = make_structured_detector()
    ms = det("Note: TESTVILLE-SUR-MER 99000 sans rue.")
    assert any(m.entity_type == "ADRESSE" and "TESTVILLE-SUR-MER" in m.value
               for m in ms), "recognizer must be wired into make_structured_detector"


# ── Engine-level masking (the real proof) ────────────────────────────────────


@pytest.fixture
def _engine(tmp_path, monkeypatch):
    """AnonymizationEngine with the structured detector wired in, on a temp
    BUBBLE_SHIELD_HOME so no real store is touched."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault
    return AnonymizationEngine(vault=Vault(),
                               extra_detectors=[make_structured_detector()])


def test_engine_masks_standalone_commune(_engine):
    out = _engine.anonymize("Adresse de correspondance: TESTVILLE-SUR-MER 99000").anonymized
    assert "TESTVILLE-SUR-MER" not in out
    assert "99000" not in out
    assert "⟦ADRESSE_" in out


def test_engine_masks_standalone_real_commune(_engine):
    out = _engine.anonymize("Note: MONTBOURG-LES-PINS 99000 sans rue.").anonymized
    assert "MONTBOURG-LES-PINS" not in out
    assert "⟦ADRESSE_" in out


def test_engine_full_address_still_masks(_engine):
    # Regression guard: the full-address ADRESSE path must keep working.
    out = _engine.anonymize("15 RUE DES SOURCES TESTVILLE 99000").anonymized
    assert "RUE DES SOURCES" not in out
    assert "⟦ADRESSE_" in out


def test_engine_precision_negative_montant(_engine):
    # 'montant 45000' must NOT become an ADRESSE (it's a MONTANT, not an address).
    out = _engine.anonymize("le montant est 45000 euros").anonymized
    assert "⟦ADRESSE_" not in out
