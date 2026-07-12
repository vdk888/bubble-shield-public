import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str((HERE.parent / "vendor")))
import bubble_shield_mcp as M
from bubble_shield import shadow_store

def test_shadow_hit_serves_cached_no_models(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    f = tmp_path / "doc.txt"; f.write_text("raw content with Jean Dupont")
    h = shadow_store.content_hash(f)
    shadow_store.put_shadow(h, "cached ⟦NOM_0001⟧ shadow")
    # sabotage the model path so a hit that touched models would crash
    monkeypatch.setattr(M, "_anonymise_file", lambda p: (_ for _ in ()).throw(AssertionError("models called on hit")))
    assert M._read_with_shadow(str(f)) == "cached ⟦NOM_0001⟧ shadow"

def test_shadow_miss_serves_raw_no_models(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    f = tmp_path / "new.txt"; f.write_text("brand new unindexed text")
    monkeypatch.setattr(M, "_anonymise_file", lambda p: (_ for _ in ()).throw(AssertionError("models called on miss")))
    out = M._read_with_shadow(str(f))
    assert "brand new unindexed text" in out

def test_gazetteer_net_masks_leaked_name_in_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    from bubble_shield import known_pii_store
    known_pii_store.add_confirmed_pii("Moreau", "NOM")
    f = tmp_path / "d.txt"; f.write_text("x")
    h = shadow_store.content_hash(f)
    shadow_store.put_shadow(h, "le dossier de Moreau est complet")  # name leaked in shadow
    # sabotage the model path so the net cannot secretly touch models
    monkeypatch.setattr(M, "_anonymise_file", lambda p: (_ for _ in ()).throw(AssertionError("models called on hit")))
    out = M._read_with_shadow(str(f))
    assert "Moreau" not in out          # net caught it
