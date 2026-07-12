import json, os, importlib
from pathlib import Path

def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import webapp.app as appmod
    importlib.reload(appmod)
    return appmod

def test_audit_event_writes_counts_only_line(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    appmod._audit_event(mission="m1", event="gazetteer_remove", token="NOM_0001", entity_type="NOM")
    log = Path(os.environ["BUBBLE_SHIELD_AUDIT_LOG"])
    assert log.exists()
    entry = json.loads(log.read_text().strip().splitlines()[-1])
    assert entry["event"] == "gazetteer_remove"
    assert entry["mission"] == "m1"
    assert entry["token"] == "NOM_0001"
    # no raw value field
    assert "value" not in entry


from fastapi.testclient import TestClient

def test_gazetteer_lists_entries(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    from bubble_shield.known_pii_store import add_confirmed_pii
    add_confirmed_pii("Testname Surname", "NOM")
    client = TestClient(appmod.app)
    r = client.get("/gazetteer")
    assert r.status_code == 200
    # masked, not raw, in the listing
    assert "Testname Surname" not in r.text
    assert "NOM" in r.text

def test_gazetteer_remove_drops_entry_and_audits(tmp_path, monkeypatch):
    """#346: /gazetteer/remove is keyed off the opaque row_id (an HMAC of the
    value, keyed on a server-only secret — see _gazetteer_row_id in
    webapp/app.py), not a raw `value` POST field. Posting the raw value
    directly is no longer accepted — nothing reversible should ever need to
    round-trip through a client, DOM or otherwise."""
    appmod = _fresh_app(tmp_path, monkeypatch)
    from bubble_shield.known_pii_store import add_confirmed_pii, is_known_pii
    add_confirmed_pii("Testname Surname", "NOM")
    client = TestClient(appmod.app)
    row_id = appmod._gazetteer_row_id("Testname Surname")
    r = client.post("/gazetteer/remove", data={"row_id": row_id}, follow_redirects=False)
    assert r.status_code == 303
    assert is_known_pii("Testname Surname") is False
    log = (tmp_path / "audit.jsonl").read_text()
    assert "gazetteer_remove" in log
    assert "Testname Surname" not in log  # no raw value in audit
