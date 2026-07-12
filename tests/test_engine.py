from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault


def test_roundtrip_exact():
    e = AnonymizationEngine()
    txt = ("M. Jean Dupont, IBAN FR76 3000 6000 0112 3456 7890 189, "
           "jean@x.com, 45 000 €, tél 06 12 34 56 78.")
    r = e.anonymize(txt)
    assert e.deanonymize(r.anonymized) == txt          # lossless


def test_anonymized_text_has_no_clear_pii():
    e = AnonymizationEngine()
    r = e.anonymize("M. Jean Dupont — jean.dupont@example.com")
    assert "jean.dupont@example.com" not in r.anonymized
    assert "Jean Dupont" not in r.anonymized


def test_safe_when_all_high_confidence():
    e = AnonymizationEngine()
    r = e.anonymize("Virement de 45 000 € à jean@x.com.")
    assert r.safe_to_send and not r.has_residual


def test_trivial_text_is_nothing_to_do():
    # A SHORT no-PII one-liner (below the "substantial" bar) is genuinely
    # nothing-to-do: safe, green verdict, NOT the zero-detection caution.
    r = AnonymizationEngine().anonymize("Réunion à 14h, rien de sensible.")
    assert r.entity_count == 0 and r.safe_to_send
    assert r.verdict_state == "nothing_to_do"
    assert not r.substantial_text
    assert "rien à anonymiser" in r.verdict_fr


def test_zero_detection_on_substantial_doc_is_NOT_safe():
    # THE BUG (product-integrity fix 2026-07-02): a SUBSTANTIAL free-text document
    # in which the engine finds NOTHING must NOT be certified safe. "Found nothing"
    # is not "safe" — on real free text a name/address is often simply MISSED.
    # This text has enough prose (>= 8 words, >= 40 chars) and no regex-detectable
    # PII, exactly the false-negative shape the review battery hit.
    substantial_no_pii = (
        "Le dossier a été transmis au service concerné pour un examen "
        "approfondi et une décision sera rendue dans les meilleurs délais.")
    r = AnonymizationEngine().anonymize(substantial_no_pii)
    assert r.entity_count == 0
    assert r.substantial_text
    assert r.verdict_state == "zero_detection"
    # The core assertions: no green, no True.
    assert r.safe_to_send is False
    assert not r.verdict_fr.startswith("✓")
    assert "⚠️" in r.verdict_fr
    assert "ne garantit PAS" in r.verdict_fr


def test_masked_doc_still_safe_but_framed_as_revue():
    # A normal doc where entities WERE found and all masked is still the closest
    # to safe (True), but framed as "revue conseillée", never an absolute "✓ sûr".
    r = AnonymizationEngine().anonymize(
        "Virement de 45 000 € à jean@x.com pour solde de tout compte du dossier.")
    assert r.entity_count > 0
    assert r.safe_to_send is True
    assert r.verdict_state == "masked_ok"
    assert r.verdict_fr.startswith("✓")


def test_low_confidence_triggers_fail_closed():
    # An invalid IBAN (failed checksum) is a low-confidence detection → flag.
    e = AnonymizationEngine()
    r = e.anonymize("IBAN FR00 0000 0000 0000 0000 0000 000 du client.")
    assert not r.safe_to_send
    assert r.low_confidence


def test_residual_scan_flags_a_leak():
    # Force a leak: an engine that only knows EMAIL leaves the IBAN visible,
    # and the residual scan (full recognizers) must catch it.
    from bubble_shield.recognizers import RECOGNIZERS
    email_only = [r for r in RECOGNIZERS if r.entity_type == "EMAIL"]
    e = AnonymizationEngine(recognizers=email_only)
    # residual scan uses the SAME recognizer set, so to truly test the
    # mechanism we scan with the full set via a second engine:
    r = e.anonymize("IBAN FR76 3000 6000 0112 3456 7890 189")
    full = AnonymizationEngine()
    leftover = full._residual_scan(r.anonymized)
    assert any(m.entity_type == "IBAN" for m in leftover)


def test_consistent_token_across_repeats():
    e = AnonymizationEngine(vault=Vault(mission="m"))
    r = e.anonymize("jean@x.com puis encore jean@x.com")
    toks = [e2.token for e2 in r.entities]
    assert toks[0] == toks[1]            # same email → same token both times


def test_use_ner_graceful_without_presidio():
    # presidio not installed here → use_ner must behave exactly like regex-only.
    from bubble_shield import presidio_ext
    assert presidio_ext.is_available() is False
    txt = "M. Jean Dupont, jean@x.com, 45 000 €"
    a = AnonymizationEngine().anonymize(txt).anonymized
    b = AnonymizationEngine(use_ner=True).anonymize(txt).anonymized
    assert a == b
