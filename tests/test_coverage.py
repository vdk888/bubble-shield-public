# tests/test_coverage.py
from bubble_shield import coverage, shadow_store


def test_coverage_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    root = tmp_path / "p"; root.mkdir()
    (root/"a.txt").write_text("a"); (root/"b.txt").write_text("b")
    shadow_store.put_shadow(shadow_store.content_hash(root/"a.txt"), "clean-a")
    c = coverage.coverage(str(root))
    assert c["total"] == 2 and c["indexed"] == 1 and abs(c["pct"] - 50.0) < 0.01
    assert str(root/"b.txt") in c["pending_files"]
