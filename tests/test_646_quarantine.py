"""
tests/test_646_quarantine.py — un-certifiable docs quarantine instead of looping (#646).

A doc that fails to certify EVERY sweep (un-extractable INPI, a giant form, a
persistently-down verify) was re-tried forever, burning the single serial Gemma worker
on a doc that can never complete. Fix: mark_pending(failed=True) increments a fail_count;
after QUARANTINE_AFTER_FAILS the doc leaves pending → quarantined (surfaced, not re-swept).
The sweep skips quarantined content_hashes.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugin" / "bubble-shield" / "vendor"))


@pytest.fixture()
def store(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", tmp)
    monkeypatch.setenv("BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE", "1")
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    monkeypatch.setenv("BUBBLE_SHIELD_QUARANTINE_AFTER_FAILS", "3")
    from bubble_shield import shadow_store as ss
    importlib.reload(ss)
    return ss


def test_failed_increments_quarantines_at_threshold(store):
    p = "/clients/x/uncertifiable.pdf"
    for _ in range(2):
        store.mark_pending(p, failed=True)
    assert p in store.pending_files() and p not in store.quarantined_files()
    store.mark_pending(p, failed=True)  # 3rd fail = threshold
    assert p not in store.pending_files(), "at threshold, leaves the pending (retry) queue"
    assert p in store.quarantined_files(), "at threshold, becomes quarantined (surfaced)"


def test_plain_miss_does_not_increment(store):
    """A read-miss queue (failed=False) is NOT a failure — it must never accrue toward
    quarantine (a not-yet-indexed file is not un-certifiable)."""
    p = "/clients/x/new.pdf"
    for _ in range(10):
        store.mark_pending(p)  # failed defaults to False
    assert p in store.pending_files()
    assert p not in store.quarantined_files(), "plain misses must never quarantine"


def test_backward_compat_old_pending_table(store, monkeypatch):
    """An existing pending table WITHOUT fail_count (pre-#646) must upgrade in place
    (ALTER TABLE ADD COLUMN) — no crash, quarantine simply starts fresh."""
    # simulate an old-schema row by dropping the column via a raw connect
    conn = store.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS pending")
        conn.execute("CREATE TABLE pending (src_path TEXT PRIMARY KEY, marked_at REAL)")
        conn.execute("INSERT INTO pending VALUES ('/old/doc.pdf', 1.0)")
        conn.commit()
    finally:
        conn.close()
    # reads must not crash; the old row is pending (fail_count defaults 0)
    assert "/old/doc.pdf" in store.pending_files()
    assert store.quarantined_files() == []


def test_sweep_skips_quarantined(store, monkeypatch, tmp_path):
    """run_sweep must SKIP a quarantined doc (not re-fail it). Drive run_sweep with a
    quarantined path present and an anonymize_fn that would fail — assert it's counted
    as quarantined, not failed."""
    from bubble_shield import shadow_index as si
    importlib.reload(si)
    # a real file on disk
    doc = tmp_path / "q.txt"
    doc.write_text("some client text")
    resolved = str(Path(os.path.expanduser(str(doc))).resolve())
    # quarantine it: 3 failures
    for _ in range(3):
        store.mark_pending(resolved, failed=True)
    assert resolved in store.quarantined_files()

    def always_fail(_path):
        raise RuntimeError("cannot certify")

    res = si.run_sweep(str(tmp_path), anonymize_fn=always_fail)
    assert res.get("quarantined", 0) >= 1, "the quarantined doc must be counted as quarantined"
    assert res.get("failed", 0) == 0, "a quarantined doc must NOT be re-failed (no worker burn)"
