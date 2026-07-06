"""tests/test_setup_ml_stable_daemon.py — LaunchAgent must point at a STABLE path.

CONTEXT (why this file exists)
------------------------------
install_launchagent() used to write a macOS LaunchAgent whose ProgramArguments
pointed at ``Path(__file__).parent / bubble_shield_nerd.py``. Inside Cowork,
__file__ resolves to an EPHEMERAL per-session plugin cache dir, e.g.
``…/local-agent-mode-sessions/<s>/rpm/plugin_<id>/.mcpb-cache/<hash>/server/scripts/…``.
Cowork garbage-collects that cache dir on every plugin update, so the
LaunchAgent then points at a DELETED path → launchd crash-loops with Errno 2 and
the daemon never starts from the agent. Reads fail-closed ("NER down") after
every plugin update.

The fix: install_daemon_to_stable_path() copies the daemon script + its vendored
deps out of the ephemeral plugin cache into ~/.bubble_shield/daemon (a stable
home that survives plugin updates), and install_launchagent() points launchd
there instead. These tests pin that behaviour.

Every test runs under a TEMP BUBBLE_SHIELD_HOME — the real ~/.bubble_shield is
never touched, launchctl is mocked, and nothing is downloaded. All data is
synthetic.
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
    """Point BUBBLE_SHIELD_HOME at `home` and (re)import the module so its
    module-level paths (BUBBLE_SHIELD_HOME, STABLE_DAEMON_ROOT, LAUNCH_PLIST…)
    repoint to the temp dir."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    # The plist is written under $HOME/Library/LaunchAgents — pin HOME to the tmp
    # dir so a test never writes into the real ~/Library/LaunchAgents.
    monkeypatch.setenv("HOME", str(home / "user_home"))
    (home / "user_home").mkdir(parents=True, exist_ok=True)
    import bubble_shield_setup_ml as ml
    importlib.reload(ml)
    return ml


def _mock_launchctl(monkeypatch, ml):
    """Stub subprocess.run so launchctl load/unload is never really invoked."""
    calls = []

    class _R:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, *a, **k):  # noqa: ANN001
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(ml.subprocess, "run", _fake_run)
    return calls


def test_install_daemon_to_stable_path_creates_layout(monkeypatch, tmp_path):
    """install_daemon_to_stable_path() copies scripts/ + vendor/ into the stable
    root, preserving the layout the daemon expects."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)

    stable_nerd = ml.install_daemon_to_stable_path()

    daemon_root = home / "daemon"
    assert (daemon_root / "scripts" / "bubble_shield_nerd.py").is_file(), \
        "stable daemon script missing"
    assert (daemon_root / "vendor").is_dir(), "stable vendor tree missing"
    # The daemon reads here.parent/vendor/bubble_shield/custom_fields.json and
    # imports from vendor/bubble_shield — that package must be present.
    assert (daemon_root / "vendor" / "bubble_shield").is_dir(), \
        "vendor/bubble_shield package missing at stable path"
    # Sibling setup script the daemon references for on-demand OpenAI fetch.
    assert (daemon_root / "scripts" / "bubble_shield_setup_ml.py").is_file()
    # Returned path is the stable one.
    assert stable_nerd == daemon_root / "scripts" / "bubble_shield_nerd.py"


def test_stable_scripts_is_minimal_allowlist_not_whole_dir(monkeypatch, tmp_path):
    """The scripts/ portion of the stable daemon copy is an EXPLICIT allowlist,
    NOT the whole scripts/ dir. On a privacy tool a SECOND stale guard.py under
    ~/.bubble_shield/ is a footgun (wrong-guard resolution), and dragging the
    hook installers, tripwire and ~30 test_*.py files in is pure dead weight.

    Pins: the daemon (nerd.py) + its only scripts/-sibling runtime dep
    (setup_ml.py) ARE copied; guard.py, the hook installers, tripwire,
    posttool_anonymize and every test_*.py are NOT."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)

    ml.install_daemon_to_stable_path()
    stable_scripts = home / "daemon" / "scripts"

    # What the daemon needs — MUST be present.
    assert (stable_scripts / "bubble_shield_nerd.py").is_file()
    assert (stable_scripts / "bubble_shield_setup_ml.py").is_file()

    # Privacy footgun / dead weight — MUST NOT be present.
    forbidden = [
        "guard.py",
        "posttool_anonymize.py",
        "install_user_hooks.py",
        "uninstall_user_hooks.py",
        "tripwire.py",
    ]
    for name in forbidden:
        assert not (stable_scripts / name).exists(), \
            f"{name} must NOT be copied into the stable daemon dir"

    # No test_*.py files leaked in.
    leaked_tests = sorted(p.name for p in stable_scripts.glob("test_*.py"))
    assert leaked_tests == [], \
        f"no test_*.py may be copied into the stable daemon dir; found {leaked_tests}"

    # And the allowlist constant itself must not contain any forbidden name —
    # guards against a future edit re-adding guard.py to the allowlist.
    for name in forbidden:
        assert name not in ml._DAEMON_SCRIPTS, \
            f"{name} must never be in the _DAEMON_SCRIPTS allowlist"


