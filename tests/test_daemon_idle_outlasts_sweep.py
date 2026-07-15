"""
tests/test_daemon_idle_outlasts_sweep.py — #561-B (2026-07-15).

Regression: the NER + Gemma daemons' idle-shutdown default had drifted back to
600s while the sweep fires every 1200s (StartInterval). So the daemon warmed for
sweep N had ALWAYS idle-shut-down before sweep N+1 → every sweep hit a cold daemon
→ a doc needing that model (any structured form: liasse/CERFA) fail-closed EVERY
sweep, stayed pending forever, and the sweep re-warmed ~4GB every 20 min to retry
one file that could never complete (observed live: 29/30 indexed, 1 liasse stuck).

Invariant this locks in: the daemon idle-shutdown DEFAULT must be strictly greater
than the sweep interval, so a daemon warmed for one sweep is still alive at the
next. (An env override can still tune it per-deployment.)
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

# The sweep's default interval (StartInterval in the plist template / docs).
SWEEP_INTERVAL_S = 1200


def _reload(mod_name, monkeypatch, env=None):
    for k in ("BUBBLE_SHIELD_NERD_IDLE", "BUBBLE_SHIELD_GEMMA_IDLE"):
        monkeypatch.delenv(k, raising=False)
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    mod = importlib.import_module(mod_name)
    return importlib.reload(mod)


def test_nerd_idle_default_outlasts_sweep_interval(monkeypatch):
    nerd = _reload("bubble_shield_nerd", monkeypatch)
    assert nerd.IDLE_SECS > SWEEP_INTERVAL_S, (
        f"nerd IDLE_SECS default ({nerd.IDLE_SECS}s) must exceed the sweep interval "
        f"({SWEEP_INTERVAL_S}s) or the daemon is always cold when the sweep runs")


def test_gemmad_idle_default_outlasts_sweep_interval(monkeypatch):
    g = _reload("bubble_shield_gemmad", monkeypatch)
    assert g.IDLE_SECS > SWEEP_INTERVAL_S, (
        f"gemmad IDLE_SECS default ({g.IDLE_SECS}s) must exceed the sweep interval "
        f"({SWEEP_INTERVAL_S}s)")


def test_idle_env_override_still_honoured(monkeypatch):
    """The 4h default must NOT hard-code away the env knob — a client can still
    set an always-warm (0) or custom idle."""
    nerd = _reload("bubble_shield_nerd", monkeypatch,
                   env={"BUBBLE_SHIELD_NERD_IDLE": "0"})
    assert nerd.IDLE_SECS == 0
    g = _reload("bubble_shield_gemmad", monkeypatch,
                env={"BUBBLE_SHIELD_GEMMA_IDLE": "7200"})
    assert g.IDLE_SECS == 7200


def test_defaults_are_the_documented_4h(monkeypatch):
    """Pin the exact default so the doc/code mismatch (comment said 4h, literal
    was 600s) can't silently reappear."""
    nerd = _reload("bubble_shield_nerd", monkeypatch)
    g = _reload("bubble_shield_gemmad", monkeypatch)
    assert nerd.IDLE_SECS == 14400, "nerd default must be 4h (#561-B)"
    assert g.IDLE_SECS == 14400, "gemmad default must be 4h (#561-B)"
