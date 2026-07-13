"""
bubble_shield/coverage_state.py — a small, FDA-free coverage snapshot the desktop
app can read.

WHY THIS EXISTS (2026-07-13)
----------------------------
The dashboard's coverage panel used to DISCOVER protected folders by scanning the
disk (Documents / Desktop / CloudStorage for `.bubble-shield.json` markers). But
the desktop app runs through Apple's shared Python (`com.apple.python3`), so macOS
attributes disk access to Python, not to "Bubble Shield.app" — granting Full Disk
Access to the app does NOT reach the reader, and the scan hits PermissionError on
CloudStorage (where Dropbox lives). Result: a Dropbox/iCloud user sees an empty
panel even with folders marked, and there is no clean FDA toggle to fix it.

The fix: the BACKGROUND SWEEP (a launchd agent — a separate process whose disk
access can be granted independently, and which already reads the folders to index
them) writes a tiny JSON snapshot here after each run. The panel READS that
snapshot instead of re-scanning the disk. The snapshot lives in
`~/.bubble_shield/` — a folder the app can always read WITHOUT Full Disk Access
(it's inside the app's own home). So the panel is FDA-independent AND always
reflects what the sweep actually indexed.

The file holds ONLY paths + counts — never file contents, never PII. Best-effort
throughout: a missing / unreadable / malformed snapshot degrades to None so the
panel falls back to a live scan (correct on a dev/CLI box with disk access) rather
than crashing.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


STATE_FILENAME = "coverage_state.json"
# Bump if the on-disk shape changes so an old reader ignores a new file safely.
STATE_VERSION = 1


def _shield_home() -> Path:
    """Resolve ~/.bubble_shield (honouring BUBBLE_SHIELD_HOME) — mirrors
    shadow_store._shield_home() exactly so the snapshot sits beside the store."""
    return Path(os.environ.get(
        "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))


def _state_path() -> Path:
    return _shield_home() / STATE_FILENAME


def write_state(roots: list, blocked: Optional[list] = None) -> bool:
    """Persist the sweep's coverage snapshot. `roots` is a list of dicts:
    {"root": str, "total": int, "indexed": int, "pct": float, "pending": int}.
    `blocked` is the list of paths the sweep couldn't read (rare — the sweep
    usually HAS access; kept for symmetry with the live path).

    Writes atomically (temp + replace) so the panel never reads a half-written
    file. Returns True on success, False on any failure (never raises — a failed
    snapshot must not break the sweep)."""
    try:
        home = _shield_home()
        home.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": time.time(),
            "roots": roots,
            "blocked_paths": blocked or [],
        }
        path = _state_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            tmp.chmod(0o600)  # paths only, but keep it private by default
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read_state() -> Optional[dict]:
    """Return the last sweep snapshot as {"roots": [...], "blocked_paths": [...],
    "updated_at": float} — or None if there is no usable snapshot (missing file,
    unreadable, malformed, or a version the reader doesn't understand). The panel
    treats None as 'fall back to a live scan'.

    Never raises."""
    try:
        path = _state_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("version") != STATE_VERSION:
            return None
        roots = data.get("roots")
        if not isinstance(roots, list):
            return None
        return {
            "roots": roots,
            "blocked_paths": data.get("blocked_paths") or [],
            "updated_at": data.get("updated_at"),
        }
    except Exception:
        return None