def test_plist_program_arguments_under_stable_root(monkeypatch, tmp_path):
    """The written plist's ProgramArguments daemon path is UNDER the stable
    daemon root and contains NO ephemeral-cache markers."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    py = home / "ml-env" / "bin" / "python"  # stable venv python (not ephemeral)
    ml.install_launchagent(py)

    plist_text = ml.LAUNCH_PLIST.read_text(encoding="utf-8")
    stable_nerd = str(home / "daemon" / "scripts" / "bubble_shield_nerd.py")
    assert stable_nerd in plist_text, \
        "plist must point at the stable daemon path"
    assert ".mcpb-cache" not in plist_text, \
        "plist must not reference the ephemeral plugin cache"
    assert "local-agent-mode-sessions" not in plist_text, \
        "plist must not reference a per-session Cowork dir"


def test_rerun_refreshes_stable_copy(monkeypatch, tmp_path):
    """A re-run of the install refreshes the stable copy to the current source
    (update-safe): a stale sentinel written into the stable copy is overwritten
    by the fresh source content on the next install."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)

    ml.install_daemon_to_stable_path()
    stable_nerd = home / "daemon" / "scripts" / "bubble_shield_nerd.py"
    source_bytes = (_SCRIPTS / "bubble_shield_nerd.py").read_bytes()

    # Simulate an out-of-date stable copy (as if the plugin was updated and the
    # old stable copy is stale).
    stable_nerd.write_text("STALE SENTINEL — old daemon version", encoding="utf-8")
    assert stable_nerd.read_bytes() != source_bytes

    # Re-run: the stable copy must be refreshed to match the live source.
    ml.install_daemon_to_stable_path()
    assert stable_nerd.read_bytes() == source_bytes, \
        "re-run must refresh the stable daemon copy from the live source"


def test_fallback_to_ephemeral_when_copy_fails(monkeypatch, tmp_path):
    """If the stable copy raises, install_launchagent falls back to the __file__
    path and still writes the plist — setup never hard-fails."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    # Force the copy to raise.
    def _boom(*a, **k):  # noqa: ANN001
        raise OSError("disk full / permission denied (synthetic)")

    monkeypatch.setattr(ml.shutil, "copytree", _boom)

    py = home / "ml-env" / "bin" / "python"
    # Must NOT raise.
    ml.install_launchagent(py)

    assert ml.LAUNCH_PLIST.is_file(), "plist must still be written on fallback"
    plist_text = ml.LAUNCH_PLIST.read_text(encoding="utf-8")
    # Fallback path is the ephemeral plugin scripts dir (where __file__ lives).
    fallback_nerd = str(Path(ml.__file__).resolve().parent / "bubble_shield_nerd.py")
    assert fallback_nerd in plist_text, \
        "fallback must write the __file__-relative daemon path into the plist"
