"""test_383_uninstall.py — uninstall removes LOCAL footprint, NEVER wipes SHARED config.

Card #383. The uninstall mirrors install_user_hooks.py and reverses exactly what it
creates, idempotently and fail-safe. The NON-NEGOTIABLE rule (Joris, 2026-06-29): a
single client's uninstall must NEVER delete the shared (Dropbox) cabinet config — that
would wipe config for all 5 CGPs. So the uninstall touches ONLY local/per-machine paths
and explicitly SKIPS any `.bubble-shield.json`-marked shared folder.

These tests run against a simulated install under a tmp HOME. They NEVER touch the real
~/.claude, ~/.bubble_shield, or any real Dropbox folder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Import the uninstall module under test, and reuse install's MARKER (the SAME marker
# the uninstall must match by — proving the reuse, not a re-derivation).
import importlib.util

REPO = Path(__file__).resolve().parent.parent
UNINSTALL_PY = REPO / "plugin" / "bubble-shield" / "scripts" / "uninstall_user_hooks.py"
INSTALL_PY = REPO / "plugin" / "bubble-shield" / "scripts" / "install_user_hooks.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shield_hook(cmd_kind: str) -> dict:
    """A settings.json hook entry that looks like one Bubble Shield installs:
    carries the MARKER and references the script kind (e.g. guard.py)."""
    return {
        "matcher": "Read|Edit|Write|Bash",
        "hooks": [{
            "type": "command",
            "command": f"[ -f X ] && python3 /x/{cmd_kind} || exit 0  # bubble-shield:{cmd_kind}",
        }],
    }


def _unrelated_hook() -> dict:
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo my-own-unrelated-hook"}],
    }


def _make_install(home: Path):
    """Build a simulated install footprint under `home`. Returns the uninstall module
    loaded with HOME pointed at this tmp home (so its module-level paths resolve here)."""
    claude = home / ".claude"
    (claude).mkdir(parents=True, exist_ok=True)

    # settings.json: 2 shield hooks (guard + posttool) in different arrays + 1 UNRELATED hook.
    settings = {
        "model": "opus",
        "hooks": {
            "PreToolUse": [_shield_hook("guard.py"), _unrelated_hook()],
            "PostToolUse": [_shield_hook("posttool_anonymize.py")],
            "UserPromptSubmit": [_shield_hook("tripwire.py")],
            "SessionStart": [_shield_hook("rearm-daemon")],
        },
    }
    (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # STABLE_DIR
    stable = claude / "bubble-shield"
    stable.mkdir(parents=True, exist_ok=True)
    (stable / "guard.py").write_text("# guard", encoding="utf-8")

    # LaunchAgent plist
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True, exist_ok=True)
    (la / "com.bubbleinvest.bubble-shield-nerd.plist").write_text("<plist/>", encoding="utf-8")

    # plugin cache
    cache = claude / "plugins" / "cache" / "bubble-shield"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "x").write_text("cache", encoding="utf-8")

    # ~/.config/bubble_shield
    cfg = home / ".config" / "bubble_shield"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "bubble-shield.json").write_text("{}", encoding="utf-8")

    # ~/.bubble_shield local DATA dir (vaults + models)
    data = home / ".bubble_shield"
    (data / "gazetteer").mkdir(parents=True, exist_ok=True)
    (data / "vault.json").write_text('{"vault":1}', encoding="utf-8")

    # desktop app + app bundle + old .command
    appdir = home / ".bubble_shield_app"
    appdir.mkdir(parents=True, exist_ok=True)
    (appdir / "x").write_text("app", encoding="utf-8")
    desktop = home / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    appbundle = desktop / "Bubble Shield.app"
    appbundle.mkdir(parents=True, exist_ok=True)
    (appbundle / "Contents").mkdir(parents=True, exist_ok=True)
    (desktop / "Bubble Shield.command").write_text("#!/bin/bash", encoding="utf-8")


@pytest.fixture
def uninst(monkeypatch, tmp_path):
    """Load the uninstall module with HOME pointed at a fresh tmp home that already
    contains a simulated install."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # The autouse #382 fixture sets BUBBLE_SHIELD_HOME to its own tmp dir; for these
    # tests the local data dir is HOME/.bubble_shield, so clear that override so the
    # uninstall's default resolution targets HOME/.bubble_shield under our tmp home.
    monkeypatch.delenv("BUBBLE_SHIELD_HOME", raising=False)
    _make_install(home)
    mod = _load("uninstall_user_hooks_under_test", UNINSTALL_PY)
    return mod, home


def test_marker_is_reused_from_install(uninst):
    """The uninstall must reuse install's MARKER, not re-derive it."""
    mod, _ = uninst
    install = _load("install_user_hooks_marker_check", INSTALL_PY)
    assert mod.MARKER == install.MARKER == "bubble-shield"


