"""
test_390_vault_seeds_denylist.py — #390 LEAK FIX: vault values seed the deny-list.

The leak (verified by Joris, 2026-06-29): a name confidently detected in doc 1
is mapped in the per-mission vault (Dupont → NOM_0001) but NEVER fed to the
deny-list gazetteer. On doc 2 for the SAME client, if the probabilistic NER
(GLiNER) MISSES the name, it leaks in clear — even though we already KNOW it's
PII (it sits in the vault from doc 1).

The fix (Option A): when a doc is anonymised, every IDENTIFYING value now in the
vault is confirmed PII → auto-seed it into the gazetteer via
`known_pii_store.seed_vault_into_gazetteer` (the no-gate explicit-add path). The
already-wired known-PII recognizer then catches it deterministically in every
subsequent doc, regardless of NER.

ALL names are SYNTHETIC ("Zorgwick Bramblesnap" et al.) — they correspond to no
real person. Every test passes an explicit temp gazetteer path so the real
~/.bubble_shield/ store is NEVER touched.
"""
from __future__ import annotations

from pathlib import Path

from bubble_shield.engine import AnonymizationEngine
from bubble_shield.known_pii_recognizer import make_known_pii_recognizer
from bubble_shield.known_pii_store import (
    load_gazetteer,
    seed_vault_into_gazetteer,
)
from bubble_shield.vault import Vault


# ════════════════════════════════════════════════════════════════════════════
# UNIT TEST — only IDENTIFYING vault values are seeded; kept types are NOT.
# ════════════════════════════════════════════════════════════════════════════

def test_seed_only_identifying_types(tmp_path: Path) -> None:
    """Build a vault holding both identifying (NOM, EMAIL, IBAN) and a NON-
    identifying kept value (MONTANT). After seeding, the identifying values are
    in the gazetteer; the MONTANT is NOT (derived from ENTITY_CATALOG, not a
    hardcoded list)."""
    gaz = tmp_path / "known_pii.json"

    vault = Vault(mission="test-390-unit")
    # Mint tokens exactly as the engine does for detected spans.
    vault.token_for("Zorgwick Bramblesnap", "NOM")
    vault.token_for("zorgwick@example.test", "EMAIL")
    vault.token_for("FR7630006000011234567890189", "IBAN")
    vault.token_for("12 345,67 €", "MONTANT")  # kept / non-identifying

    added = seed_vault_into_gazetteer(vault, path=gaz)
    assert added == 3, f"expected 3 identifying values seeded, got {added}"

    gz = load_gazetteer(path=gaz)
    assert gz.contains("Zorgwick Bramblesnap"), "NOM should be seeded"
    assert gz.contains("zorgwick@example.test"), "EMAIL should be seeded"
    assert gz.contains("FR7630006000011234567890189"), "IBAN should be seeded"
    assert not gz.contains("12 345,67 €"), "MONTANT (non-identifying) must NOT be seeded"


def test_seed_is_idempotent(tmp_path: Path) -> None:
    """Re-seeding the same vault adds nothing new (add_confirmed_pii dedupes
    case-insensitively)."""
    gaz = tmp_path / "known_pii.json"
    vault = Vault(mission="test-390-idem")
    vault.token_for("Zorgwick Bramblesnap", "NOM")

    assert seed_vault_into_gazetteer(vault, path=gaz) == 1
    assert seed_vault_into_gazetteer(vault, path=gaz) == 0, "second seed must add nothing"


# ════════════════════════════════════════════════════════════════════════════
# THE ACCEPTANCE GATE — simulate the real leak end-to-end through the engine.
#
#   doc 1: synthetic name detected + vaulted + seeded into the gazetteer.
#   doc 2: SAME name, but NER DISABLED (no detectors wired) so ONLY the deny-list
#          can catch it → assert the name is STILL MASKED, not leaked in clear.
#
# This proves the fix closes the leak.
# ════════════════════════════════════════════════════════════════════════════

def test_acceptance_gate_ner_miss_still_masked(tmp_path: Path) -> None:
    gaz = tmp_path / "known_pii.json"
    name = "Zorgwick Bramblesnap"

    # ---- doc 1: name is detected + vaulted (simulate the engine's detect path
    #            by minting the token the way engine.anonymize does), then the
    #            #390 hook seeds the vault into the gazetteer.
    vault_doc1 = Vault(mission="test-390-accept")
    vault_doc1.token_for(name, "NOM")
    seeded = seed_vault_into_gazetteer(vault_doc1, path=gaz)
    assert seeded == 1, "the doc-1 vaulted name must be seeded into the gazetteer"

    # sanity: the gazetteer now knows the name
    assert load_gazetteer(path=gaz).contains(name)

    # ---- doc 2: SAME name, NER OFF. We wire ONLY the gazetteer-fed known-PII
    #            recognizer and NO detectors (extra_detectors=[] and no GLiNER),
    #            so the ONLY thing that can mask the name is the deny-list. If the
    #            fix works, the name is masked anyway; if it didn't, it leaks.
    recognizer = make_known_pii_recognizer(path=gaz)
    assert recognizer is not None, "gazetteer has an entry → recognizer must exist"

    engine = AnonymizationEngine(
        vault=Vault(mission="test-390-accept-doc2"),
        extra_recognizers=[recognizer],
    )
    # Force NER to miss: bare name, no civility, no context the regex NOM would
    # catch — exactly the case where GLiNER is the only hope and it whiffed.
    doc2 = "Veuillez transmettre le dossier à Zorgwick Bramblesnap avant vendredi."
    result = engine.anonymize(doc2)

    assert name not in result.anonymized, (
        "LEAK NOT CLOSED: NER-missed known name leaked in clear on doc 2.\n"
        f"anonymized output: {result.anonymized!r}"
    )
    assert "NOM_" in result.anonymized, "the name should be masked as a NOM token"

    # reversibility intact: doc-2 vault restores the masked name
    restored = engine.deanonymize(result.anonymized)
    assert name in restored


def test_acceptance_gate_baseline_without_seed_leaks(tmp_path: Path) -> None:
    """Negative control: WITHOUT seeding, the same NER-off doc-2 LEAKS the name.
    This proves the acceptance gate above is meaningful (the masking comes from
    the seed, not from some other path)."""
    gaz = tmp_path / "known_pii.json"  # empty — no seed
    name = "Zorgwick Bramblesnap"

    recognizer = make_known_pii_recognizer(path=gaz)
    # empty gazetteer → recognizer is None (zero-cost noop), so no deny-list
    extra = [recognizer] if recognizer is not None else []

    engine = AnonymizationEngine(
        vault=Vault(mission="test-390-baseline"),
        extra_recognizers=extra,
    )
    doc2 = "Veuillez transmettre le dossier à Zorgwick Bramblesnap avant vendredi."
    result = engine.anonymize(doc2)

    assert name in result.anonymized, (
        "baseline expected to LEAK (no seed, NER off) — if it's masked here the "
        "acceptance gate isn't isolating the seed's effect"
    )
