"""
test_phase1_review_queue.py — Phase 1: review queue store + feeder.

All candidate values are SYNTHETIC (invented names, no real clients).
Tests use tmp_path to redirect both the queue store and the gazetteer so
the production ~/.bubble_shield is NEVER touched.

Synthetic names used: DUPONT, LEFEBVRE, MARCHETTI, FAUXNOM, ANCIEN.

Test plan (covers all spec bounding proofs):
  1. DEDUP (#2)          — same token from 3 docs → 1 pending item,
                           occurrence_count=3, 3 distinct doc_refs.
  2. GAZETTEER-SKIP (#1) — token already in gazetteer → add_candidate skips it,
                           nothing queued.
  3. CONFIRM end-to-end  — add_candidate → confirm → token in gazetteer +
                           removed from pending.  Prove a second add_candidate
                           for the same token is now SKIPPED (gazetteer-skip).
  4. DISMISS             — dismiss moves item to dismissed LOG; list_dismissed
                           shows it; list_pending no longer shows it; item NOT
                           deleted (auditable).
  5. EXPIRE (#3)         — pending item with old first_seen → expire_old
                           auto-dismisses it to dismissed_log with reason
                           "auto-expired"; pending shrinks.
  6. FEEDER              — sidecar with 2 candidates → feed_from_sidecar creates
                           2 pending items (dedup applied on repeat calls);
                           fail-open on a bad/missing sidecar.
  7. CORRUPT QUEUE JSON  — malformed JSON → empty, no crash.  Atomic write /
                           chmod 600 verified on the written file.
  8. MOST-RECURRING ORDER — list_pending returns highest occurrence_count first.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    """Sandbox BUBBLE_SHIELD_HOME so dismiss()→safe_words.add_safe() (added in
    #348 Task 4) writes to a temp dir, never the real ~/.bubble_shield/.
    The queue/gazetteer files are already sandboxed via path=; the safe-list
    store is BUBBLE_SHIELD_HOME-aware, so it needs this env override too."""
    home = tmp_path_factory.mktemp("bs_home")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_queue_path(tmp_path: Path) -> Path:
    return tmp_path / "review_queue.json"


def _make_gaz_path(tmp_path: Path) -> Path:
    return tmp_path / "known_pii.json"


def _populate_gazetteer(gaz_path: Path, entries: list[tuple[str, str]]) -> None:
    """Write synthetic entries to a gazetteer file (Gate B explicit adds)."""
    from bubble_shield.known_pii_store import add_confirmed_pii
    for value, entity_type in entries:
        add_confirmed_pii(value, entity_type, path=gaz_path)


# ── import under test ─────────────────────────────────────────────────────────

from bubble_shield.review_queue import (
    add_candidate,
    confirm,
    dismiss,
    expire_old,
    feed_from_sidecar,
    list_dismissed,
    list_pending,
)
from bubble_shield.known_pii_store import (
    add_confirmed_pii,
    is_known_pii,
    load_gazetteer,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. DEDUP (#2) — N occurrences across docs → 1 pending item
# ═══════════════════════════════════════════════════════════════════════════════

def test_dedup_three_docs(tmp_path: Path) -> None:
    """Same token from 3 different docs → 1 pending item with occurrence_count=3."""
    qpath = _make_queue_path(tmp_path)

    key1 = add_candidate("DUPONT", "NOM", "dossier_01.pdf", path=qpath)
    key2 = add_candidate("dupont", "NOM", "dossier_02.pdf", path=qpath)  # case variant
    key3 = add_candidate("Dupont", "NOM", "dossier_03.pdf", path=qpath)  # mixed case

    # All three normalize to the same key.
    assert key1 == "DUPONT"
    assert key2 == "DUPONT"
    assert key3 == "DUPONT"

    pending = list_pending(path=qpath)
    assert len(pending) == 1, "dedup: 3 occurrences → 1 pending item"
    item = pending[0]
    assert item["occurrence_count"] == 3
    assert sorted(item["doc_refs"]) == ["dossier_01.pdf", "dossier_02.pdf", "dossier_03.pdf"]
    assert item["status"] == "pending"


def test_dedup_same_doc_not_duplicated_in_refs(tmp_path: Path) -> None:
    """Adding the same doc twice: doc_ref appears once (no duplicates in list)."""
    qpath = _make_queue_path(tmp_path)

    add_candidate("LEFEBVRE", "NOM", "doc_A.pdf", path=qpath)
    add_candidate("LEFEBVRE", "NOM", "doc_A.pdf", path=qpath)  # same doc again

    pending = list_pending(path=qpath)
    assert len(pending) == 1
    assert pending[0]["occurrence_count"] == 2
    assert pending[0]["doc_refs"] == ["doc_A.pdf"]  # only once


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GAZETTEER-SKIP (#1) — already known PII → never queued
# ═══════════════════════════════════════════════════════════════════════════════

def test_gazetteer_skip(tmp_path: Path, monkeypatch) -> None:
    """Token already in gazetteer → add_candidate returns None, nothing queued."""
    qpath = _make_queue_path(tmp_path)
    gaz_path = _make_gaz_path(tmp_path)

    # Populate the gazetteer with MARCHETTI.
    add_confirmed_pii("MARCHETTI", "NOM", path=gaz_path)

    # Monkeypatch is_known_pii to point at the temp gazetteer.
    import bubble_shield.review_queue as rq
    original_is_known = rq.is_known_pii

    def patched_is_known_pii(value, *, path=None):
        return original_is_known(value, path=gaz_path)

    monkeypatch.setattr(rq, "is_known_pii", patched_is_known_pii)

    result = add_candidate("MARCHETTI", "NOM", "doc.pdf", path=qpath)

    assert result is None, "gazetteer-skip: known PII → add_candidate returns None"
    assert list_pending(path=qpath) == [], "gazetteer-skip: nothing queued"


def test_gazetteer_skip_variant_case(tmp_path: Path, monkeypatch) -> None:
    """Case variant of a gazetteered token → still skipped."""
    qpath = _make_queue_path(tmp_path)
    gaz_path = _make_gaz_path(tmp_path)

    add_confirmed_pii("Marchetti", "NOM", path=gaz_path)

    import bubble_shield.review_queue as rq
    original_is_known = rq.is_known_pii

    def patched_is_known_pii(value, *, path=None):
        return original_is_known(value, path=gaz_path)

    monkeypatch.setattr(rq, "is_known_pii", patched_is_known_pii)

    result = add_candidate("marchetti", "NOM", "doc.pdf", path=qpath)
    assert result is None
    assert list_pending(path=qpath) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CONFIRM end-to-end (#1 loop)
#    add_candidate → confirm → token in gazetteer + removed from pending.
#    Prove: a subsequent add_candidate is SKIPPED (gazetteer-skip = #1 drain).
# ═══════════════════════════════════════════════════════════════════════════════

def test_confirm_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """
    The crux of the self-draining design:
      1. DUPONT queued (not yet in gazetteer).
      2. confirm("DUPONT") → written to gazetteer + removed from pending.
      3. A second add_candidate("DUPONT") is NOW skipped (gazetteer-skip).
      4. list_pending is empty; list_dismissed shows the confirmed entry.
    """
    qpath = _make_queue_path(tmp_path)
    gaz_path = _make_gaz_path(tmp_path)

    # Patch is_known_pii and add_confirmed_pii to use the temp gazetteer.
    import bubble_shield.review_queue as rq
    from bubble_shield import known_pii_store as kps
    original_is_known = kps.is_known_pii
    original_add_confirmed = kps.add_confirmed_pii

    def patched_is_known_pii(value, *, path=None):
        return original_is_known(value, path=gaz_path)

    def patched_add_confirmed(value, entity_type, *, path=None):
        return original_add_confirmed(value, entity_type, path=gaz_path)

    monkeypatch.setattr(rq, "is_known_pii", patched_is_known_pii)
    # Also patch the add_confirmed_pii called inside confirm():
    monkeypatch.setattr(
        "bubble_shield.review_queue.add_confirmed_pii",
        patched_add_confirmed,
    )

    # Step 1: queue DUPONT.
    key = add_candidate("DUPONT", "NOM", "dossier.pdf", path=qpath)
    assert key == "DUPONT"
    assert len(list_pending(path=qpath)) == 1

    # Step 2: confirm.
    returned_value = confirm("DUPONT", path=qpath)
    assert returned_value == "DUPONT", "confirm() returns the real value"

    # Step 3: item removed from pending.
    assert list_pending(path=qpath) == [], "confirmed item removed from pending"

    # Step 4: item in dismissed_log with status=confirmed.
    dismissed = list_dismissed(path=qpath)
    assert len(dismissed) == 1
    assert dismissed[0]["status"] == "confirmed"
    assert dismissed[0]["value"] == "DUPONT"

    # Step 5: DUPONT is now in the gazetteer.
    assert patched_is_known_pii("DUPONT"), "confirmed value in gazetteer"

    # Step 6: a SECOND add_candidate("DUPONT") is skipped — the #1 self-drain.
    key2 = add_candidate("DUPONT", "NOM", "dossier2.pdf", path=qpath)
    assert key2 is None, "second add_candidate skipped by gazetteer-skip (#1 drain)"
    assert list_pending(path=qpath) == [], "queue still empty after second add attempt"


def test_confirm_not_found_returns_none(tmp_path: Path) -> None:
    """confirm() on a non-existent key returns None gracefully."""
    qpath = _make_queue_path(tmp_path)
    result = confirm("NONEXISTENT", path=qpath)
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DISMISS — moves to dismissed LOG, not deleted; auditable
# ═══════════════════════════════════════════════════════════════════════════════

def test_dismiss_moves_to_log(tmp_path: Path) -> None:
    """dismiss() moves item from pending to dismissed_log; not deleted."""
    qpath = _make_queue_path(tmp_path)

    add_candidate("FAUXNOM", "NOM", "contrat.pdf", path=qpath)
    assert len(list_pending(path=qpath)) == 1

    dismissed_before = list_dismissed(path=qpath)
    assert dismissed_before == []

    result = dismiss("FAUXNOM", path=qpath)
    assert result is True

    # Not in pending anymore.
    assert list_pending(path=qpath) == []

    # In dismissed_log.
    log = list_dismissed(path=qpath)
    assert len(log) == 1
    entry = log[0]
    assert entry["normalized"] == "FAUXNOM"
    assert entry["status"] == "dismissed"
    assert entry["dismiss_reason"] == "user-dismissed"
    assert "resolved_at" in entry


def test_dismiss_not_found_returns_false(tmp_path: Path) -> None:
    """dismiss() on a non-existent key returns False gracefully."""
    qpath = _make_queue_path(tmp_path)
    result = dismiss("DOESNOTEXIST", path=qpath)
    assert result is False


def test_dismiss_log_not_deleted_on_subsequent_add(tmp_path: Path) -> None:
    """The dismissed log entry persists even if a different item is added later."""
    qpath = _make_queue_path(tmp_path)

    add_candidate("FAUXNOM", "NOM", "doc1.pdf", path=qpath)
    dismiss("FAUXNOM", path=qpath)

    # Add a different item.
    add_candidate("LEFEBVRE", "NOM", "doc2.pdf", path=qpath)

    # FAUXNOM still in dismissed_log.
    log = list_dismissed(path=qpath)
    assert any(e["normalized"] == "FAUXNOM" for e in log)
    # LEFEBVRE in pending.
    pending = list_pending(path=qpath)
    assert any(e["normalized"] == "LEFEBVRE" for e in pending)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. EXPIRE (#3) — backstop auto-dismiss
# ═══════════════════════════════════════════════════════════════════════════════

def test_expire_old_moves_to_log(tmp_path: Path) -> None:
    """An old pending item is auto-dismissed to the log with reason 'auto-expired'."""
    qpath = _make_queue_path(tmp_path)

    # Add a fresh item and an old item.
    add_candidate("ANCIEN", "NOM", "vieux.pdf", path=qpath)
    add_candidate("RECENT", "NOM", "neuf.pdf", path=qpath)

    # Back-date ANCIEN's first_seen to 40 days ago.
    raw_path = qpath
    raw = json.loads(raw_path.read_text())
    for item in raw["items"]:
        if item["normalized"] == "ANCIEN":
            old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
            item["first_seen"] = old_ts
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))

    expired = expire_old(max_age_days=30, path=qpath)
    assert expired == 1, "expire_old should return count of expired items"

    pending = list_pending(path=qpath)
    assert len(pending) == 1
    assert pending[0]["normalized"] == "RECENT"

    log = list_dismissed(path=qpath)
    assert len(log) == 1
    assert log[0]["normalized"] == "ANCIEN"
    assert log[0]["dismiss_reason"] == "auto-expired"
    assert log[0]["status"] == "dismissed"
    assert "resolved_at" in log[0]


def test_expire_nothing_when_all_recent(tmp_path: Path) -> None:
    """No items expire when all are recent."""
    qpath = _make_queue_path(tmp_path)
    add_candidate("RECENT", "NOM", "doc.pdf", path=qpath)
    expired = expire_old(max_age_days=30, path=qpath)
    assert expired == 0
    assert len(list_pending(path=qpath)) == 1


def test_expire_empty_queue(tmp_path: Path) -> None:
    """expire_old on empty queue returns 0 without error."""
    qpath = _make_queue_path(tmp_path)
    expired = expire_old(max_age_days=30, path=qpath)
    assert expired == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. FEEDER — feed_from_sidecar
# ═══════════════════════════════════════════════════════════════════════════════

def _write_sidecar(sidecar_path: Path, entries: list[dict]) -> None:
    """Write a synthetic sidecar file (Phase-0 format)."""
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    sidecar_path.chmod(0o600)


def test_feeder_two_candidates(tmp_path: Path, monkeypatch) -> None:
    """Sidecar with 2 candidates → 2 pending items created."""
    qpath = _make_queue_path(tmp_path)

    # Redirect BUBBLE_SHIELD_HOME so the feeder finds our synthetic sidecar.
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    # Force module to re-read BUBBLE_SHIELD_HOME for _candidates_dir().
    import importlib
    import bubble_shield.review_queue as rq
    importlib.reload(rq)

    sidecar_dir = tmp_path / "candidates"
    sidecar_path = sidecar_dir / "test-mission.candidates.json"
    _write_sidecar(sidecar_path, [
        {
            "value": "DUPONT",
            "normalized": "DUPONT",
            "entity_type": "NOM",
            "source_doc": "doc1.pdf",
            "score": 0.45,
            "threshold": 0.6,
            "char_start": 0,
            "char_end": 6,
            "mission": "test-mission",
            "is_residual": False,
            "ts": "2026-06-27T10:00:00+00:00",
        },
        {
            "value": "LEFEBVRE",
            "normalized": "LEFEBVRE",
            "entity_type": "NOM",
            "source_doc": "doc2.pdf",
            "score": 0.50,
            "threshold": 0.6,
            "char_start": 0,
            "char_end": 8,
            "mission": "test-mission",
            "is_residual": False,
            "ts": "2026-06-27T10:01:00+00:00",
        },
    ])

    count = rq.feed_from_sidecar("test-mission", path=qpath)
    assert count == 2, "feeder: 2 distinct candidates → 2 pending items"

    pending = rq.list_pending(path=qpath)
    assert len(pending) == 2
    normalized_keys = {p["normalized"] for p in pending}
    assert normalized_keys == {"DUPONT", "LEFEBVRE"}


def test_feeder_dedup_on_repeat_call(tmp_path: Path, monkeypatch) -> None:
    """Calling feed_from_sidecar twice with same sidecar → dedup, not duplicates."""
    qpath = _make_queue_path(tmp_path)

    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    import importlib
    import bubble_shield.review_queue as rq
    importlib.reload(rq)

    sidecar_dir = tmp_path / "candidates"
    sidecar_path = sidecar_dir / "repeat.candidates.json"
    _write_sidecar(sidecar_path, [
        {
            "value": "MARCHETTI",
            "normalized": "MARCHETTI",
            "entity_type": "NOM",
            "source_doc": "doc.pdf",
            "score": 0.45,
            "threshold": 0.6,
            "char_start": 0,
            "char_end": 9,
            "mission": "repeat",
            "is_residual": False,
            "ts": "2026-06-27T10:00:00+00:00",
        },
    ])

    rq.feed_from_sidecar("repeat", path=qpath)
    rq.feed_from_sidecar("repeat", path=qpath)  # second drain of same sidecar

    pending = rq.list_pending(path=qpath)
    assert len(pending) == 1, "dedup: second drain does not create a duplicate item"
    assert pending[0]["occurrence_count"] == 2


def test_feeder_fail_open_missing_sidecar(tmp_path: Path, monkeypatch) -> None:
    """Missing sidecar → feed_from_sidecar returns 0 without error (fail-open)."""
    qpath = _make_queue_path(tmp_path)

    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    import importlib
    import bubble_shield.review_queue as rq
    importlib.reload(rq)

    count = rq.feed_from_sidecar("nonexistent-mission", path=qpath)
    assert count == 0


def test_feeder_fail_open_corrupt_sidecar(tmp_path: Path, monkeypatch) -> None:
    """Corrupt sidecar JSON → feed_from_sidecar returns 0 without error."""
    qpath = _make_queue_path(tmp_path)

    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    import importlib
    import bubble_shield.review_queue as rq
    importlib.reload(rq)

    sidecar_dir = tmp_path / "candidates"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    bad_sidecar = sidecar_dir / "corrupt.candidates.json"
    bad_sidecar.write_text("{this is not valid json[[[", encoding="utf-8")

    count = rq.feed_from_sidecar("corrupt", path=qpath)
    assert count == 0, "fail-open: corrupt sidecar → 0, no exception"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CORRUPT QUEUE JSON + ATOMIC WRITE / CHMOD 600
# ═══════════════════════════════════════════════════════════════════════════════

def test_corrupt_queue_json_graceful(tmp_path: Path) -> None:
    """Corrupt review_queue.json → treated as empty, no crash."""
    qpath = _make_queue_path(tmp_path)
    qpath.write_text("NOT VALID JSON{{{", encoding="utf-8")

    # All ops should gracefully return empty results.
    assert list_pending(path=qpath) == []
    assert list_dismissed(path=qpath) == []
    expired = expire_old(max_age_days=30, path=qpath)
    assert expired == 0

    # Writing a new candidate still works (overwrites corruption).
    key = add_candidate("DUPONT", "NOM", "doc.pdf", path=qpath)
    assert key == "DUPONT"
    assert len(list_pending(path=qpath)) == 1


def test_atomic_write_and_chmod_600(tmp_path: Path) -> None:
    """After add_candidate, the store file should be chmod 600."""
    qpath = _make_queue_path(tmp_path)

    add_candidate("DUPONT", "NOM", "doc.pdf", path=qpath)

    assert qpath.exists()
    mode = stat.S_IMODE(qpath.stat().st_mode)
    assert mode == 0o600, f"Expected chmod 600, got {oct(mode)}"


def test_no_tmp_file_left_after_write(tmp_path: Path) -> None:
    """Atomic write: no leftover .tmp file after successful write."""
    qpath = _make_queue_path(tmp_path)
    add_candidate("DUPONT", "NOM", "doc.pdf", path=qpath)
    tmp_file = qpath.with_suffix(".tmp")
    assert not tmp_file.exists(), "No .tmp file should remain after atomic write"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. MOST-RECURRING ORDER
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_pending_most_recurring_first(tmp_path: Path) -> None:
    """list_pending returns items sorted by occurrence_count descending."""
    qpath = _make_queue_path(tmp_path)

    # DUPONT: 1 occurrence.
    add_candidate("DUPONT", "NOM", "d1.pdf", path=qpath)

    # LEFEBVRE: 3 occurrences.
    add_candidate("LEFEBVRE", "NOM", "d1.pdf", path=qpath)
    add_candidate("LEFEBVRE", "NOM", "d2.pdf", path=qpath)
    add_candidate("LEFEBVRE", "NOM", "d3.pdf", path=qpath)

    # MARCHETTI: 2 occurrences.
    add_candidate("MARCHETTI", "NOM", "d1.pdf", path=qpath)
    add_candidate("MARCHETTI", "NOM", "d2.pdf", path=qpath)

    pending = list_pending(path=qpath)
    counts = [p["occurrence_count"] for p in pending]
    assert counts == sorted(counts, reverse=True), (
        "list_pending should be ordered most-recurring first"
    )
    assert pending[0]["normalized"] == "LEFEBVRE"


# ═══════════════════════════════════════════════════════════════════════════════
# BONUS: dismissed item never re-queued via add_candidate
# ═══════════════════════════════════════════════════════════════════════════════

def test_dismissed_item_not_requeued(tmp_path: Path) -> None:
    """A dismissed item: a new add_candidate for the same token is ignored."""
    qpath = _make_queue_path(tmp_path)

    add_candidate("FAUXNOM", "NOM", "doc1.pdf", path=qpath)
    dismiss("FAUXNOM", path=qpath)

    # Try to add again (e.g. from a new doc).
    result = add_candidate("FAUXNOM", "NOM", "doc2.pdf", path=qpath)
    assert result is None, "dismissed item should not be re-queued"
    assert list_pending(path=qpath) == []
