"""
tests/test_644_daemon_refresh.py — host update refreshes the DAEMON stable dir (#644).

The launchd nerd/gemmad run from ~/.bubble_shield/daemon/{scripts,vendor}, copied ONCE
at ML-pack setup and NEVER refreshed on update (verified live 2026-07-15: daemon vendor
was 3 days stale). Same 'repo ≠ running code' class as the guard host-refresh (v1.23.28),
different location. Fix: the armed host-refresh ALSO re-copies the daemon dir + kickstarts
the daemons — GATED on the daemon dir existing (no ML pack → untouched), and only
kickstarts when code ACTUALLY changed.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(monkeypatch, tmp_path, *, with_daemon_dir: bool, plugin_engine="# NEW ENGINE\n"):
    import install_user_hooks as ih
    importlib.reload(ih)

    # fake plugin source
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    for n in ("bubble_shield_nerd.py", "bubble_shield_gemmad.py",
              "gemma_classifier.py", "bubble_shield_setup_ml.py"):
        (plugin / "scripts" / n).write_text(f"# {n} NEW\n")
    (plugin / "vendor" / "bubble_shield").mkdir(parents=True)
    (plugin / "vendor" / "bubble_shield" / "engine.py").write_text(plugin_engine)
    monkeypatch.setattr(ih, "PLUGIN_ROOT", str(plugin))

    # daemon stable dir (maybe pre-existing with OLD content)
    daemon = tmp_path / "home" / "daemon"
    monkeypatch.setattr(ih, "_DAEMON_STABLE_ROOT", daemon)
    if with_daemon_dir:
        (daemon / "scripts").mkdir(parents=True)
        (daemon / "scripts" / "bubble_shield_nerd.py").write_text("# nerd OLD\n")
        (daemon / "vendor" / "bubble_shield").mkdir(parents=True)
        (daemon / "vendor" / "bubble_shield" / "engine.py").write_text("# OLD ENGINE\n")

    kicks = []
    monkeypatch.setattr(ih, "_kickstart_daemons", lambda: kicks.append(True))
    return ih, daemon, kicks


def test_no_daemon_dir_is_untouched(monkeypatch, tmp_path):
    """A host without the ML pack (no daemon dir) → zero footprint, no kickstart."""
    ih, daemon, kicks = _load(monkeypatch, tmp_path, with_daemon_dir=False)
    ih._refresh_daemon_stable_dir_if_present()
    assert not daemon.exists(), "must not create the daemon dir on a host without the ML pack"
    assert kicks == [], "must not kickstart daemons that don't exist"


def test_stale_daemon_is_refreshed_and_kickstarted(monkeypatch, tmp_path):
    """The incident: daemon dir exists with OLD code → refresh to NEW + kickstart."""
    ih, daemon, kicks = _load(monkeypatch, tmp_path, with_daemon_dir=True)
    ih._refresh_daemon_stable_dir_if_present()
    assert (daemon / "scripts" / "bubble_shield_nerd.py").read_text() == "# bubble_shield_nerd.py NEW\n"
    assert (daemon / "vendor" / "bubble_shield" / "engine.py").read_text() == "# NEW ENGINE\n"
    assert (daemon / "scripts" / "gemma_classifier.py").is_file(), "the new daemon file is added"
    assert kicks == [True], "a real code change must kickstart the daemons"


def test_unchanged_daemon_is_NOT_kickstarted(monkeypatch, tmp_path):
    """If the daemon already has the current code, DON'T churn it every SessionStart."""
    # Pre-seed the daemon dir with the SAME content the plugin has.
    ih, daemon, kicks = _load(monkeypatch, tmp_path, with_daemon_dir=True)
    # First refresh makes it current + kickstarts once.
    ih._refresh_daemon_stable_dir_if_present()
    kicks.clear()
    # Second refresh: nothing changed → no kickstart.
    ih._refresh_daemon_stable_dir_if_present()
    assert kicks == [], "an unchanged daemon must NOT be kickstarted (no churn)"


def test_refresh_gated_on_armed(monkeypatch, tmp_path):
    """The top-level _refresh_stable_scripts_if_armed calls the daemon refresh only
    when the guard is armed (unarmed host → the whole thing is a no-op)."""
    ih, daemon, kicks = _load(monkeypatch, tmp_path, with_daemon_dir=True)
    monkeypatch.setattr(ih, "_guard_armed_in_host_settings", lambda: False)
    called = []
    monkeypatch.setattr(ih, "_refresh_daemon_stable_dir_if_present",
                        lambda: called.append(True))
    monkeypatch.setattr(ih, "_install_scripts", lambda: None)
    ih._refresh_stable_scripts_if_armed()
    assert called == [], "unarmed host → daemon refresh must not run"
