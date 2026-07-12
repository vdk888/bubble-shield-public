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


def test_plist_program_arguments_include_no_warm_flag(monkeypatch, tmp_path):
    """#lazywarm2 — the nerd LaunchAgent's ProgramArguments must pass
    `--no-warm` as a THIRD argument (after the script path), so new installs
    get a lazy daemon that does not load its ~2.8GB model at login (Task 1
    already added `--no-warm` support to bubble_shield_nerd.py's main()).
    RunAtLoad stays true — only the WHEN of model warm changes."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    py = home / "ml-env" / "bin" / "python"
    ml.install_launchagent(py)

    plist_text = ml.LAUNCH_PLIST.read_text(encoding="utf-8")
    import plistlib
    parsed = plistlib.loads(plist_text.encode("utf-8"))
    args = parsed["ProgramArguments"]
    assert "--no-warm" in args, \
        f"nerd plist ProgramArguments must include --no-warm; got {args}"
    stable_nerd = str(home / "daemon" / "scripts" / "bubble_shield_nerd.py")
    # --no-warm must come AFTER the script path (it's an arg TO the daemon).
    assert args.index("--no-warm") > args.index(stable_nerd), \
        "--no-warm must appear after the daemon script path in ProgramArguments"
    assert parsed["RunAtLoad"] is True, \
        "RunAtLoad must stay true — a modelless daemon at login is fine"


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


# --- #568 — Gemma daemon LaunchAgent (install_gemma_launchagent) -----------
# Mirrors the GLiNER nerd coverage above for the SEPARATE Gemma judge daemon
# (own label com.bubbleshield.gemmad, own plist, own stable-path copy step).


def test_install_gemma_daemon_to_stable_path_creates_layout(monkeypatch, tmp_path):
    """install_gemma_daemon_to_stable_path() copies bubble_shield_gemmad.py +
    its sibling gemma_classifier.py into the stable daemon root."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)

    stable_gemmad = ml.install_gemma_daemon_to_stable_path()

    daemon_root = home / "daemon"
    assert (daemon_root / "scripts" / "bubble_shield_gemmad.py").is_file(), \
        "stable gemma daemon script missing"
    assert (daemon_root / "scripts" / "gemma_classifier.py").is_file(), \
        "stable gemma_classifier.py sibling missing"
    assert stable_gemmad == daemon_root / "scripts" / "bubble_shield_gemmad.py"


