"""Custom recognizer loading tests — synthetic data only."""
import json

import pytest

from bubble_shield import custom_recognizers as cr


def _write_cfg(tmp_path, cfg) -> str:
    p = tmp_path / "custom_fields.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def test_returns_empty_when_no_config(tmp_path):
    missing = str(tmp_path / "does_not_exist.json")
    assert cr.load_custom_recognizers(missing) == []


def test_loads_valid_entry(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "DOSSIER_CODE", "label": "Code dossier",
             "pattern": r"\b[A-Z]{2}-\d{5}-[A-Z]\b"}
        ]
    })
    recs = cr.load_custom_recognizers(path)
    assert len(recs) == 1
    assert recs[0].entity_type == "DOSSIER_CODE"


def test_redos_guard_rejects_nested_quantifier(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "EVIL", "pattern": r"(\w+)+"}
        ]
    })
    assert cr.load_custom_recognizers(path) == []


def test_custom_recognizer_matches_synthetic_doc(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "DOSSIER_CODE", "pattern": r"\b[A-Z]{2}-\d{5}-[A-Z]\b"}
        ]
    })
    rec = cr.load_custom_recognizers(path)[0]
    matches = rec.find("Marc DURAND, dossier ref: AB-12345-X pour suivi.")
    assert any(m.value == "AB-12345-X" for m in matches)


def test_invalid_entity_type_skipped(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "lower_case", "pattern": r"\d+"},
            {"entity_type": "OK_TYPE", "pattern": r"\d+"},
        ]
    })
    recs = cr.load_custom_recognizers(path)
    assert [r.entity_type for r in recs] == ["OK_TYPE"]


def test_invalid_regex_skipped(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "BAD", "pattern": r"([unclosed"},
            {"entity_type": "GOOD", "pattern": r"\d{3}"},
        ]
    })
    recs = cr.load_custom_recognizers(path)
    assert [r.entity_type for r in recs] == ["GOOD"]


def test_known_validator_attached(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "CARD", "pattern": r"\d{16}", "validator": "luhn"}
        ]
    })
    recs = cr.load_custom_recognizers(path)
    assert len(recs) == 1
    assert recs[0].validator is not None


def test_unknown_validator_not_eval(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "X", "pattern": r"\d+", "validator": "__import__('os').system"}
        ]
    })
    recs = cr.load_custom_recognizers(path)
    # Either the entry is skipped or the validator is left None — never eval'd.
    for r in recs:
        assert r.validator is None or callable(r.validator)
    # Crucially, no callable smuggled in from the string name.
    assert all(getattr(r.validator, "__name__", "") != "system" for r in recs)


def test_priority_clamped(tmp_path):
    path = _write_cfg(tmp_path, {
        "regex_fields": [
            {"entity_type": "HIGH", "pattern": r"\d+", "priority": 500}
        ]
    })
    recs = cr.load_custom_recognizers(path)
    assert recs[0].priority <= 99


def test_load_config_returns_empty_for_missing(tmp_path):
    assert cr.load_custom_fields_config(str(tmp_path / "nope.json")) == {}
