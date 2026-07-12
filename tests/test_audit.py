"""
test_audit.py — append-only audit log (RGPD art. 5-2 / 30, accountability).

The audit log is a *processing record*: it proves WHAT was processed (counts &
entity types, mission, verdict) WITHOUT ever storing the PII values themselves.
A log that leaked the values it audits would defeat the privacy purpose, so the
key invariant tested here is: counts/types IN, raw values OUT.

Synthetic data only — the "client" below is the canonical M. Jean Dupont fixture.
"""
import json
import os
import stat

from bubble_shield.audit import append_entry, log_result, read_audit
from bubble_shield.engine import AnonymizationEngine


def test_append_accumulates(tmp_path):
    """append_entry only ever appends — entries accumulate, never overwrite."""
    log = tmp_path / "audit.jsonl"
    append_entry(log, mission="dossier-a", event="anonymize",
                 counts={"NOM": 2, "EMAIL": 1})
    append_entry(log, mission="dossier-a", event="dossier",
                 counts={"IBAN": 3}, file_count=4)
    append_entry(log, mission="dossier-b", event="forget", counts={})

    entries = read_audit(log)
    assert len(entries) == 3
    assert entries[0]["mission"] == "dossier-a"
    assert entries[0]["event"] == "anonymize"
    assert entries[0]["counts"] == {"NOM": 2, "EMAIL": 1}
    assert entries[1]["file_count"] == 4
    assert entries[2]["event"] == "forget"
    # every entry carries an ISO-UTC timestamp
    for e in entries:
        assert e["timestamp"].endswith("Z") or "+00:00" in e["timestamp"]


def test_file_is_jsonl_one_object_per_line(tmp_path):
    log = tmp_path / "audit.jsonl"
    append_entry(log, mission="m", event="anonymize", counts={"NOM": 1})
    append_entry(log, mission="m", event="anonymize", counts={"EMAIL": 1})
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line is independently valid JSON


def test_file_perms_0600(tmp_path):
    log = tmp_path / "audit.jsonl"
    append_entry(log, mission="m", event="anonymize", counts={"NOM": 1})
    mode = stat.S_IMODE(os.stat(log).st_mode)
    assert mode == 0o600


def test_read_audit_roundtrips(tmp_path):
    log = tmp_path / "audit.jsonl"
    payloads = [
        {"mission": "x", "event": "anonymize", "counts": {"NOM": 1}},
        {"mission": "y", "event": "purge", "counts": {"IBAN": 2, "EMAIL": 5}},
    ]
    for p in payloads:
        append_entry(log, mission=p["mission"], event=p["event"],
                     counts=p["counts"])
    entries = read_audit(log)
    assert [e["mission"] for e in entries] == ["x", "y"]
    assert [e["counts"] for e in entries] == [p["counts"] for p in payloads]


def test_read_audit_missing_file_returns_empty(tmp_path):
    assert read_audit(tmp_path / "nope.jsonl") == []


def test_log_result_records_counts_and_types_not_values(tmp_path):
    """The whole point: a real AnonymizationResult is logged as type→count, and
    the raw PII (names, emails, IBANs) NEVER appears in the audit file."""
    log = tmp_path / "audit.jsonl"
    text = ("Le client M. Jean Dupont, jean.dupont@example.com, "
            "IBAN FR76 3000 6000 0112 3456 7890 189.")
    result = AnonymizationEngine().anonymize(text)

    entry = log_result(log, result, mission="synthetic-dossier", event="anonymize")

    raw = log.read_text(encoding="utf-8")

    # 1. The entity TYPES and their counts ARE recorded.
    assert "NOM" in raw
    assert "EMAIL" in raw
    assert entry["counts"].get("NOM", 0) >= 1
    assert entry["counts"].get("EMAIL", 0) >= 1
    assert entry["total"] == result.entity_count
    assert entry["safe_to_send"] == result.safe_to_send

    # 2. The raw PII VALUES are NOT anywhere in the audit file.
    for secret in ("Jean Dupont", "jean.dupont@example.com",
                   "FR76", "3000 6000", "Dupont"):
        assert secret not in raw, f"PII value leaked into audit log: {secret!r}"

    # 3. It is persisted and reads back.
    entries = read_audit(log)
    assert len(entries) == 1
    assert entries[0]["mission"] == "synthetic-dossier"


def test_log_result_appends_to_existing(tmp_path):
    log = tmp_path / "audit.jsonl"
    append_entry(log, mission="prior", event="anonymize", counts={"NOM": 1})
    result = AnonymizationEngine().anonymize("M. Jean Dupont.")
    log_result(log, result, mission="later", event="anonymize")
    entries = read_audit(log)
    assert len(entries) == 2
    assert entries[0]["mission"] == "prior"
    assert entries[1]["mission"] == "later"
