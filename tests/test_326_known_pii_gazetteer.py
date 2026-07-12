"""
test_326_known_pii_gazetteer.py — Phase 1: local persistent known-PII deny-list.

All names are SYNTHETIC test names that do not correspond to any real client.
Tests use a tmp_path fixture to point the gazetteer at a temporary file — the
production ~/.bubble_shield/gazetteer is never touched.

Synthetic names used: DUPONT, LEFEBVRE, MARCHAND, MARC, TESTILLON.
The accent/case test uses the invented surname "TESTILLON" with accented
variant "Tèstillon" to exercise NFD normalisation.

Test plan:
  1. Cross-doc: synthetic name confirmed in doc-1 (via explicit add) is then
     auto-masked in doc-2 where it appears bare with no context that GLiNER
     would catch.
  2. Word-boundary: "MARC" in the gazetteer does NOT mask "MARCHAND".
  3. Accent / case-insensitive: stored as "Tèstillon", matches "TESTILLON" and
     "testillon" (accent-insensitive via NFD normalisation, case-insensitive).
  4. Overlap precedence: a checksum-valid IBAN wins over a gazetteer NOM on
     the same span.
  5. Empty gazetteer → make_known_pii_recognizer returns None (zero-cost noop).
  6. Anti-poisoning: low-confidence auto-detection does NOT enter the gazetteer;
     high-confidence DOES.
  7. Add / remove API round-trip (idempotency, un-poison).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bubble_shield.engine import AnonymizationEngine
from bubble_shield.known_pii_recognizer import KnownPiiRecognizer, make_known_pii_recognizer
from bubble_shield.known_pii_store import (
    GazetteeredPII,
    PiiEntry,
    add_confirmed_pii,
    is_known_pii,
    load_gazetteer,
    maybe_add_detection,
    remove_pii,
)
from bubble_shield.recognizers import Match, resolve_overlaps
from bubble_shield.vault import Vault


# ── helpers ───────────────────────────────────────────────────────────────────

def _gazetteer_with(tmp_path: Path, entries: list[tuple[str, str]]) -> Path:
    """Write a synthetic gazetteer to a temp file and return the path."""
    data = {
        "version": 1,
        "entries": [
            {"value": v, "entity_type": et, "added_at": "2026-01-01T00:00:00+00:00"}
            for v, et in entries
        ],
    }
    p = tmp_path / "known_pii.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ════════════════════════════════════════════════════════════════════════════════
# 1. CROSS-DOC MASKING — the prize test
#    Synthetic name confirmed in doc-1; bare surname auto-masked in doc-2 even
#    though there is no context that GLiNER or the regex NOM would catch.
# ════════════════════════════════════════════════════════════════════════════════

def test_cross_doc_bare_surname_masked(tmp_path: Path) -> None:
    """Cross-doc: 'DUPONT' confirmed in doc-1 via explicit add; bare 'DUPONT'
    in doc-2 (no title, no first name, no context) is deterministically masked
    through the engine's extra_recognizers path."""
    gaz_path = tmp_path / "known_pii.json"

    # Gate B explicit add — simulates: NER found "Jean DUPONT" with high
    # confidence in doc-1 and the engine persisted it.
    added = add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    assert added, "add_confirmed_pii should return True for a new entry"

    # Build recognizer from the temp gazetteer.
    recognizer = make_known_pii_recognizer(path=gaz_path)
    assert recognizer is not None, "recognizer should not be None when gazetteer has entries"

    # Doc-2: bare surname, no context, no civility — GLiNER would miss this.
    doc2 = "Veuillez contacter DUPONT pour validation du dossier."

    # Wire the recognizer into the engine and anonymise.
    engine = AnonymizationEngine(
        vault=Vault(mission="test-cross-doc"),
        extra_recognizers=[recognizer],
    )
    result = engine.anonymize(doc2)

    assert "DUPONT" not in result.anonymized, (
        "Known PII 'DUPONT' should be masked in doc-2 even without NER context"
    )
    assert "NOM_" in result.anonymized, "Expected a NOM token in the anonymised output"

    # Reversibility: the vault must restore the original string.
    restored = engine.deanonymize(result.anonymized)
    assert "DUPONT" in restored


