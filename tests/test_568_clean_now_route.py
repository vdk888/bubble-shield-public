from starlette.testclient import TestClient

from webapp.app import app


def test_clean_now_route_runs_and_redirects(monkeypatch):
    import webapp.app as wa
    monkeypatch.setattr(wa, "_depollute_now", lambda: {"unmasked": ["conseiller"], "logged": 1})
    c = TestClient(app)
    r = c.post("/gazetteer/depollute", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_clean_now_route_redirects_to_review_with_flash_count(monkeypatch):
    import webapp.app as wa
    monkeypatch.setattr(wa, "_depollute_now", lambda: {"unmasked": ["conseiller", "fiscal"], "logged": 2})
    c = TestClient(app)
    r = c.post("/gazetteer/depollute", follow_redirects=False)
    assert r.status_code in (302, 303)
    location = r.headers["location"]
    assert location.startswith("/review")
    assert "2" in location


def test_clean_now_route_calls_depollute_gazetteer_with_daemon_classify(monkeypatch):
    """The un-mocked _depollute_now must actually wire depollute_gazetteer +
    daemon_classify together (per the brief) — mock the underlying pieces to
    verify the wiring without needing a live Gemma daemon."""
    import webapp.app as wa

    calls = {}

    def fake_depollute_gazetteer(classify_fn, **kwargs):
        calls["classify_fn"] = classify_fn
        return {"unmasked": [], "kept": [], "logged": 0}

    monkeypatch.setattr("bubble_shield.depollute.depollute_gazetteer", fake_depollute_gazetteer)

    result = wa._depollute_now()
    assert result == {"unmasked": [], "kept": [], "logged": 0}
    from bubble_shield.depollute import daemon_classify
    assert calls["classify_fn"] is daemon_classify


def test_review_inbox_shows_depollute_sourced_item(monkeypatch, tmp_path):
    """A pending item whose doc_refs contains 'depollute' (the source string
    the T5 pipeline passes to add_candidate) must render with an audit label
    in the review inbox, using the existing confirm/dismiss buttons."""
    import bubble_shield.review_queue as rq

    q = tmp_path / "queue.json"
    rq.add_candidate("conseiller", "NOM", "depollute", path=q)

    import webapp.app as wa

    # app.py's /review route calls feed_from_sidecar_all/expire_old/list_pending
    # with no path= (defaults to BUBBLE_SHIELD_HOME). Neutralize the feed/expire
    # side effects and redirect list_pending to our isolated tmp queue.
    monkeypatch.setattr(rq, "feed_from_sidecar_all", lambda **kw: 0)
    monkeypatch.setattr(rq, "expire_old", lambda **kw: 0)
    orig_list_pending = rq.list_pending
    monkeypatch.setattr(rq, "list_pending", lambda **kw: orig_list_pending(path=q))

    c = TestClient(wa.app)
    r = c.get("/review")
    assert r.status_code == 200
    assert "conseiller" in r.text
    assert "dé-masqué" in r.text.lower() or "depollute" in r.text.lower()
