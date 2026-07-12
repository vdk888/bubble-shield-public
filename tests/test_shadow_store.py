import os
from pathlib import Path
from bubble_shield import shadow_store

def test_store_path_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    assert shadow_store.store_path() == tmp_path / "shield.db"

def test_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    conn = shadow_store.connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shadows" in names and "gazetteer" in names
    conn.close()

def test_put_then_get_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    shadow_store.put_shadow("abc123", "le client ⟦NOM_0001⟧", src_path="/x/y.pdf")
    assert shadow_store.get_shadow("abc123") == "le client ⟦NOM_0001⟧"

def test_get_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    assert shadow_store.get_shadow("nope") is None

def test_store_file_mode_is_0600_after_put_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    shadow_store.put_shadow("abc123", "le client ⟦NOM_0001⟧", src_path="/x/y.pdf")
    p = shadow_store.store_path()
    assert p.is_file()
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600
    dir_mode = os.stat(p.parent).st_mode & 0o777
    assert dir_mode == 0o700

def test_content_hash_stable_and_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    f = tmp_path / "d.txt"; f.write_text("hello")
    h1 = shadow_store.content_hash(f)
    assert h1 == shadow_store.content_hash(f)        # stable
    f.write_text("hello world")
    assert shadow_store.content_hash(f) != h1        # changes on edit

def test_list_indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    shadow_store.put_shadow("h1", "a"); shadow_store.put_shadow("h2", "b")
    assert shadow_store.list_indexed() == {"h1", "h2"}

def test_mark_and_clear_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    shadow_store.mark_pending("/x/a.pdf")
    assert "/x/a.pdf" in shadow_store.pending_files()
    shadow_store.clear_pending("/x/a.pdf")
    assert "/x/a.pdf" not in shadow_store.pending_files()
