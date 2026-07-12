"""
test_task2_depollute_allowlist.py — Task 2 entity-type allowlist (the P0).

De-pollution may ONLY reach the judge for allowlisted entity types
{NOM, POSTE, ADRESSE}. Every other entity type (IBAN, SIRET, SECU, EMAIL,
TEL, NUM_*, LIEU_NAISSANCE, DATE_NAISSANCE, RAISON_SOCIALE, URL,
PIECE_IDENTITE, ...) must be left MASKED, untouched, and MUST NEVER be
passed to `classify_fn` nor auto-unmasked — not via the classifier lane,
not via the junk lane.

This is the structural safety guarantee of Task 2 (root cause of #589):
a name-focused judge would wrongly un-mask an IBAN as "not a name", so
non-name types never reach it at all.

All PII in this file is SYNTHETIC. No real client values anywhere.
"""
from __future__ import annotations

from bubble_shield import known_pii_store as kps
from bubble_shield.depollute import DEPOLLUTE_ALLOWLIST, depollute_gazetteer


class _RecordingClassifier:
    """Fake classify_fn that records EXACTLY the tokens it was handed and
    returns MOT for every one (i.e. it WANTS to un-mask everything). If a
    non-allowlisted value ever reaches it, this fake would un-mask it and the
    assertions below would catch both the leak into the classifier AND the
    resulting un-mask."""

    def __init__(self):
        self.seen: list[str] = []

    def __call__(self, tokens):
        toks = list(tokens)
        self.seen.extend(toks)
        return [{"token": t, "verdict": "MOT"} for t in toks]


def _seed_typed(tmp_path, typed):
    p = tmp_path / "gaz.json"
    for value, etype in typed:
        kps.add_confirmed_pii(value, etype, path=p)
    return p


def test_allowlist_constant_is_exactly_nom_poste_adresse():
    assert DEPOLLUTE_ALLOWLIST == {"NOM", "POSTE", "ADRESSE"}


def test_only_allowlisted_types_reach_classifier_the_p0(tmp_path):
    # A gazetteer mixing allowlisted name-ish types with the dangerous
    # structured-PII types. The recording classifier records everything it
    # sees. NON-allowlisted types must NEVER appear in what it saw and must
    # remain masked afterward.
    # POSTE/ADRESSE/NOM values are all capitalized here so they route through
    # the "uncertain" lane (classifier) rather than the lowercase junk lane —
    # proving each allowlisted TYPE actually reaches the judge.
    gaz = _seed_typed(
        tmp_path,
        [
            ("Jean Dupont", "NOM"),
            ("Cadre De La Mission", "POSTE"),
            ("12 Rue Des Lilas 75011 Paris", "ADRESSE"),
            ("FR7612345678901234567890123", "IBAN"),
            ("12345678901234", "SIRET"),
            ("alice@example.fr", "EMAIL"),
            ("Paris", "LIEU_NAISSANCE"),
        ],
    )
    q = tmp_path / "queue.json"
    clf = _RecordingClassifier()

    res = depollute_gazetteer(clf, gaz_path=gaz, queue_path=q)

    # P0 ASSERTION: the classifier saw ONLY the allowlisted values. No IBAN,
    # SIRET, EMAIL or LIEU_NAISSANCE value was ever handed to it.
    assert set(clf.seen) == {
        "Jean Dupont",
        "Cadre De La Mission",
        "12 Rue Des Lilas 75011 Paris",
    }
    for forbidden in (
        "FR7612345678901234567890123",
        "12345678901234",
        "alice@example.fr",
        "Paris",
    ):
        assert forbidden not in clf.seen, (
            f"non-allowlisted value {forbidden!r} reached the classifier"
        )

    # Non-allowlisted entries stay in the gazetteer (masked). The allowlisted
    # ones were un-masked (classifier said MOT for all).
    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "FR7612345678901234567890123" in remaining
    assert "12345678901234" in remaining
    assert "alice@example.fr" in remaining
    assert "Paris" in remaining
    assert "Jean Dupont" not in remaining
    assert "Cadre De La Mission" not in remaining
    assert "12 Rue Des Lilas 75011 Paris" not in remaining

    # Bookkeeping: only allowlisted entries appear in unmasked; non-allowlisted
    # never appear in unmasked OR kept (they are skipped entirely).
    assert set(res["unmasked"]) == {
        "Jean Dupont",
        "Cadre De La Mission",
        "12 Rue Des Lilas 75011 Paris",
    }
    for forbidden in (
        "FR7612345678901234567890123",
        "12345678901234",
        "alice@example.fr",
        "Paris",
    ):
        assert forbidden not in res["unmasked"]
        assert forbidden not in res["kept"]


def test_raison_sociale_excluded_kept_masked(tmp_path):
    # RAISON_SOCIALE is deliberately EXCLUDED from the allowlist (Joris:
    # raison sociale = PII, keep masked). It must never reach the classifier.
    gaz = _seed_typed(
        tmp_path,
        [
            ("SARL Lumière Patrimoine", "RAISON_SOCIALE"),
            ("Jean Dupont", "NOM"),
        ],
    )
    q = tmp_path / "queue.json"
    clf = _RecordingClassifier()

    res = depollute_gazetteer(clf, gaz_path=gaz, queue_path=q)

    assert "SARL Lumière Patrimoine" not in clf.seen
    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "SARL Lumière Patrimoine" in remaining  # stays masked
    assert "SARL Lumière Patrimoine" not in res["unmasked"]
    assert "SARL Lumière Patrimoine" not in res["kept"]