def test_shield_hooks_removed_unrelated_preserved(uninst):
    """Default uninstall: shield hooks GONE, the UNRELATED hook PRESERVED, the rest of
    settings.json intact."""
    mod, home = uninst
    mod.uninstall(purge_data=False)

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    # non-hook keys preserved exactly
    assert settings["model"] == "opus"
    hooks = settings["hooks"]
    # PreToolUse: shield guard gone, unrelated kept
    pre = hooks["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["hooks"][0]["command"] == "echo my-own-unrelated-hook"
    # PostToolUse / UserPromptSubmit / SessionStart: shield-only arrays now empty
    assert hooks["PostToolUse"] == []
    assert hooks["UserPromptSubmit"] == []
    assert hooks["SessionStart"] == []


def test_local_artifacts_removed(uninst):
    """STABLE_DIR, LaunchAgent plist, plugin cache, ~/.config/bubble_shield removed."""
    mod, home = uninst
    mod.uninstall(purge_data=False)
    assert not (home / ".claude" / "bubble-shield").exists()
    assert not (home / "Library" / "LaunchAgents" / "com.bubbleinvest.bubble-shield-nerd.plist").exists()
    assert not (home / ".claude" / "plugins" / "cache" / "bubble-shield").exists()
    assert not (home / ".config" / "bubble_shield").exists()


def test_data_dir_kept_without_purge(uninst):
    """Without --purge-data, ~/.bubble_shield (vaults!) is KEPT."""
    mod, home = uninst
    mod.uninstall(purge_data=False)
    data = home / ".bubble_shield"
    assert data.exists()
    assert (data / "vault.json").exists()


def test_data_dir_removed_with_purge(uninst):
    """With --purge-data, the LOCAL ~/.bubble_shield is removed."""
    mod, home = uninst
    mod.uninstall(purge_data=True)
    assert not (home / ".bubble_shield").exists()


def test_app_artifacts_removed(uninst):
    """Desktop-app footprint removed: ~/.bubble_shield_app, the .app bundle, old .command."""
    mod, home = uninst
    mod.uninstall(purge_data=False)
    assert not (home / ".bubble_shield_app").exists()
    assert not (home / "Desktop" / "Bubble Shield.app").exists()
    assert not (home / "Desktop" / "Bubble Shield.command").exists()


def test_idempotent_rerun(uninst):
    """Running uninstall twice is a clean no-op the second time (no exception)."""
    mod, home = uninst
    mod.uninstall(purge_data=True)
    mod.uninstall(purge_data=True)  # must not raise


def test_settings_with_only_unrelated_hooks_is_noop(monkeypatch, tmp_path):
    """A settings.json with ONLY unrelated hooks → uninstall leaves it byte-identical."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BUBBLE_SHIELD_HOME", raising=False)
    settings = {"hooks": {"PreToolUse": [_unrelated_hook()]}, "other": "kept"}
    sp = home / ".claude" / "settings.json"
    original = json.dumps(settings, indent=2)
    sp.write_text(original, encoding="utf-8")

    mod = _load("uninstall_noop_check", UNINSTALL_PY)
    mod.uninstall(purge_data=False)

    assert sp.read_text(encoding="utf-8") == original  # byte-identical, untouched


def test_missing_settings_is_failsafe_noop(monkeypatch, tmp_path):
    """No settings.json at all → uninstall is a clean no-op (no crash, no file created)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BUBBLE_SHIELD_HOME", raising=False)
    mod = _load("uninstall_missing_settings", UNINSTALL_PY)
    mod.uninstall(purge_data=False)  # must not raise
    assert not (home / ".claude" / "settings.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# THE CRITICAL TEST — a shared (Dropbox-marked) config folder must SURVIVE,
# even with --purge-data. (Joris, 2026-06-29: a single client's uninstall must
# NEVER wipe the shared cabinet config for all 5 CGPs.)
# ─────────────────────────────────────────────────────────────────────────────
def test_shared_dropbox_config_survives_even_with_purge(monkeypatch, tmp_path):
    mod_install = _load("install_for_shared_test", INSTALL_PY)  # noqa: F841 (sanity import)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BUBBLE_SHIELD_HOME", raising=False)
    _make_install(home)

    # A fake Dropbox shared-config folder, OUTSIDE the local paths, carrying the
    # cabinet marker .bubble-shield.json + a known file representing shared config.
    dropbox = tmp_path / "Dropbox" / "Bubble Shield Cabinet"
    dropbox.mkdir(parents=True, exist_ok=True)
    (dropbox / ".bubble-shield.json").write_text('{"cabinet":"bubble-cgp"}', encoding="utf-8")
    shared_file = dropbox / "shared_gazetteer.json"
    shared_file.write_text('{"shared":"DO NOT WIPE — config for all 5 CGPs"}', encoding="utf-8")

    # Point the env override at the shared folder (the #352 shared-config path) and run
    # uninstall with the most destructive flag.
    monkeypatch.setenv("BUBBLE_SHIELD_SHARED_CONFIG", str(dropbox))
    mod = _load("uninstall_shared_safety", UNINSTALL_PY)
    skipped = mod.uninstall(purge_data=True)

    # The shared Dropbox folder AND its marker AND its config file all SURVIVE.
    assert dropbox.exists(), "shared Dropbox folder was deleted — THIS WIPES ALL 5 CGPs"
    assert (dropbox / ".bubble-shield.json").exists(), "shared cabinet marker deleted"
    assert shared_file.exists(), "shared config file deleted"
    assert shared_file.read_text(encoding="utf-8") == '{"shared":"DO NOT WIPE — config for all 5 CGPs"}'

    # The uninstall reports that it explicitly skipped the shared path.
    assert str(dropbox) in skipped, "uninstall did not record skipping the shared path"

    # And the LOCAL footprint was still removed (purge ran on local only).
    assert not (home / ".claude" / "bubble-shield").exists()
    assert not (home / ".bubble_shield").exists()  # local data purged
    assert not (home / ".config" / "bubble_shield").exists()
