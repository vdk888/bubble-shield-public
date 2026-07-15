"""
tests/test_host_guard_refresh.py — host last-mile guard refresh (2026-07-15).

Incident: v1.23.27 shipped a guard fix to dev/public/the app checkout, but the
guard the hook ACTUALLY runs lives at STABLE_DIR (~/.claude/bubble-shield/guard.py)
and was never refreshed on the host Mac — the self-installer only copied scripts
inside the Cowork VM. So the fix never reached the running guard; the user kept
getting the old flaky "erreur interne" blocks.

Fix: on a host (non-Cowork) SessionStart, IF the guard is already armed in the
host settings.json (user opted in), refresh the STABLE_DIR scripts from the
current plugin — WITHOUT writing settings.json. Safety-critical guarantee: a host
that never armed the guard must be left COMPLETELY untouched (zero footprint).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(monkeypatch, tmp_path, *, plugin_root=None):
    """Import install_user_hooks with STABLE_DIR + settings redirected to tmp, and
    a fake plugin root holding a NEW guard.py to copy from."""
    import install_user_hooks as ih
    importlib.reload(ih)

    stable = tmp_path / "stable"
    settings = tmp_path / "settings.json"

    monkeypatch.setattr(ih, "STABLE_DIR", stable)
    monkeypatch.setattr(ih, "_user_settings_path", lambda: settings)
    # Never let the test think it's in Cowork.
    monkeypatch.setattr(ih, "_in_cowork_vm", lambda: False)

    # Fake plugin root with a NEW guard.py (the "fixed" version to propagate).
    if plugin_root is None:
        plugin_root = tmp_path / "plugin"
        (plugin_root / "scripts").mkdir(parents=True)
        (plugin_root / "scripts" / "guard.py").write_text("# NEW FIXED GUARD v2\n")
        (plugin_root / "scripts" / "tripwire.py").write_text("# tripwire\n")
    monkeypatch.setattr(ih, "PLUGIN_ROOT", str(plugin_root))
    return ih, stable, settings


def _arm(settings: Path, cmd="[ -f '/x/guard.py' ] && python3 x  # bubble-shield:guard.py"):
    settings.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [
            {"type": "command", "command": cmd}]}]}
    }))


# ── the safety-critical guarantee: un-armed host = zero footprint ─────────────

def test_unarmed_host_is_left_untouched(monkeypatch, tmp_path):
    """A host that NEVER armed the guard must get NO STABLE_DIR, no scripts —
    exactly the pre-fix zero-footprint behaviour. This is the spill guard."""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    # no settings.json at all → definitely not armed
    ih._refresh_stable_scripts_if_armed()
    assert not stable.exists(), "must not create STABLE_DIR on an un-armed host"


def test_settings_present_but_guard_not_armed_is_untouched(monkeypatch, tmp_path):
    """settings.json exists but has NO guard entry (user uses Claude but never
    installed Shield) → still zero footprint."""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo other"}]}]}}))
    ih._refresh_stable_scripts_if_armed()
    assert not stable.exists(), "a non-Shield settings.json must not trigger refresh"


# ── the fix: armed host gets the new guard on update ──────────────────────────

def test_armed_host_refreshes_stable_guard(monkeypatch, tmp_path):
    """The incident case: guard armed on host, STABLE_DIR holds OLD guard, an
    update runs the installer → STABLE_DIR/guard.py becomes the NEW one."""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    _arm(settings)
    # simulate a pre-existing STABLE_DIR with the OLD (flaky) guard
    stable.mkdir(parents=True)
    (stable / "guard.py").write_text("# OLD FLAKY GUARD v1\n")

    ih._refresh_stable_scripts_if_armed()

    assert (stable / "guard.py").read_text() == "# NEW FIXED GUARD v2\n", \
        "armed host must receive the current plugin's guard.py on refresh"


def test_armed_detection_matches_fix3_module_import_cmd(monkeypatch, tmp_path):
    """FIX3 changed the guard command to `python3 -c 'import guard; guard.main()'`
    — the arm-detection must still recognise it (it keeps the `[ -f .../guard.py ]`
    existence check, so 'guard.py' is still in the command)."""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    fix3_cmd = ("[ -f '/x/guard.py' ] && CLAUDE_PLUGIN_ROOT='/x' "
                "python3 -c \"import sys; sys.path.insert(0,'/x'); import guard; guard.main()\" "
                "|| exit 0  # bubble-shield:guard.py")
    _arm(settings, cmd=fix3_cmd)
    assert ih._guard_armed_in_host_settings() is True


def test_refresh_never_writes_settings(monkeypatch, tmp_path):
    """The refresh path must NEVER modify settings.json — it only copies scripts.
    (Arming settings.json stays Cowork-only.)"""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    _arm(settings)
    before = settings.read_text()
    ih._refresh_stable_scripts_if_armed()
    assert settings.read_text() == before, "refresh must not touch settings.json"


def test_main_on_host_calls_refresh_and_exits(monkeypatch, tmp_path):
    """End-to-end: main() on a host (not Cowork) with an armed guard refreshes
    STABLE_DIR then exits 0, WITHOUT arming/writing settings.json."""
    ih, stable, settings = _load(monkeypatch, tmp_path)
    _arm(settings)
    stable.mkdir(parents=True)
    (stable / "guard.py").write_text("# OLD\n")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(""))
    before = settings.read_text()
    with pytest.raises(SystemExit) as e:
        ih.main()
    assert e.value.code == 0
    assert (stable / "guard.py").read_text() == "# NEW FIXED GUARD v2\n"
    assert settings.read_text() == before, "host main() must not write settings.json"