def test_gemma_plist_program_arguments_under_stable_root(monkeypatch, tmp_path):
    """The written gemma plist's ProgramArguments daemon path is UNDER the
    stable daemon root, uses the correct label, and contains no ephemeral-cache
    markers."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    py = home / "gemma-env" / "bin" / "python"  # stable gemma venv python
    ml.install_gemma_launchagent(py)

    assert ml.GEMMA_LAUNCH_PLIST.is_file()
    plist_text = ml.GEMMA_LAUNCH_PLIST.read_text(encoding="utf-8")
    stable_gemmad = str(home / "daemon" / "scripts" / "bubble_shield_gemmad.py")
    assert stable_gemmad in plist_text, \
        "gemma plist must point at the stable daemon path"
    assert str(py) in plist_text, "gemma plist must invoke the gemma-env python"
    assert "com.bubbleshield.gemmad" in plist_text
    assert ".mcpb-cache" not in plist_text, \
        "gemma plist must not reference the ephemeral plugin cache"
    assert "local-agent-mode-sessions" not in plist_text, \
        "gemma plist must not reference a per-session Cowork dir"
    # Separate plist file from the GLiNER nerd LaunchAgent.
    assert ml.GEMMA_LAUNCH_PLIST != ml.LAUNCH_PLIST


def test_gemma_plist_program_arguments_include_no_warm_flag(monkeypatch, tmp_path):
    """#lazywarm2 — the gemma LaunchAgent's ProgramArguments must pass
    `--no-warm` as a THIRD argument (after the daemon script path), so new
    installs get a lazy gemmad that does not load the ~4GB MLX model at login
    (Task 1 added `--no-warm` support to bubble_shield_gemmad.py's main()).
    RunAtLoad stays true — only the WHEN of model warm changes."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    py = home / "gemma-env" / "bin" / "python"
    ml.install_gemma_launchagent(py)

    plist_text = ml.GEMMA_LAUNCH_PLIST.read_text(encoding="utf-8")
    import plistlib
    parsed = plistlib.loads(plist_text.encode("utf-8"))
    args = parsed["ProgramArguments"]
    assert "--no-warm" in args, \
        f"gemma plist ProgramArguments must include --no-warm; got {args}"
    stable_gemmad = str(home / "daemon" / "scripts" / "bubble_shield_gemmad.py")
    assert args.index("--no-warm") > args.index(stable_gemmad), \
        "--no-warm must appear after the daemon script path in ProgramArguments"
    assert parsed["RunAtLoad"] is True, \
        "RunAtLoad must stay true — a modelless daemon at login is fine"


def test_gemma_fallback_to_ephemeral_when_copy_fails(monkeypatch, tmp_path):
    """If the stable copy raises, install_gemma_launchagent falls back to the
    __file__ path and still writes the plist — setup never hard-fails."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    def _boom(*a, **k):  # noqa: ANN001
        raise OSError("disk full / permission denied (synthetic)")

    monkeypatch.setattr(ml.shutil, "copy2", _boom)

    py = home / "gemma-env" / "bin" / "python"
    ml.install_gemma_launchagent(py)  # must not raise

    assert ml.GEMMA_LAUNCH_PLIST.is_file(), "plist must still be written on fallback"
    plist_text = ml.GEMMA_LAUNCH_PLIST.read_text(encoding="utf-8")
    fallback_gemmad = str(Path(ml.__file__).resolve().parent / "bubble_shield_gemmad.py")
    assert fallback_gemmad in plist_text, \
        "fallback must write the __file__-relative gemma daemon path into the plist"


# --- #568 final review must-fix — honest no-egress (HF_HUB_OFFLINE at runtime) --
# The reviewer flagged: install_gemma_env() only pip-installs mlx-lm, it never
# pre-downloads the Gemma model snapshot, so the model fetches from HuggingFace
# at first warm_up() (login) — contradicting bubble_shield_gemmad.py's own
# "(on-device, no egress)" claim and the README's absolute no-network claim.
# Fix: pre-download at install time (mirrors GLiNER's download_model()) AND set
# HF_HUB_OFFLINE=1 in the gemma daemon's LaunchAgent env, so RUNTIME serving is
# genuinely zero-egress regardless of what happened at install.


def test_gemma_plist_sets_hf_hub_offline(monkeypatch, tmp_path):
    """The gemma daemon LaunchAgent's EnvironmentVariables must include
    HF_HUB_OFFLINE=1 so the warm daemon (which calls mlx_lm.load() in-process)
    can NEVER reach HuggingFace at runtime — the model must already be local
    from install_gemma_env(). This is what makes the "(on-device, no egress)"
    claim in bubble_shield_gemmad.py's docstring actually true."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    _mock_launchctl(monkeypatch, ml)

    py = home / "gemma-env" / "bin" / "python"
    ml.install_gemma_launchagent(py)

    plist_text = ml.GEMMA_LAUNCH_PLIST.read_text(encoding="utf-8")
    assert "HF_HUB_OFFLINE" in plist_text, \
        "gemma plist must set HF_HUB_OFFLINE so the daemon never reaches HF at runtime"
    # Must be set to "1" (truthy for huggingface_hub's offline check), inside the
    # EnvironmentVariables dict alongside BUBBLE_SHIELD_HOME.
    import plistlib
    parsed = plistlib.loads(plist_text.encode("utf-8"))
    env = parsed.get("EnvironmentVariables", {})
    assert env.get("HF_HUB_OFFLINE") == "1", \
        f"HF_HUB_OFFLINE must be '1' in EnvironmentVariables, got {env.get('HF_HUB_OFFLINE')!r}"


def test_install_gemma_env_predownloads_model(monkeypatch, tmp_path):
    """install_gemma_env() must attempt to pre-download the Gemma model
    snapshot (mirrors the GLiNER path's download_model()) so it's present on
    disk BEFORE the daemon's first warm_up() — never a first-login HF fetch.

    We don't run a real download (no network in CI): subprocess.run is mocked
    and we assert install_gemma_env() issues a pip-install call for mlx-lm/
    wordfreq AND a SEPARATE call that references the Gemma MODEL_ID (the
    pre-download step), i.e. strictly more than one subprocess invocation."""
    home = tmp_path / "home"
    ml = _reload_under_home(monkeypatch, home)
    (home / "gemma-env" / "bin").mkdir(parents=True, exist_ok=True)
    (home / "gemma-env" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, *a, **k):  # noqa: ANN001
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(ml.subprocess, "run", _fake_run)
    # The gemma-env python bin already exists above, so the venv-create branch
    # is skipped entirely (install_gemma_env goes straight to pip-install +
    # pre-download). Since #venv-py312 the module no longer imports the `venv`
    # module — it shells out to `<python3.12> -m venv` via subprocess (already
    # mocked above), so there is nothing extra to stub here.

    ml.install_gemma_env()

    joined = " ".join(" ".join(map(str, c)) if isinstance(c, (list, tuple)) else str(c)
                       for c in calls)
    assert "mlx-lm" in joined and "wordfreq" in joined, \
        "install_gemma_env must still pip-install mlx-lm + wordfreq"
    GEMMA_MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"
    assert GEMMA_MODEL_ID in joined, \
        ("install_gemma_env must pre-download the Gemma model "
         f"({GEMMA_MODEL_ID}) at install time, not defer to first warm_up()")
    assert len(calls) >= 2, \
        "expected at least a pip-install call AND a separate model pre-download call"
