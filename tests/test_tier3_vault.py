import importlib, os
from pathlib import Path
from fastapi.testclient import TestClient


def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import webapp.app as appmod
    importlib.reload(appmod)
    return appmod


def _seed_vault(tmp_path, mission="m1"):
    from bubble_shield.vault import Vault
    v = Vault(mission=mission)
    tok = v.token_for("Testname Surname", "NOM")
    vdir = tmp_path / "vaults"; vdir.mkdir(parents=True, exist_ok=True)
    v.save(vdir / f"{mission}.vault.json")
    return tok


def test_vault_missions_lists(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    _seed_vault(tmp_path, "m1")
    client = TestClient(appmod.app)
    r = client.get("/vault")
    assert r.status_code == 200
    assert "m1" in r.text


def test_vault_detail_masks_values(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    _seed_vault(tmp_path, "m1")
    client = TestClient(appmod.app)
    r = client.get("/vault/m1")
    assert r.status_code == 200
    # raw value NEVER in the page source
    assert "Testname Surname" not in r.text


def test_vault_reveal_returns_value_and_audits(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    tok = _seed_vault(tmp_path, "m1")
    client = TestClient(appmod.app)
    token_inner = tok.strip("⟦⟧")  # token without the ⟦⟧ brackets
    r = client.get(f"/vault/m1/reveal/{token_inner}")
    assert r.status_code == 200
    assert "Testname Surname" in r.json()["value"]
    log = (tmp_path / "audit.jsonl").read_text()
    assert "vault_reveal" in log
    assert "Testname Surname" not in log  # value not in audit


def test_vault_rectify_changes_value_keeps_token(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    tok = _seed_vault(tmp_path, "m1")
    token_inner = tok.strip("⟦⟧")
    client = TestClient(appmod.app)
    r = client.post("/vault/m1/rectify",
                    data={"token": token_inner, "new_value": "Corrected Name"},
                    follow_redirects=False)
    assert r.status_code == 303
    from bubble_shield.vault import Vault
    v = Vault.load(tmp_path / "vaults" / "m1.vault.json")
    assert v.value_for(tok) == "Corrected Name"
    log = (tmp_path / "audit.jsonl").read_text()
    assert "vault_rectify" in log
    assert "Corrected Name" not in log


def test_vault_forget_requires_typed_confirm(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    tok = _seed_vault(tmp_path, "m1")
    token_inner = tok.strip("⟦⟧")
    client = TestClient(appmod.app)
    # without confirm=OUBLIER → rejected, mapping survives
    r = client.post("/vault/m1/forget", data={"token": token_inner, "confirm": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    from bubble_shield.vault import Vault
    v = Vault.load(tmp_path / "vaults" / "m1.vault.json")
    assert v.value_for(tok) is not None  # NOT forgotten


def test_vault_forget_with_confirm_removes_and_audits(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    tok = _seed_vault(tmp_path, "m1")
    token_inner = tok.strip("⟦⟧")
    client = TestClient(appmod.app)
    r = client.post("/vault/m1/forget", data={"token": token_inner, "confirm": "OUBLIER"},
                    follow_redirects=False)
    assert r.status_code == 303
    from bubble_shield.vault import Vault
    v = Vault.load(tmp_path / "vaults" / "m1.vault.json")
    assert v.value_for(tok) is None  # forgotten
    log = (tmp_path / "audit.jsonl").read_text()
    assert "vault_forget" in log


def test_forget_subject_count_preview(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    from bubble_shield.vault import Vault
    v = Vault(mission="m1")
    v.token_for("Dupont Jean", "NOM")
    v.token_for("dupont@example.com", "EMAIL")
    vdir = tmp_path / "vaults"; vdir.mkdir(parents=True, exist_ok=True)
    v.save(vdir / "m1.vault.json")
    client = TestClient(appmod.app)
    r = client.get("/vault/m1/forget-subject-count", params={"q": "dupont"})
    assert r.status_code == 200
    assert "2" in r.text  # both entries matched


def test_forget_subject_wipes_with_confirm(tmp_path, monkeypatch):
    appmod = _fresh_app(tmp_path, monkeypatch)
    from bubble_shield.vault import Vault
    v = Vault(mission="m1")
    v.token_for("Dupont Jean", "NOM")
    v.token_for("dupont@example.com", "EMAIL")
    vdir = tmp_path / "vaults"; vdir.mkdir(parents=True, exist_ok=True)
    v.save(vdir / "m1.vault.json")
    client = TestClient(appmod.app)
    r = client.post("/vault/m1/forget-subject", data={"q": "dupont", "confirm": "OUBLIER"},
                    follow_redirects=False)
    assert r.status_code == 303
    v2 = Vault.load(vdir / "m1.vault.json")
    assert len(v2.to_token) == 0
