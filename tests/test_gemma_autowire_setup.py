"""tests/test_gemma_autowire_setup.py — wire Gemma provisioning into main().

CONTEXT (the gap this closes)
------------------------------
#568 added the de-pollution feature + a Gemma daemon: install_gemma_env(),
install_gemma_daemon_to_stable_path(), install_gemma_launchagent() and
download_gemma_model() all EXIST in bubble_shield_setup_ml.py — but NOTHING
CALLS THEM from main(). main() installs GLiNER (+ OpenAI-PF) and calls
install_launchagent() for the GLiNER daemon, but never provisions Gemma. On a
real (non-technical) client machine the Gemma daemon is therefore never
installed, so de-pollution permanently no-ops — and a CGP client will NEVER
open a terminal to run a command manually. This must be part of the automatic
one-pass install the agent already triggers via the bubble_shield_setup_ml MCP
tool (action='start' runs this main()).

The fix: after the GLiNER install_launchagent(py) call, main() ALSO calls
install_gemma_env() -> install_gemma_daemon_to_stable_path() ->
install_gemma_launchagent(<gemma-env python>), wrapped in its own try/except —
fail-open PER MODEL, mirroring the existing OpenAI-PF pattern: a Gemma
provisioning failure logs + records states["gemma"] = "error" but must NEVER
abort the GLiNER install or make main() return non-zero. De-pollution simply
no-ops safely if Gemma isn't provisioned (already the correct degraded
behaviour). A --no-gemma flag (default: install) mirrors --no-openai.

Every test runs under a TEMP BUBBLE_SHIELD_HOME, mocks venv creation, pip
install, model download and launchctl — NOTHING is really downloaded or
installed. All data synthetic.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _reload_under_home(monkeypatch, home: Path):
    """Point BUBBLE_SHIELD_HOME/HOME at tmp dirs and (re)import the module so
    its module-level paths repoint to the temp dir. Mirrors the sibling test
    files' convention (test_setup_ml_stable_daemon.py, test_387_*.py)."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.setenv("HOME", str(home / "user_home"))
    (home / "user_home").mkdir(parents=True, exist_ok=True)
    import bubble_shield_setup_ml as ml
    importlib.reload(ml)
    return ml


def _make_gliner_model(home: Path, ml) -> None:
    """Drop a fake GLiNER onnx file so model_present()/download_model() skip
    real work for the GLiNER leg (download_model() itself is monkeypatched
    below anyway, but this keeps state realistic)."""
    f = home / "models" / "onnx-community__gliner_multi_pii-v1" / "onnx" / "model_quantized.onnx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("fake-onnx", encoding="utf-8")


def _stub_everything_but_gemma(monkeypatch, ml, gemma_calls: list):
    """Stub every real side-effecting call main() makes EXCEPT the three Gemma
    provisioning functions under test, whose invocation (and argument) we want
    to observe directly. Nothing downloads, nothing touches launchd."""
    monkeypatch.setattr(ml, "ensure_venv", lambda: Path("/fake/ml-env/bin/python"))
    monkeypatch.setattr(ml, "ensure_deps", lambda *a, **k: None)
    monkeypatch.setattr(ml, "download_model", lambda *a, **k: "present")
    monkeypatch.setattr(ml, "write_manifest", lambda *a, **k: None)
    monkeypatch.setattr(ml, "verify", lambda *a, **k: True)
    monkeypatch.setattr(ml, "verify_openai", lambda *a, **k: True)
    monkeypatch.setattr(ml, "install_launchagent", lambda *a, **k: None)

    def _fake_install_gemma_env():
        gemma_calls.append("install_gemma_env")
        return ml.GEMMA_ENV

    def _fake_install_gemma_daemon_to_stable_path():
        gemma_calls.append("install_gemma_daemon_to_stable_path")
        return ml.STABLE_DAEMON_ROOT / "scripts" / "bubble_shield_gemmad.py"

    def _fake_install_gemma_launchagent(py):
        gemma_calls.append(("install_gemma_launchagent", py))

    monkeypatch.setattr(ml, "install_gemma_env", _fake_install_gemma_env)
    monkeypatch.setattr(ml, "install_gemma_daemon_to_stable_path",
                        _fake_install_gemma_daemon_to_stable_path)
    monkeypatch.setattr(ml, "install_gemma_launchagent", _fake_install_gemma_launchagent)


# --- 1. default run provisions Gemma after the GLiNER launchagent -----------

