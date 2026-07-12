"""tests/test_venv_py312_pin.py — the ML/OCR venvs must PIN Python 3.12.

CONTEXT (why this file exists)
------------------------------
Bubble Shield's three ML venvs (ml-env, gemma-env, ocr-env) used to be created
with ``venv.create(...)`` / ``venv.EnvBuilder().create(...)`` — i.e. with
"whatever python launched the setup script". On a stock Mac that launcher is
/usr/bin/python3 == 3.9.6, so the venvs got pinned to 3.9 BY ACCIDENT. Nobody
chose 3.9; it caused LibreSSL warnings + env flakiness and (critically) blocked
the Gemma vision swap because mlx_vlm needs Python 3.10+.

The fix (#venv-py312): both setup scripts now select a Python 3.12 interpreter
EXPLICITLY via find_python312() and create every venv with it. find_python312()
searches PATH GENERICALLY (python3.12, then any verified-3.12 candidate) — it
does NOT hardcode /opt/homebrew (a client Mac won't have Homebrew). If no 3.12
is present it raises a clear, actionable RuntimeError instead of silently
falling back to the accidental 3.9.

These tests pin that behaviour without creating a real venv or needing 3.12 on
the CI machine (subprocess is mocked).
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_setup_ml as ml  # noqa: E402
import bubble_shield_setup_ocr as ocr  # noqa: E402

MODULES = [ml, ocr]


class _FakeCompleted:
    def __init__(self, stdout="3.12", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.mark.parametrize("mod", MODULES)
def test_find_python312_prefers_python3_12_on_path(mod, monkeypatch):
    """find_python312 returns the `python3.12` PATH entry when present, and
    verifies its version by actually running it."""
    monkeypatch.setattr(mod.shutil, "which",
                        lambda name: "/somewhere/python3.12" if name == "python3.12" else None)

    def _fake_run(cmd, *a, **k):
        # Only the version probe is expected here; report 3.12.
        return _FakeCompleted(stdout="3.12")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    assert mod.find_python312() == "/somewhere/python3.12"


@pytest.mark.parametrize("mod", MODULES)
def test_find_python312_stable_path_is_in_candidates(mod):
    """#604 — install-app.sh's bare-Mac provisioner runs in a SEPARATE process
    from the setup script and does not export its PATH mutation, so
    find_python312() must independently probe the stable install path it
    provisions the relocatable interpreter into
    (~/.bubble_shield/py312/python/bin/python3.12), not rely on PATH alone.

    NOTE: asserts the PATH SHAPE (suffix) rather than an exact `Path.home()`
    equality, and reloads the module fresh under the REAL (unpatched) HOME
    first. Other test files in this suite (e.g. test_setup_ml_stable_daemon.py's
    `_reload_under_home`) `importlib.reload()` these modules under a
    monkeypatched HOME and never reload back — leaving module-level
    Path.home()-derived globals pinned to a stale tmp HOME for the rest of the
    process if this test ran later and didn't defend against it."""
    assert hasattr(mod, "STABLE_PY312"), (
        f"{mod.__name__} must expose STABLE_PY312 (the #604 provisioned-Python "
        "discovery path)"
    )
    fresh_mod = importlib.reload(mod)  # re-pin any Path.home()-derived globals to the real HOME
    expected = str(Path.home() / ".bubble_shield" / "py312" / "python" / "bin" / "python3.12")
    assert fresh_mod.STABLE_PY312 == expected


@pytest.mark.parametrize("mod", MODULES)
def test_find_python312_discovers_stable_path_when_absent_from_path(mod, monkeypatch):
    """Simulate a bare Mac where install-app.sh provisioned 3.12 at the stable
    path but PATH (in this separate process) has no python3.12 / python3 that
    resolves to it — find_python312() must still discover it via STABLE_PY312."""
    # Nothing resolves via PATH lookups in this process.
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.sys, "executable", "/usr/bin/python3")  # stock 3.9 launcher

    def _fake_run(cmd, *a, **k):
        # Only the STABLE_PY312 path (and sys.executable / which("python3"),
        # both None/absent here) get probed; report 3.12 ONLY for the stable
        # provisioned path, mirroring a real bare-Mac post-#604-install state.
        if cmd[0] == mod.STABLE_PY312:
            return _FakeCompleted(stdout="3.12")
        return _FakeCompleted(stdout="3.9")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    assert mod.find_python312() == mod.STABLE_PY312


@pytest.mark.parametrize("mod", MODULES)
def test_find_python312_is_generic_not_homebrew_hardcoded(mod):
    """No CODE line may hardcode /opt/homebrew — discovery has to be generic so a
    bare client Mac (no Homebrew) still works once 3.12 is provisioned. Comments
    may mention /opt/homebrew (they explain WHY it's avoided); we only reject it
    appearing on a non-comment source line."""
    # AST-level check: /opt/homebrew must not appear as a STRING LITERAL in the
    # module (a hardcoded path). Comments and docstrings that merely explain why
    # we avoid it are fine. Docstrings are Constant nodes too, so we skip the
    # module/function docstrings explicitly by only flagging string constants
    # that contain a path-like /opt/homebrew AND are not a docstring.
    import ast
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds:
                docstrings.add(ds)
    offending = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "/opt/homebrew" in node.value and node.value not in docstrings
    ]
    assert not offending, (
        "setup script must not hardcode /opt/homebrew as a string literal — "
        f"search python3.12 on PATH generically. Offending: {offending}")


@pytest.mark.parametrize("mod", MODULES)
def test_find_python312_raises_actionable_error_when_absent(mod, monkeypatch):
    """When no 3.12 is present (only stock 3.9), find_python312 raises a clear
    RuntimeError rather than silently pinning 3.9."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.sys, "executable", "/usr/bin/python3")

    def _fake_run_39(cmd, *a, **k):
        return _FakeCompleted(stdout="3.9")  # every candidate reports 3.9

    monkeypatch.setattr(mod.subprocess, "run", _fake_run_39)
    with pytest.raises(RuntimeError) as exc:
        mod.find_python312()
    msg = str(exc.value)
    assert "3.12" in msg and "python3.12" in msg


def test_ml_ensure_venv_creates_with_py312(monkeypatch, tmp_path):
    """ensure_venv() (ml-env) must create the venv with the 3.12 interpreter via
    `<py312> -m venv`, NOT with the venv module / launching interpreter."""
    monkeypatch.setattr(ml, "ML_ENV", tmp_path / "ml-env")
    monkeypatch.setattr(ml, "find_python312", lambda: "/pinned/python3.12")
    calls = []
    monkeypatch.setattr(ml.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd) or None)
    ml.ensure_venv()
    assert calls, "ensure_venv should shell out to create the venv"
    cmd = calls[0]
    assert cmd[0] == "/pinned/python3.12"
    assert "venv" in cmd and str(tmp_path / "ml-env") in cmd


def test_ocr_ensure_venv_creates_with_py312(monkeypatch, tmp_path):
    """ensure_venv() (ocr-env) must create the venv with the 3.12 interpreter."""
    monkeypatch.setattr(ocr, "OCR_ENV", tmp_path / "ocr-env")
    monkeypatch.setattr(ocr, "find_python312", lambda: "/pinned/python3.12")
    calls = []
    monkeypatch.setattr(ocr.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd) or None)
    ocr.ensure_venv()
    assert calls, "ensure_venv should shell out to create the venv"
    cmd = calls[0]
    assert cmd[0] == "/pinned/python3.12"
    assert "venv" in cmd and str(tmp_path / "ocr-env") in cmd
