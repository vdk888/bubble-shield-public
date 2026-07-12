"""Tests for webapp/dashboard.py — risk-control stats over the audit log."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webapp.dashboard import summarize  # noqa: E402


def _run(safe=True, counts=None, event="anonymize"):
    c = counts if counts is not None else {"NOM": 2, "MONTANT": 1}
    numeric_total = sum(v for v in c.values() if isinstance(v, int))
    return {
        "timestamp": "2026-06-02T10:00:00Z",
        "mission": "demo",
        "event": event,
        "counts": c,
        "total": numeric_total,
        "safe_to_send": safe,
    }


def test_empty():
    s = summarize([])
    assert s["total_runs"] == 0
    assert s["has_data"] is False
    assert s["safe_rate"] == 0.0


def test_counts_runs_and_safety():
    s = summarize([_run(safe=True), _run(safe=False), _run(safe=True)])
    assert s["total_runs"] == 3
    assert s["safe_runs"] == 2
    assert s["unsafe_runs"] == 1
    assert s["safe_rate"] == 0.667  # rounded to 3 dp for display


def test_missing_safe_key_is_unsafe_failclosed():
    entry = _run()
    del entry["safe_to_send"]
    s = summarize([entry])
    assert s["unsafe_runs"] == 1  # unknown verdict → treated as a risk


def test_errors_counted_separately_not_as_unsafe():
    s = summarize([_run(safe=True), _run(event="error", counts={})])
    assert s["error_runs"] == 1
    assert s["total_runs"] == 1          # error not counted as a run
    assert s["unsafe_runs"] == 0         # error is not an "unsafe send"


def test_entity_totals_aggregate_and_sorted():
    s = summarize([
        _run(counts={"NOM": 3, "MONTANT": 1}),
        _run(counts={"NOM": 2, "IBAN": 1}),
    ])
    assert s["entity_totals"]["NOM"] == 5
    assert s["entity_totals"]["MONTANT"] == 1
    assert s["entity_totals"]["IBAN"] == 1
    assert s["total_entities"] == 7
    # most_common → NOM first
    assert list(s["entity_totals"].keys())[0] == "NOM"


def test_malformed_count_value_skipped():
    s = summarize([_run(counts={"NOM": "oops", "MONTANT": 2})])
    assert s["entity_totals"].get("NOM") is None
    assert s["entity_totals"]["MONTANT"] == 2


def test_recent_is_newest_first_and_capped():
    entries = [_run(counts={"NOM": i}) for i in range(60)]
    s = summarize(entries, recent=10)
    assert len(s["recent"]) == 10
    # newest first → the last appended (NOM:59) leads
    assert s["recent"][0]["counts"]["NOM"] == 59


def test_vault_reveal_never_counted_as_run_or_unsafe():
    """Regression for #587: a vault_reveal (document RESTORE) has no
    `safe_to_send` key, so the fail-closed `_is_unsafe` logic used to flag it
    as "à relire" once it was miscounted into `runs`. A restore is the
    OPPOSITE of a risk — it must never inflate the anonymise/risk stats."""
    entries = [
        _run(safe=True),                                     # 1 real anonymise, safe
        _run(safe=False),                                    # 1 real anonymise, unsafe
        {"timestamp": "2026-06-02T10:05:00Z", "mission": "demo",
         "event": "vault_reveal", "counts": {}, "total": 0},  # restore, no safe_to_send key
        {"timestamp": "2026-06-02T10:06:00Z", "mission": "demo",
         "event": "vault_reveal", "counts": {}, "total": 0},  # another restore
        _run(event="error", counts={}),                       # 1 error
    ]
    s = summarize(entries)
    assert s["total_runs"] == 2          # only the two anonymize entries
    assert s["unsafe_runs"] == 1         # only the genuinely-unsafe anonymize run
    assert s["safe_runs"] == 1
    assert s["error_runs"] == 1
    assert s["reveal_runs"] == 2         # reveals surfaced separately, honestly labeled


def test_real_audit_log_shape_38_events_3_anonymize():
    """Mirrors the shape of the real ~/.bubble_shield/audit.jsonl that triggered
    #587: 3 anonymize runs (all safe) + 35 vault_reveal restores + 0 errors.
    Before the fix this produced "35 à relire of 38". After the fix: 0 of 3."""
    entries = [_run(safe=True) for _ in range(3)]
    entries += [
        {"timestamp": "2026-06-02T10:00:00Z", "mission": "demo",
         "event": "vault_reveal", "counts": {}, "total": 0}
        for _ in range(35)
    ]
    s = summarize(entries)
    assert s["total_runs"] == 3
    assert s["unsafe_runs"] == 0
    assert s["reveal_runs"] == 35
