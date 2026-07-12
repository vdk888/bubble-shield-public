"""
tests/test_review_ui.py — Phase 3: Review queue UI routes.

All candidate values are synthetic (fictional names per pii-guard rules).
Real values only ever live in the local store at runtime; never in code/tests.

Tests:
1. GET /review renders pending items + Confirmer/Ignorer buttons (seeded with
   synthetic LEFEBVRE / BERTRAND via review_queue.add_candidate).
2. POST /review/confirm → item gone from pending, confirm called.
3. POST /review/dismiss → item in dismissed log.
4. GET /review/dismissed renders the dismissed audit log.
5. GET /review drains sidecar (feed_from_sidecar called) + expires old items
   (expire_old called) on load — verified via mock.
6. review_queue.py is the Phase-1 file (not rebuilt).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from webapp.app import app
import bubble_shield.review_queue as rq

client = TestClient(app, follow_redirects=True)


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    """Sandbox BUBBLE_SHIELD_HOME so dismiss()→safe_words.add_safe() (added in
    #348 Task 4) writes to a temp dir, never the real ~/.bubble_shield/.
    The queue file is already sandboxed via path=; the safe-list store is
    BUBBLE_SHIELD_HOME-aware, so it needs this env override too."""
    home = tmp_path_factory.mktemp("bs_home")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Synthetic names that we guarantee are NOT in the real gazetteer.
# Use uncommon fictitious surnames to avoid gazetteer-skip (#1 rule).
_NAME_A = "LEFEBVRE"
_NAME_B = "BERTRAND"


def _seeded_queue(tmp_path: Path) -> Path:
    """Return a tmp queue file pre-seeded with two synthetic pending items.

    We patch is_known_pii to always return False so add_candidate never
    gazetteer-skips, regardless of the real on-disk gazetteer state.
    """
    queue_file = tmp_path / "review_queue.json"
    with mock.patch("bubble_shield.review_queue.is_known_pii", return_value=False):
        rq.add_candidate(_NAME_A, "NOM", "dossier-fictif.pdf", path=queue_file)
        rq.add_candidate(_NAME_B, "NOM", "contrat-fictif.pdf", path=queue_file)
        # second occurrence of _NAME_A so occurrence_count = 2
        rq.add_candidate(_NAME_A, "NOM", "annexe-fictive.pdf", path=queue_file)
    return queue_file


# ---------------------------------------------------------------------------
# 1 — GET /review renders pending items + action buttons
# ---------------------------------------------------------------------------

def test_review_inbox_renders_pending_items(tmp_path):
    """GET /review must render the pending items and Confirmer/Ignorer buttons."""
    queue_file = _seeded_queue(tmp_path)
    pending = rq.list_pending(path=queue_file)
    assert len(pending) == 2, f"Expected 2 pending items, got {len(pending)}"

    with (
        mock.patch("bubble_shield.review_queue.feed_from_sidecar", return_value=0),
        mock.patch("bubble_shield.review_queue.expire_old", return_value=0),
        mock.patch("bubble_shield.review_queue.list_pending", return_value=pending),
    ):
        r = client.get("/review")

    assert r.status_code == 200
    # Synthetic names must appear
    assert _NAME_A in r.text, f"{_NAME_A} not found in review page"
    assert _NAME_B in r.text, f"{_NAME_B} not found in review page"
    # Confirmer and Ignorer buttons
    assert "Confirmer" in r.text
    assert "Ignorer" in r.text
    # FR section heading
    assert "File de r" in r.text  # "File de révision" heading fragment
    # Occurrence count for _NAME_A (2 occurrences)
    assert "2 occurrence" in r.text


# ---------------------------------------------------------------------------
# 2 — POST /review/confirm calls confirm; item drained from store
# ---------------------------------------------------------------------------

def test_review_confirm_drains_item_and_writes_gazetteer(tmp_path):
    """POST /review/confirm → confirm called with correct normalized key;
    item removed from pending in the store; moved to dismissed_log as confirmed."""
    queue_file = _seeded_queue(tmp_path)

    real_confirm = rq.confirm
    captured: list[str] = []

    def spy_confirm(normalized, *, path=None):
        captured.append(normalized)
        # run on our tmp store; suppress real gazetteer write
        with mock.patch("bubble_shield.review_queue.add_confirmed_pii"):
            return real_confirm(normalized, path=queue_file)

    with (
        mock.patch("bubble_shield.review_queue.feed_from_sidecar", return_value=0),
        mock.patch("bubble_shield.review_queue.expire_old", return_value=0),
        mock.patch("bubble_shield.review_queue.confirm", side_effect=spy_confirm),
        # list_pending after redirect reads the real (now mutated) tmp store
        mock.patch(
            "bubble_shield.review_queue.list_pending",
            side_effect=lambda *a, **kw: rq.list_pending(path=queue_file),
        ),
    ):
        r = client.post("/review/confirm", data={"normalized": _NAME_A})

    assert r.status_code == 200  # TestClient follows redirect → /review
    # confirm was called with the correct key
    assert _NAME_A in captured, f"Expected confirm called with {_NAME_A}; got {captured}"

    # _NAME_A must be gone from active pending in our tmp store
    remaining = [it["normalized"] for it in rq.list_pending(path=queue_file)]
    assert _NAME_A not in remaining
    # _NAME_B still pending
    assert _NAME_B in remaining

    # _NAME_A is now in the dismissed_log as confirmed
    log = rq.list_dismissed(path=queue_file)
    confirmed_normals = [it["normalized"] for it in log if it["status"] == "confirmed"]
    assert _NAME_A in confirmed_normals


# ---------------------------------------------------------------------------
# 3 — POST /review/dismiss moves item to dismissed log
# ---------------------------------------------------------------------------

def test_review_dismiss_moves_item_to_log(tmp_path):
    """POST /review/dismiss → item in dismissed log with status='dismissed'."""
    queue_file = _seeded_queue(tmp_path)

    real_dismiss = rq.dismiss
    captured: list[str] = []

    def spy_dismiss(normalized, *, path=None, reason="user-dismissed"):
        captured.append(normalized)
        return real_dismiss(normalized, path=queue_file, reason=reason)

    with (
        mock.patch("bubble_shield.review_queue.feed_from_sidecar", return_value=0),
        mock.patch("bubble_shield.review_queue.expire_old", return_value=0),
        mock.patch("bubble_shield.review_queue.dismiss", side_effect=spy_dismiss),
        mock.patch(
            "bubble_shield.review_queue.list_pending",
            side_effect=lambda *a, **kw: rq.list_pending(path=queue_file),
        ),
    ):
        r = client.post("/review/dismiss", data={"normalized": _NAME_B})

    assert r.status_code == 200
    assert _NAME_B in captured

    # _NAME_B gone from pending
    remaining = [it["normalized"] for it in rq.list_pending(path=queue_file)]
    assert _NAME_B not in remaining

    # _NAME_B in the dismissed log
    log = rq.list_dismissed(path=queue_file)
    dismissed_normals = [it["normalized"] for it in log if it["status"] == "dismissed"]
    assert _NAME_B in dismissed_normals


# ---------------------------------------------------------------------------
# 4 — GET /review/dismissed renders the audit log
# ---------------------------------------------------------------------------

def test_review_dismissed_renders_log(tmp_path):
    """GET /review/dismissed renders confirmed + dismissed entries."""
    queue_file = _seeded_queue(tmp_path)

    # Dismiss _NAME_B, confirm _NAME_A in the real tmp store.
    rq.dismiss(_NAME_B, path=queue_file)
    with mock.patch("bubble_shield.review_queue.add_confirmed_pii"):
        rq.confirm(_NAME_A, path=queue_file)

    dismissed = rq.list_dismissed(path=queue_file)
    assert len(dismissed) == 2, f"Expected 2 dismissed entries, got {dismissed}"

    with mock.patch(
        "bubble_shield.review_queue.list_dismissed",
        return_value=dismissed,
    ):
        r = client.get("/review/dismissed")

    assert r.status_code == 200
    # Both synthetic values must appear in the rendered log
    for item in dismissed:
        assert item["value"] in r.text, (
            f"Expected {item['value']!r} in /review/dismissed response"
        )
    # Status labels (FR template)
    assert "confirm" in r.text.lower()
    assert "ignor" in r.text.lower()
    # Audit log section heading
    assert "archiv" in r.text.lower()


# ---------------------------------------------------------------------------
# 5 — GET /review drains ALL sidecars + expires old items on load
# ---------------------------------------------------------------------------

def test_review_load_calls_feed_and_expire():
    """GET /review must drain ALL sidecars (feed_from_sidecar_all) AND expire_old
    on every load (#394 — was a single hardcoded 'demo' mission)."""
    feed_called: list[bool] = []
    expire_called: list[int] = []

    def _fake_feed_all(*, path=None):
        feed_called.append(True)
        return 0

    def _fake_expire(max_age_days=30, *, path=None):
        expire_called.append(max_age_days)
        return 0

    with (
        mock.patch("bubble_shield.review_queue.feed_from_sidecar_all", side_effect=_fake_feed_all),
        mock.patch("bubble_shield.review_queue.expire_old", side_effect=_fake_expire),
        mock.patch("bubble_shield.review_queue.list_pending", return_value=[]),
    ):
        r = client.get("/review")

    assert r.status_code == 200
    assert len(feed_called) >= 1, "feed_from_sidecar_all must be called on /review load"
    assert len(expire_called) >= 1, "expire_old must be called on /review load"


# ---------------------------------------------------------------------------
# 6 — review_queue.py was NOT rebuilt: module docstring + key API present
# ---------------------------------------------------------------------------

def test_review_queue_phase1_present_not_rebuilt():
    """Confirm review_queue.py is the Phase-1 file (not a Phase-3 reimplementation).

    Checks the module docstring is intact and all expected public functions exist.
    """
    import bubble_shield.review_queue as _rq
    import inspect

    src = inspect.getsource(_rq)
    # Phase-1 module docstring markers
    assert "Phase 1: HITL review queue store" in src
    assert "ADVISORY ONLY" in src
    assert "GAZETTEER-SKIP" in src

    # All Phase-1 public API must be present
    for fn_name in ("add_candidate", "confirm", "dismiss",
                    "list_pending", "list_dismissed",
                    "expire_old", "feed_from_sidecar", "feed_from_sidecar_all"):
        assert hasattr(_rq, fn_name), f"Missing Phase-1 function: {fn_name}"


# ---------------------------------------------------------------------------
# 7 — #394 PROOF: /review drains ALL candidate sidecars, not a hardcoded
#     'demo' mission. The HITL loop works end-to-end across MANY dossiers.
# ---------------------------------------------------------------------------

import json


def _write_sidecar(home: Path, mission: str, value: str, entity_type: str, score: float):
    """Write a Phase-0 candidate sidecar for `mission` with one sub-threshold
    candidate (mirrors candidate_sidecar's on-disk list-of-dicts format)."""
    cand_dir = home / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in mission if c.isalnum() or c in "-_.") or "default"
    sidecar = cand_dir / f"{safe}.candidates.json"
    sidecar.write_text(
        json.dumps([
            {
                "value": value,
                "normalized": rq._normalize(value),
                "entity_type": entity_type,
                "score": score,
                "threshold": 0.6,
                "source_doc": f"dossier-{safe}.pdf",
                "mission": mission,
                "is_residual": True,
                "ts": "2026-06-29T00:00:00+00:00",
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    return sidecar


def test_394_review_drains_all_missions_end_to_end(tmp_path_factory, monkeypatch):
    """THE #394 PROOF — the HITL review loop works end to end across dossiers.

    The plugin writes sub-threshold candidates under mission 'mcp-session' (the
    BUBBLE_SHIELD_SESSION default), NOT 'demo'. Before the fix, /review drained
    only the hardcoded 'demo' mission → orphaned candidates, empty queue, inert
    loop. This proves /review now drains EVERY sidecar.

    Synthetic PII only (fictional address/postcode), pii-guard compliant.
    """
    from bubble_shield.known_pii_store import is_known_pii

    home = tmp_path_factory.mktemp("bs394_home")
    # Sandbox EVERYTHING under a temp store: candidates dir, review_queue.json,
    # AND the gazetteer all resolve from BUBBLE_SHIELD_HOME — never the real store.
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))

    # Synthetic sub-threshold candidates (score 0.54 < 0.6 threshold):
    #  - mission 'mcp-session' (the real default the plugin writes under)
    #  - a SECOND, differently-named mission (proves it's not one hardcoded name)
    addr_mcp = "130 RUE TESTRUE 99000"
    addr_other = "42 IMPASSE FICTIVE 88000"
    _write_sidecar(home, "mcp-session", addr_mcp, "ADRESSE", 0.54)
    _write_sidecar(home, "dossier-martin-2026", addr_other, "ADRESSE", 0.54)

    # Sanity: neither is in the (empty, sandboxed) gazetteer yet.
    assert not is_known_pii(addr_mcp)
    assert not is_known_pii(addr_other)

    # --- /review: drains ALL sidecars (no feeder mocking — real path) --------
    r = client.get("/review")
    assert r.status_code == 200
    # BOTH candidates surface — drained from mcp-session AND the other mission,
    # NOT just 'demo' (which has no sidecar at all here).
    assert addr_mcp in r.text, (
        f"mcp-session candidate {addr_mcp!r} missing from /review — "
        "it was NOT drained (the #394 bug)."
    )
    assert addr_other in r.text, (
        f"second-mission candidate {addr_other!r} missing from /review — "
        "/review is still draining only one hardcoded mission."
    )

    # --- Confirm the mcp-session candidate → it enters the gazetteer ---------
    normalized = rq._normalize(addr_mcp)
    rc = client.post("/review/confirm", data={"normalized": normalized})
    assert rc.status_code == 200  # follow_redirects=True → final 200

    # THE loop closed: a sub-threshold leak the human confirmed is now known PII.
    assert is_known_pii(addr_mcp), (
        "confirming the candidate did NOT gazetteer it — HITL loop still broken."
    )
    # The other dossier's candidate is untouched (still pending, not confirmed).
    assert not is_known_pii(addr_other)
