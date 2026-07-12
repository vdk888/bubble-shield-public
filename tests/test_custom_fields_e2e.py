"""End-to-end: a custom regex recognizer flows through the engine and cloaks
the custom entity in synthetic text. Synthetic data only."""
import json

from bubble_shield import custom_recognizers as cr
from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault


def _load_recs(tmp_path):
    p = tmp_path / "custom_fields.json"
    p.write_text(json.dumps({
        "regex_fields": [
            {"entity_type": "DOSSIER_CODE", "label": "Code dossier",
             "pattern": r"\b[A-Z]{2}-\d{5}-[A-Z]\b"}
        ]
    }), encoding="utf-8")
    return cr.load_custom_recognizers(str(p))


def test_custom_field_is_cloaked(tmp_path):
    recs = _load_recs(tmp_path)
    engine = AnonymizationEngine(vault=Vault(mission="e2e"), extra_recognizers=recs)
    text = "Marc DURAND, dossier ref: AB-12345-X."
    res = engine.anonymize(text)

    assert "AB-12345-X" not in res.anonymized
    assert any(e.entity_type == "DOSSIER_CODE" for e in res.entities)


def test_custom_field_roundtrips(tmp_path):
    recs = _load_recs(tmp_path)
    engine = AnonymizationEngine(vault=Vault(mission="e2e-rt"), extra_recognizers=recs)
    text = "dossier ref: AB-12345-X"
    res = engine.anonymize(text)
    assert engine.deanonymize(res.anonymized) == text


def test_custom_field_does_not_steal_iban_span(tmp_path):
    # A greedy custom pattern must not carve a checksum-valid IBAN.
    p = tmp_path / "custom_fields.json"
    p.write_text(json.dumps({
        "regex_fields": [
            {"entity_type": "GREEDY", "pattern": r"FR\d{2}", "priority": 99}
        ]
    }), encoding="utf-8")
    recs = cr.load_custom_recognizers(str(p))
    engine = AnonymizationEngine(vault=Vault(mission="e2e-iban"), extra_recognizers=recs)
    text = "IBAN FR7630006000011234567890189 du client."
    res = engine.anonymize(text)
    # The full IBAN should be cloaked as one IBAN span (length-first overlap).
    assert "FR7630006000011234567890189" not in res.anonymized
    assert any(e.entity_type == "IBAN" for e in res.entities)
