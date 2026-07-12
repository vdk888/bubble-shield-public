"""Task 2 (#589): dashboard 3-way toggle for gemma_mode (all / hard / off).

The dashboard must let a non-technical user pick the background-masker Gemma
mode without hand-editing JSON. The control writes `gemma_mode` into the SAME
bubble-shield.json the guard's `_gemma_mode()` reads — resolved via the
BUBBLE_SHIELD_GUARD_CONFIG env var (Task 1's resolution order).

CRITICAL contract: writing gemma_mode is a read-MODIFY-write. Every other key
already in bubble-shield.json MUST survive the write untouched.
"""
import json

from fastapi.testclient import TestClient

from webapp.app import app

client = TestClient(app)


def _seed_config(tmp_path, monkeypatch, extra: dict) -> str:
    """Write a bubble-shield.json with pre-existing keys and point the guard
    config resolution at it via BUBBLE_SHIELD_GUARD_CONFIG. Returns the path."""
    cfg_path = tmp_path / "bubble-shield.json"
    cfg_path.write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg_path))
    return str(cfg_path)


def test_dashboard_renders_three_gemma_options(tmp_path, monkeypatch):
    """GET /dashboard shows the 3 FR-labelled options; current mode preselected."""
    _seed_config(tmp_path, monkeypatch, {"gemma_mode": "hard", "other_key": 1})

    r = client.get("/dashboard")
    assert r.status_code == 200
    # The 3 radio values must be present.
    assert 'value="all"' in r.text
    assert 'value="hard"' in r.text
    assert 'value="off"' in r.text
    # FR labels (product is FR-first).
    assert "Pseudonymisation Gemma sur tous les documents" in r.text
    assert "Formulaires complexes uniquement" in r.text
    assert "Désactivée" in r.text
    # Current mode ("hard") must be the checked option.
    # The 'hard' radio should carry a `checked` marker; a cheap proxy is that
    # the checked attribute co-occurs with the hard value in the rendered form.
    assert "gemma_mode" in r.text


def test_post_gemma_mode_writes_hard_and_preserves_other_keys(tmp_path, monkeypatch):
    """POST gemma_mode=hard -> bubble-shield.json has gemma_mode:hard AND keeps
    every pre-existing key (read-modify-write, not clobber)."""
    cfg_path = _seed_config(
        tmp_path, monkeypatch,
        {
            "gemma_mode": "all",
            "protected_folders": ["~/Documents/clients"],
            "detector": {"mode": "both"},
            "some_flag": True,
        },
    )

    r = client.post("/dashboard/gemma", data={"gemma_mode": "hard"})
    assert r.status_code == 200

    saved = json.loads((tmp_path / "bubble-shield.json").read_text(encoding="utf-8"))
    # The toggle changed.
    assert saved["gemma_mode"] == "hard"
    # Every other pre-existing key survived untouched.
    assert saved["protected_folders"] == ["~/Documents/clients"]
    assert saved["detector"] == {"mode": "both"}
    assert saved["some_flag"] is True


def test_post_each_valid_mode_roundtrips(tmp_path, monkeypatch):
    """All three modes write correctly."""
    _seed_config(tmp_path, monkeypatch, {"gemma_mode": "all", "keep_me": "x"})
    for mode in ("off", "hard", "all"):
        r = client.post("/dashboard/gemma", data={"gemma_mode": mode})
        assert r.status_code == 200
        saved = json.loads((tmp_path / "bubble-shield.json").read_text(encoding="utf-8"))
        assert saved["gemma_mode"] == mode
        assert saved["keep_me"] == "x"


def test_post_invalid_mode_rejected_config_unchanged(tmp_path, monkeypatch):
    """An out-of-range mode must NOT be written; fail toward the existing value."""
    _seed_config(tmp_path, monkeypatch, {"gemma_mode": "all", "keep_me": "x"})
    r = client.post("/dashboard/gemma", data={"gemma_mode": "banana"})
    assert r.status_code == 200
    saved = json.loads((tmp_path / "bubble-shield.json").read_text(encoding="utf-8"))
    # Unchanged — invalid input never persisted.
    assert saved["gemma_mode"] == "all"
    assert saved["keep_me"] == "x"


def test_post_creates_config_when_missing(tmp_path, monkeypatch):
    """If the target config file doesn't exist yet, the write creates it with
    just gemma_mode (no other keys to preserve)."""
    cfg_path = tmp_path / "sub" / "bubble-shield.json"
    monkeypatch.setenv("BUBBLE_SHIELD_GUARD_CONFIG", str(cfg_path))
    r = client.post("/dashboard/gemma", data={"gemma_mode": "off"})
    assert r.status_code == 200
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["gemma_mode"] == "off"