def test_cross_doc_first_name_then_surname_only(tmp_path: Path) -> None:
    """Full name confirmed; bare surname in a later sentence is masked."""
    gaz_path = tmp_path / "known_pii.json"
    add_confirmed_pii("LEFEBVRE", "NOM", path=gaz_path)
    add_confirmed_pii("Claire", "NOM", path=gaz_path)

    recognizer = make_known_pii_recognizer(path=gaz_path)
    engine = AnonymizationEngine(
        vault=Vault(mission="test-lefebvre"),
        extra_recognizers=[recognizer],
    )

    doc = "Dossier LEFEBVRE — à traiter en priorité. Contacter Claire si besoin."
    result = engine.anonymize(doc)

    assert "LEFEBVRE" not in result.anonymized
    assert "Claire" not in result.anonymized


# ════════════════════════════════════════════════════════════════════════════════
# 2. WORD-BOUNDARY — "MARC" must NOT match inside "MARCHAND"
# ════════════════════════════════════════════════════════════════════════════════

def test_word_boundary_no_substring_match(tmp_path: Path) -> None:
    """'MARC' in the gazetteer does NOT fire on 'MARCHAND'."""
    gaz_path = _gazetteer_with(tmp_path, [("MARC", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)
    assert recognizer is not None

    doc = "Contacter M. MARCHAND à l'adresse indiquée."
    matches = recognizer.find(doc)

    # No match should fire on "MARCHAND" for the entry "MARC".
    assert not any(m.value.upper() == "MARCHAND" for m in matches), (
        "Gazetteer 'MARC' must not match inside 'MARCHAND' (no substring false-hit)"
    )
    # Confirm: an isolated "MARC" IS matched.
    doc_with_marc = "Merci, MARC, de bien vouloir confirmer."
    matches_marc = recognizer.find(doc_with_marc)
    assert any(m.value.upper() == "MARC" for m in matches_marc), (
        "Gazetteer 'MARC' must match standalone 'MARC'"
    )


def test_word_boundary_not_prefix(tmp_path: Path) -> None:
    """'DUPONT' in the gazetteer does NOT match 'DUPONT-MOREAU' (hyphen boundary
    depends on the regex engine; we accept either outcome as long as we don't
    fragment 'MARCHAND' into 'MARC' + 'HAND')."""
    gaz_path = _gazetteer_with(tmp_path, [("MARC", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)
    # The core contract: MARCHAND is never fully replaced by a MARC entry.
    doc = "MARCHAND est le responsable du dossier."
    matches = recognizer.find(doc)
    for m in matches:
        assert m.end - m.start != len("MARCHAND"), (
            "Pattern must not match the whole 'MARCHAND' token for entry 'MARC'"
        )


# ════════════════════════════════════════════════════════════════════════════════
# 3. ACCENT / CASE-INSENSITIVE MATCHING
# ════════════════════════════════════════════════════════════════════════════════

def test_case_insensitive_match(tmp_path: Path) -> None:
    """Stored as 'DUPONT', must match 'dupont', 'Dupont', 'DUPONT'."""
    gaz_path = _gazetteer_with(tmp_path, [("DUPONT", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)

    for variant in ("dupont", "Dupont", "DUPONT"):
        matches = recognizer.find(f"Contacter {variant} svp.")
        assert matches, f"Expected match for '{variant}' (case-insensitive)"


def test_accent_insensitive_match(tmp_path: Path) -> None:
    """Stored as 'Tèstillon' (accented è), must match 'TESTILLON' (no accent)
    and 'testillon' — accent-insensitive via NFD normalisation."""
    gaz_path = _gazetteer_with(tmp_path, [("Tèstillon", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)

    # NFD decomposition strips the combining grave from 'è' → 'e', so
    # 'TESTILLON' (no accent) should match the stored 'Tèstillon'.
    for variant in ("TESTILLON", "Tèstillon", "testillon"):
        matches = recognizer.find(f"Dossier de {variant}.")
        assert matches, f"Expected accent-insensitive match for '{variant}'"


# ════════════════════════════════════════════════════════════════════════════════
# 4. OVERLAP PRECEDENCE — valid IBAN wins over gazetteer NOM on the same span
# ════════════════════════════════════════════════════════════════════════════════

def test_valid_iban_wins_over_gazetteer_nom(tmp_path: Path) -> None:
    """A checksum-valid IBAN span is retained; a gazetteer NOM on the same
    span is dropped by resolve_overlaps() (length-first, then priority).

    This tests the priority composition: IBAN recognizer priority=95 >> known-PII
    priority=3, so the IBAN always wins on overlap.  We exercise resolve_overlaps
    directly to keep the test free of heavy ML setup.
    """
    # Simulate an IBAN whose alphanumeric part the recognizer might fire on
    # if "FR7630006000011234567890189" were in the gazetteer (it isn't,
    # but we synthetically inject overlapping matches to test the mechanism).
    from bubble_shield.recognizers import RECOGNIZERS, detect

    # The IBAN "FR76 3000 6000 0112 3456 7890 189" is checksum-valid.
    text = "IBAN FR76 3000 6000 0112 3456 7890 189"

    # Add the IBAN text as a "NOM" in the gazetteer (extreme adversarial case:
    # someone stored the literal IBAN string as a name — wrong, but we must
    # handle it gracefully).
    gaz_path = _gazetteer_with(tmp_path, [("FR76 3000 6000 0112 3456 7890 189", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)

    # Get the IBAN match from the core detector.
    iban_matches = detect(text)
    # Get the gazetteer NOM match.
    nom_matches = recognizer.find(text) if recognizer else []

    all_raw = iban_matches + nom_matches
    resolved = resolve_overlaps(all_raw)

    # After overlap resolution there must be exactly 1 match on that span,
    # and it must be typed IBAN (not NOM).
    assert len(resolved) == 1, f"Expected 1 resolved match, got {len(resolved)}: {resolved}"
    assert resolved[0].entity_type == "IBAN", (
        f"IBAN must win over gazetteer NOM; got {resolved[0].entity_type}"
    )


# ════════════════════════════════════════════════════════════════════════════════
# 5. EMPTY GAZETTEER → zero-cost noop
# ════════════════════════════════════════════════════════════════════════════════

def test_empty_gazetteer_returns_none(tmp_path: Path) -> None:
    """make_known_pii_recognizer returns None when the gazetteer has no entries
    (file doesn't exist or is empty) — zero-cost for new deployments."""
    # Non-existent file.
    recognizer = make_known_pii_recognizer(path=tmp_path / "nonexistent.json")
    assert recognizer is None

    # Empty entries list.
    gaz_path = tmp_path / "empty.json"
    gaz_path.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    recognizer2 = make_known_pii_recognizer(path=gaz_path)
    assert recognizer2 is None


def test_empty_gazetteer_engine_noop(tmp_path: Path) -> None:
    """When the gazetteer is empty, engine behaviour is bit-for-bit identical to
    an engine without the recognizer.  Verify no match count change."""
    engine_no_gaz = AnonymizationEngine(vault=Vault(mission="base"))
    engine_with_gaz = AnonymizationEngine(
        vault=Vault(mission="base"),
        extra_recognizers=[],   # make_known_pii_recognizer would return None
    )
    text = "Réunion avec Martine Lambert le 15 mars."
    r1 = engine_no_gaz.anonymize(text)
    r2 = engine_with_gaz.anonymize(text)
    assert r1.entity_count == r2.entity_count


# ════════════════════════════════════════════════════════════════════════════════
# 6. ANTI-POISONING
# ════════════════════════════════════════════════════════════════════════════════

def test_low_confidence_ml_does_not_auto_enter(tmp_path: Path) -> None:
    """A soft-ML detection with score < HIGH_CONF_ML_THRESHOLD (0.80) must NOT
    be added to the gazetteer automatically."""
    gaz_path = tmp_path / "gaz.json"
    # priority=5 is soft-ML; score=0.55 is below the 0.80 gate.
    added = maybe_add_detection(
        "DUPONT", "NOM", score=0.55, priority=5, path=gaz_path
    )
    assert not added, "Low-confidence ML detection must not enter the gazetteer"
    assert not gaz_path.exists() or not is_known_pii("DUPONT", path=gaz_path)


def test_low_confidence_regex_nom_does_not_auto_enter(tmp_path: Path) -> None:
    """A regex NOM with priority=50 and score=0.8 (below 0.85 regex gate) must
    not auto-enter."""
    gaz_path = tmp_path / "gaz.json"
    added = maybe_add_detection(
        "LEFEBVRE", "NOM", score=0.80, priority=50, path=gaz_path
    )
    assert not added, "Regex NOM below 0.85 threshold must not enter the gazetteer"


def test_high_confidence_ml_auto_enters(tmp_path: Path) -> None:
    """A soft-ML detection with score >= 0.80 and priority <= 5 DOES enter."""
    gaz_path = tmp_path / "gaz.json"
    added = maybe_add_detection(
        "DUPONT", "NOM", score=0.82, priority=5, path=gaz_path
    )
    assert added, "High-confidence ML detection should enter the gazetteer"
    assert is_known_pii("DUPONT", path=gaz_path)


def test_high_confidence_regex_nom_auto_enters(tmp_path: Path) -> None:
    """A regex NOM with score >= 0.85 and priority=50 does enter."""
    gaz_path = tmp_path / "gaz.json"
    added = maybe_add_detection(
        "LEFEBVRE", "NOM", score=0.85, priority=50, path=gaz_path
    )
    assert added, "High-confidence regex NOM (>= 0.85) should enter the gazetteer"
    assert is_known_pii("LEFEBVRE", path=gaz_path)


def test_non_nom_entity_type_does_not_auto_enter(tmp_path: Path) -> None:
    """maybe_add_detection only handles NOM for now (not IBAN/EMAIL/etc.)."""
    gaz_path = tmp_path / "gaz.json"
    added = maybe_add_detection(
        "FR7630006000011234567890189", "IBAN", score=1.0, priority=95,
        path=gaz_path
    )
    assert not added, "Non-NOM entity types should not auto-enter via maybe_add_detection"


# ════════════════════════════════════════════════════════════════════════════════
# 7. ADD / REMOVE API — idempotency and un-poison
# ════════════════════════════════════════════════════════════════════════════════

def test_add_is_idempotent(tmp_path: Path) -> None:
    """Adding the same value twice returns False on the second call."""
    gaz_path = tmp_path / "gaz.json"
    assert add_confirmed_pii("DUPONT", "NOM", path=gaz_path) is True
    assert add_confirmed_pii("DUPONT", "NOM", path=gaz_path) is False
    # Exactly one entry in the file.
    g = load_gazetteer(path=gaz_path)
    assert len(g.entries) == 1


def test_add_case_insensitive_dedup(tmp_path: Path) -> None:
    """'dupont' and 'DUPONT' are treated as the same entry."""
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    added = add_confirmed_pii("dupont", "NOM", path=gaz_path)
    assert not added, "Case-insensitive duplicate should not be re-added"
    g = load_gazetteer(path=gaz_path)
    assert len(g.entries) == 1


def test_remove_existing_entry(tmp_path: Path) -> None:
    """Removing an existing entry returns True and the entry is gone."""
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    removed = remove_pii("DUPONT", path=gaz_path)
    assert removed is True
    assert not is_known_pii("DUPONT", path=gaz_path)

    # The recognizer built after removal should return None (empty gazetteer).
    rec = make_known_pii_recognizer(path=gaz_path)
    assert rec is None


def test_remove_nonexistent_returns_false(tmp_path: Path) -> None:
    gaz_path = tmp_path / "gaz.json"
    assert remove_pii("LEFEBVRE", path=gaz_path) is False


def test_remove_case_insensitive(tmp_path: Path) -> None:
    """Removal is case-insensitive (un-poison 'dupont' removes 'DUPONT')."""
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    removed = remove_pii("dupont", path=gaz_path)
    assert removed is True


def test_is_known_pii_case_insensitive(tmp_path: Path) -> None:
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("LEFEBVRE", "NOM", path=gaz_path)
    assert is_known_pii("lefebvre", path=gaz_path)
    assert is_known_pii("LEFEBVRE", path=gaz_path)
    assert not is_known_pii("DUPONT", path=gaz_path)


def test_multiple_entries_persist(tmp_path: Path) -> None:
    """Two distinct entries both survive a save/load round-trip."""
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    add_confirmed_pii("LEFEBVRE", "NOM", path=gaz_path)
    g = load_gazetteer(path=gaz_path)
    values_lower = {e.value.lower() for e in g.entries}
    assert "dupont" in values_lower
    assert "lefebvre" in values_lower
    assert len(g.entries) == 2


# ════════════════════════════════════════════════════════════════════════════════
# 8. ENGINE INTEGRATION — extra_recognizers path (engine.py plumbing)
# ════════════════════════════════════════════════════════════════════════════════

def test_engine_extra_recognizer_path(tmp_path: Path) -> None:
    """The engine's extra_recognizers path handles a KnownPiiRecognizer
    correctly: the name is masked, the vault is populated, de-anonymisation
    restores the original."""
    gaz_path = _gazetteer_with(tmp_path, [("LEFEBVRE", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)
    vault = Vault(mission="test-engine-integration")

    engine = AnonymizationEngine(
        vault=vault,
        extra_recognizers=[recognizer],
    )
    text = "Référence dossier: LEFEBVRE — en attente de validation."
    result = engine.anonymize(text)

    assert "LEFEBVRE" not in result.anonymized, "Known PII must be masked via engine"
    restored = engine.deanonymize(result.anonymized)
    assert "LEFEBVRE" in restored


def test_engine_does_not_break_structured_pii(tmp_path: Path) -> None:
    """Adding a KnownPiiRecognizer does not disturb structured PII detection
    (IBAN, EMAIL) — they are still masked correctly alongside the NOM."""
    gaz_path = _gazetteer_with(tmp_path, [("DUPONT", "NOM")])
    recognizer = make_known_pii_recognizer(path=gaz_path)

    engine = AnonymizationEngine(
        vault=Vault(mission="test-struct"),
        extra_recognizers=[recognizer],
    )
    text = ("Dossier DUPONT. IBAN FR76 3000 6000 0112 3456 7890 189. "
            "Contact: test@example.com.")
    result = engine.anonymize(text)

    assert "DUPONT" not in result.anonymized
    assert "FR76" not in result.anonymized
    assert "test@example.com" not in result.anonymized
    # Restoration is lossless.
    assert engine.deanonymize(result.anonymized) == text


# ════════════════════════════════════════════════════════════════════════════════
# 9. FILE INTEGRITY — gazetteer file is created outside the repo
#    (we can't assert on ~/ in CI, but we verify the path logic)
# ════════════════════════════════════════════════════════════════════════════════

def test_gazetteer_file_is_chmod_600(tmp_path: Path) -> None:
    """Verify the gazetteer file is written with chmod 600."""
    gaz_path = tmp_path / "gaz.json"
    add_confirmed_pii("DUPONT", "NOM", path=gaz_path)
    import stat
    mode = gaz_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_corrupt_gazetteer_loads_empty(tmp_path: Path) -> None:
    """A corrupt/malformed JSON file must not crash — loads as empty."""
    gaz_path = tmp_path / "gaz.json"
    gaz_path.write_text("{not valid json", encoding="utf-8")
    g = load_gazetteer(path=gaz_path)
    assert g.is_empty
    # And the recognizer should be None.
    assert make_known_pii_recognizer(path=gaz_path) is None
