"""
tests/test_387_setup_all_models.py — #387 one-pass model setup.

Covers the skip-if-present + per-model status logic WITHOUT triggering any real
multi-GB download:

1. setup_ml.model_present() is a pure disk predicate (present iff onnx exists).
2. setup_ocr.ocr_models_present() reflects the cache sentinel.
3. MCP _model_states() reports gliner/openai/ocr present|absent from disk.
4. MCP _per_model_line() names each model + its state for the user.
5. MCP _setup_start() is a NO-OP that reports "déjà présent" for all three when
   every model is already on disk (the 2nd-run / fresh-machine-with-models case)
   — and spawns NO download subprocess.
6. MCP _setup_status() reports "ready" with all three present, "absent" when none.

Every test runs under a TEMP BUBBLE_SHIELD_HOME — the real ~/.bubble_shield is
never touched and no model is ever downloaded. All data synthetic.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))


# GLiNER / OpenAI-PF / OCR layout the setup + MCP agree on.
_GLINER_DIR = "onnx-community__gliner_multi_pii-v1"
_GLINER_ONNX = "onnx/model_quantized.onnx"
_OPENAI_DIR = "openai__privacy-filter"
_OPENAI_ONNX = "onnx/model_q4.onnx"


def _reload_under_home(monkeypatch, home: Path):
    """Point BUBBLE_SHIELD_HOME at `home` and (re)import the modules so their
    module-level paths repoint to the temp dir."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    import bubble_shield_setup_ml as ml
    import bubble_shield_setup_ocr as ocr
    import bubble_shield_mcp as mcp
    importlib.reload(ml)
    importlib.reload(ocr)
    importlib.reload(mcp)
    return ml, ocr, mcp


def _make_model(home: Path, sub: str, onnx_rel: str) -> None:
    """Create a fake model dir with the onnx file present (no real weights)."""
    f = home / "models" / sub / onnx_rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("fake-onnx", encoding="utf-8")


def _make_all(home: Path) -> None:
    _make_model(home, _GLINER_DIR, _GLINER_ONNX)
    _make_model(home, _OPENAI_DIR, _OPENAI_ONNX)
    (home / "layout_model_cached.flag").parent.mkdir(parents=True, exist_ok=True)
    (home / "layout_model_cached.flag").write_text("cached", encoding="utf-8")


# --- 1. setup_ml.model_present ----------------------------------------------

def test_model_present_predicate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ml, _ocr, _mcp = _reload_under_home(monkeypatch, home)
    assert ml.model_present(ml.DEFAULT_MODEL, ml.DEFAULT_ONNX) is False
    _make_model(home, _GLINER_DIR, _GLINER_ONNX)
    assert ml.model_present(ml.DEFAULT_MODEL, ml.DEFAULT_ONNX) is True


# --- 2. setup_ocr.ocr_models_present ----------------------------------------

def test_ocr_models_present_predicate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, ocr, _mcp = _reload_under_home(monkeypatch, home)
    assert ocr.ocr_models_present() is False
    (home).mkdir(parents=True, exist_ok=True)
    (home / "layout_model_cached.flag").write_text("cached", encoding="utf-8")
    assert ocr.ocr_models_present() is True


# --- 3. MCP _model_states ----------------------------------------------------

def test_model_states_absent_then_present(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, _ocr, mcp = _reload_under_home(monkeypatch, home)
    assert mcp._model_states() == {"gliner": "absent", "openai": "absent", "ocr": "absent"}
    _make_all(home)
    assert mcp._model_states() == {"gliner": "present", "openai": "present", "ocr": "present"}


# --- 4. MCP _per_model_line --------------------------------------------------

def test_per_model_line_names_each_model(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, _ocr, mcp = _reload_under_home(monkeypatch, home)
    line = mcp._per_model_line({"gliner": "present", "openai": "absent", "ocr": "absent"},
                               downloading=True)
    assert "GLiNER ✓ déjà présent" in line
    assert "OpenAI-PF ↓ téléchargement" in line
    assert "OCR ↓ téléchargement" in line


# --- 5. MCP _setup_start no-op when all present (no download spawned) ---------

def test_setup_start_all_present_is_noop(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, _ocr, mcp = _reload_under_home(monkeypatch, home)
    _make_all(home)

    # Guard: if _setup_start tried to download, it would Popen a subprocess.
    import subprocess
    called = {"popen": False}
    orig = subprocess.Popen

    def _no_popen(*a, **k):  # noqa: ANN001
        called["popen"] = True
        return orig(["true"])

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    r = mcp._setup_start()
    assert r["state"] == "ready"
    assert r["models"] == {"gliner": "present", "openai": "present", "ocr": "present"}
    assert "déjà présent" in r["per_model"]
    assert called["popen"] is False, "all-present setup must NOT spawn a download"


# --- 6. MCP _setup_status ----------------------------------------------------

def test_setup_status_ready_when_all_present(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, _ocr, mcp = _reload_under_home(monkeypatch, home)
    _make_all(home)
    r = mcp._setup_status()
    assert r["state"] == "ready"
    assert r["models"] == {"gliner": "present", "openai": "present", "ocr": "present"}
    assert r["per_model"].count("✓ déjà présent") == 3


def test_setup_status_absent_when_none(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _ml, _ocr, mcp = _reload_under_home(monkeypatch, home)
    r = mcp._setup_status()
    assert r["state"] == "absent"
    assert r["models"] == {"gliner": "absent", "openai": "absent", "ocr": "absent"}


# --- defaults: OpenAI-PF is ON by default now (#387) -------------------------

def test_openai_default_on(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ml, _ocr, _mcp = _reload_under_home(monkeypatch, home)
    args = ml.argparse.ArgumentParser()
    # mirror main()'s parser additions for the --openai default
    args.add_argument("--openai", action="store_true", default=True)
    args.add_argument("--no-openai", dest="openai", action="store_false")
    assert args.parse_args([]).openai is True
    assert args.parse_args(["--no-openai"]).openai is False