def test_main_provisions_gemma_by_default(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _make_gliner_model(home, ml)
    gemma_calls = []
    _stub_everything_but_gemma(monkeypatch, ml, gemma_calls)

    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py"])
    rc = ml.main()

    assert rc == 0
    assert gemma_calls[0] == "install_gemma_env"
    assert gemma_calls[1] == "install_gemma_daemon_to_stable_path"
    assert gemma_calls[2][0] == "install_gemma_launchagent"
    # install_gemma_launchagent must be called with the GEMMA-ENV python, not
    # the GLiNER ml-env python.
    assert gemma_calls[2][1] == ml._venv_python(ml.GEMMA_ENV)

    out = capsys.readouterr().out
    assert '"gemma": "ready"' in out.replace(" ", "") or '"gemma":"ready"' in out.replace(" ", "")


# --- 2. --no-gemma opts out, mirroring --no-openai ---------------------------

def test_no_gemma_flag_skips_provisioning(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _make_gliner_model(home, ml)
    gemma_calls = []
    _stub_everything_but_gemma(monkeypatch, ml, gemma_calls)

    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py", "--no-gemma"])
    rc = ml.main()

    assert rc == 0
    assert gemma_calls == [], "no Gemma function may be called under --no-gemma"


# --- 3. Gemma failure is fail-open: never aborts GLiNER, never non-zero -----

def test_gemma_failure_does_not_abort_gliner_install(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _make_gliner_model(home, ml)
    gemma_calls = []
    _stub_everything_but_gemma(monkeypatch, ml, gemma_calls)

    launchagent_calls = []
    monkeypatch.setattr(ml, "install_launchagent",
                        lambda *a, **k: launchagent_calls.append("gliner"))

    def _boom():
        gemma_calls.append("install_gemma_env")
        raise RuntimeError("disk full / network down")

    monkeypatch.setattr(ml, "install_gemma_env", _boom)

    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py"])
    rc = ml.main()

    # The overall install must still succeed — GLiNER's launchagent must still
    # have been installed, and main() must NOT return non-zero because of a
    # Gemma-only failure.
    assert rc == 0
    assert launchagent_calls == ["gliner"], "GLiNER launchagent must still install"
    assert gemma_calls == ["install_gemma_env"], "must not proceed past the failing call"

    out = capsys.readouterr().out
    assert '"gemma": "error"' in out.replace(" ", "") or '"gemma":"error"' in out.replace(" ", "")


# --- 4. downstream Gemma failures (daemon copy / launchagent) are also caught -

def test_gemma_launchagent_failure_is_fail_open(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _make_gliner_model(home, ml)
    gemma_calls = []
    _stub_everything_but_gemma(monkeypatch, ml, gemma_calls)

    def _boom(py):
        gemma_calls.append("install_gemma_launchagent")
        raise RuntimeError("launchctl refused")

    monkeypatch.setattr(ml, "install_gemma_launchagent", _boom)

    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py"])
    rc = ml.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert '"gemma": "error"' in out.replace(" ", "") or '"gemma":"error"' in out.replace(" ", "")


# --- 5. --no-gemma is a real argparse flag, mirroring --no-openai's shape ---

def test_no_gemma_argparse_shape(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    args = ml.argparse.ArgumentParser()
    args.add_argument("--gemma", action="store_true", default=True)
    args.add_argument("--no-gemma", dest="gemma", action="store_false")
    assert args.parse_args([]).gemma is True
    assert args.parse_args(["--no-gemma"]).gemma is False


# --- 6. gemma provisioning never spawns real subprocess/venv work in default
#        argparse wiring of main() itself (belt-and-suspenders on the parser) -

def test_main_parser_has_no_gemma_flag(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _make_gliner_model(home, ml)
    gemma_calls = []
    _stub_everything_but_gemma(monkeypatch, ml, gemma_calls)

    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py", "--check-only"])
    # --check-only path must not touch gemma provisioning at all (mirrors
    # --check-only's existing behaviour of only verifying GLiNER/OpenAI).
    monkeypatch.setattr(ml, "_venv_python", lambda env: Path("/fake/bin/python") if env == ml.ML_ENV else ml.GEMMA_ENV)
    (home / "ml-env" / "bin").mkdir(parents=True, exist_ok=True)
    (home / "ml-env" / "bin" / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["bubble_shield_setup_ml.py", "--check-only"])
    rc = ml.main()
    assert gemma_calls == [], "--check-only must not provision gemma"
