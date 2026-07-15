# tests/test_348_safe_feed.py
"""Task 4 (#348): dismissing a review candidate feeds the self-improving safe-list.

API adaptation note (verified against bubble_shield/review_queue.py):
  - The plan assumed `add_candidate(value, entity_type=...)` + `dismiss(value)`.
  - The REAL API is:
        add_candidate(value, entity_type, doc, *, path=None) -> normalized | None
        dismiss(normalized, *, path=None, reason="user-dismissed") -> bool
    i.e. dismiss takes the NORMALIZED key (not the raw value), and add_candidate
    returns that key.  We capture it and feed it to dismiss.
  - review_queue's default store path is HARDCODED to ~/.bubble_shield (the module
    constant is NOT BUBBLE_SHIELD_HOME-aware), so we pass an explicit `path=` to keep
    the real home untouched.  safe_words IS BUBBLE_SHIELD_HOME-aware, so setenv covers it.
"""
import importlib


def test_dismiss_feeds_safe_list(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    import bubble_shield.review_queue as rq
    importlib.reload(rq)
    import bubble_shield.safe_words as sw
    importlib.reload(sw)

    queue_path = tmp_path / "review_queue.json"

    # A wrongly-flagged ordinary word the reviewer dismisses as "not PII".
    normalized = rq.add_candidate("Wrongword", "NOM", "doc1.pdf", path=queue_path)
    assert normalized is not None

    assert sw.is_safe("Wrongword") is False  # not safe-listed yet
    ok = rq.dismiss(normalized, path=queue_path)
    assert ok is True

    # The dismissed word is now on the safe-list → never masked again.
    assert sw.is_safe("Wrongword") is True
    assert sw.is_safe("wrongword") is True  # case-insensitive


def test_dismiss_safe_list_failure_does_not_break_dismiss(tmp_path, monkeypatch):
    """Fail-open: if the safe-list write raises, dismiss still succeeds."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    import bubble_shield.review_queue as rq
    importlib.reload(rq)
    import bubble_shield.safe_words as sw
    importlib.reload(sw)

    queue_path = tmp_path / "review_queue.json"
    normalized = rq.add_candidate("Anotherword", "NOM", "doc2.pdf", path=queue_path)
    assert normalized is not None

    monkeypatch.setattr(sw, "add_safe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # dismiss must still move the item to the dismissed_log despite the safe-list error.
    assert rq.dismiss(normalized, path=queue_path) is True
    assert any(it.get("status") == "dismissed" for it in rq.list_dismissed(path=queue_path))


# ── /safe/add route (un-hide as not-PII) ──────────────────────────────────────

def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import webapp.app as appmod
    importlib.reload(appmod)
    return appmod


def test_safe_add_route_typed_confirm_and_audit_value_free(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    appmod = _fresh_app(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw
    importlib.reload(sw)
    client = TestClient(appmod.app)

    # Without confirm=SUR → no write.
    r = client.post("/safe/add", data={"value": "Patrimoine"}, follow_redirects=False)
    assert r.status_code == 303
    assert sw.is_safe("Patrimoine") is False

    # With confirm=SUR → safe-listed + audited.
    r = client.post("/safe/add", data={"value": "Patrimoine", "confirm": "SUR"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert sw.is_safe("Patrimoine") is True

    log = (tmp_path / "audit.jsonl").read_text()
    assert "safe_add" in log
    # Audit hygiene: the raw safe-listed word is NEVER in the audit line.
    assert "Patrimoine" not in log
    import json
    entry = json.loads(log.strip().splitlines()[-1])
    assert entry["event"] == "safe_add"
    assert entry["entity_type"] == "NOM"
    assert entry["counts"] == {"NOM": 1}
    assert "value" not in entry


def test_safe_add_route_ignores_value_b64_now_removed(tmp_path, monkeypatch):
    """#579 (2026-07-16): the reversible-base64 `value_b64` path was REMOVED (a devtools
    atob() on the DOM field recovered the cleartext — the same reversible-DOM leak #346
    fixed on /gazetteer/remove). The route was dormant, so posting `value_b64` is now a
    NO-OP: the value is NOT added (only a raw `value` add path remains)."""
    import base64
    from fastapi.testclient import TestClient
    appmod = _fresh_app(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw
    importlib.reload(sw)
    client = TestClient(appmod.app)

    b64 = base64.urlsafe_b64encode("Investissements".encode()).decode()
    r = client.post("/safe/add", data={"value_b64": b64, "confirm": "SUR"},
                    follow_redirects=False)
    assert r.status_code == 303  # route still responds (redirect)
    assert sw.is_safe("Investissements") is False, \
        "value_b64 is no longer decoded/added — the reversible-DOM channel is closed (#579)"
