#!/usr/bin/env python3
"""Bubble Shield — uninstaller. Reverses exactly what install_user_hooks.py creates.

WHY THIS EXISTS (card #383, proven on Joris's Mac 2026-06-29)
-------------------------------------------------------------
`/plugin uninstall` removes ONLY the marketplace entry. It LEAVES the entire
host-native footprint live: the PreToolUse/PostToolUse/UserPromptSubmit/SessionStart
hooks in ~/.claude/settings.json (still intercepting EVERY tool call), the host
scripts in ~/.claude/bubble-shield/, the LaunchAgent
~/Library/LaunchAgents/com.bubbleinvest.bubble-shield-nerd.plist, the plugin cache,
~/.config/bubble_shield/, and the multi-GB ~/.bubble_shield/ data dir (reversible PII
vaults + ONNX models). For a tool whose whole point is a host-wide install, an
uninstall that leaves the interception hooks armed is a real defect.

This script removes the LOCAL footprint cleanly, idempotently, fail-safe.

THE NON-NEGOTIABLE RULE (Joris, 2026-06-29) — never wipe SHARED config
----------------------------------------------------------------------
When #352 ships, the gazetteer / custom-fields / policy are shared via a Dropbox
folder carrying a `.bubble-shield.json` cabinet marker, owned by the cabinet (5 CGPs).
A SINGLE client's uninstall MUST NOT delete that shared store — it would wipe config
for EVERYONE. So:

  * The uninstall touches ONLY local/per-machine paths (under ~/.claude, ~/.config,
    ~/Library, ~/.bubble_shield, ~/.bubble_shield_app, ~/Desktop).
  * It NEVER deletes or descends into any Dropbox / shared-marked folder. If it
    detects a configured shared-config path (a `.bubble-shield.json`-marked folder, or
    an env override pointing at one), it explicitly SKIPS it and says so.
  * VAULTS are always LOCAL — even --purge-data only removes the local ~/.bubble_shield,
    NEVER a shared store. Losing a local vault only costs THIS machine's decloak
    ability; it never affects another CGP.

Usage:
    python3 uninstall_user_hooks.py            # remove local footprint, KEEP ~/.bubble_shield
    python3 uninstall_user_hooks.py --purge-data   # also remove LOCAL ~/.bubble_shield (vaults!)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Reuse install's MARKER + entry-matcher (don't re-derive them) ─────────────
# The install side owns the definition of "what is one of our hooks". We import it
# so the uninstall recognises EXACTLY the same entries the installer wrote — match
# by the SAME MARKER, never a parallel guess that could drift.
_INSTALL_PATH = Path(__file__).resolve().parent / "install_user_hooks.py"
_spec = importlib.util.spec_from_file_location("_bubble_shield_install", _INSTALL_PATH)
_install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_install)

MARKER = _install.MARKER  # "bubble-shield" — the single source of truth
_entry_is_bubble_shield = _install._entry_is_bubble_shield


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


# ── LOCAL, per-machine paths — the ONLY things this uninstall may remove ──────
def _settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def _stable_dir() -> Path:
    return _home() / ".claude" / "bubble-shield"


# All host LaunchAgents Bubble Shield installs. `nerd` is the original GLiNER
# daemon; `gemmad` (the Gemma de-pollution/second-pass judge) and `sweep` (the
# shadow-index background indexer) were added later — every one must be removed
# on uninstall or its plist keeps launchd respawning a daemon after the plugin
# is gone. Keep this list in sync with what the installers register.
LAUNCH_LABELS = (
    "com.bubbleinvest.bubble-shield-nerd",
    "com.bubbleshield.gemmad",
    "com.bubbleinvest.bubble-shield-sweep",
)
# Back-compat: some callers/tests reference the original single label.
LAUNCH_LABEL = LAUNCH_LABELS[0]


def _launch_plists() -> list[Path]:
    la = _home() / "Library" / "LaunchAgents"
    return [la / f"{label}.plist" for label in LAUNCH_LABELS]


def _launch_plist() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _plugin_cache() -> Path:
    return _home() / ".claude" / "plugins" / "cache" / "bubble-shield"


def _config_dir() -> Path:
    return _home() / ".config" / "bubble_shield"


def _data_dir() -> Path:
    """The LOCAL data dir (vaults + gazetteer + models). Honours BUBBLE_SHIELD_HOME
    so it matches where the engine actually wrote — but ONLY when that override still
    points at a LOCAL path (see _is_shared_path). A shared/Dropbox override is refused."""
    override = os.environ.get("BUBBLE_SHIELD_HOME")
    if override:
        return Path(override)
    return _home() / ".bubble_shield"


def _app_dir() -> Path:
    return Path(os.environ.get("BUBBLE_SHIELD_APP_DIR") or (_home() / ".bubble_shield_app"))


def _desktop() -> Path:
    return _home() / "Desktop"


# ── SHARED-CONFIG SAFETY ──────────────────────────────────────────────────────
# A path is SHARED (and therefore off-limits) if it carries the cabinet marker
# `.bubble-shield.json`, or if any ancestor up to HOME carries it, or if it lives
# under a folder that looks like a Dropbox / shared cloud root. We refuse to delete
# or descend into any such path — at most we un-link the local machine from it.
SHARED_MARKER = ".bubble-shield.json"
_SHARED_ROOT_HINTS = ("Dropbox", "Google Drive", "OneDrive", "iCloud Drive", "Box")


def _is_shared_path(path: Path) -> bool:
    """True if `path` is (or lives under) a shared cabinet store we must NOT touch.

    Detection, in order:
      1. the path itself or any ancestor (up to and including the user's HOME) carries
         the `.bubble-shield.json` cabinet marker → it's the shared cabinet store;
      2. any path component matches a known cloud-sync root name (Dropbox, etc.).
    Resolved with .expanduser()/.resolve() so symlinked Dropbox folders are caught.
    """
    try:
        p = path.expanduser().resolve()
    except Exception:
        # If we can't even resolve it, be conservative and treat it as shared (skip).
        return True

    home = _home().expanduser().resolve()

    # 1) marker on the path or any ancestor (stop at HOME — local paths under HOME
    #    never carry the cabinet marker; the shared store lives OUTSIDE the local tree).
    for cand in (p, *p.parents):
        try:
            if (cand / SHARED_MARKER).is_file():
                return True
        except Exception:
            pass
        if cand == home or cand == cand.parent:  # reached HOME or filesystem root
            break

    # 2) a recognisable cloud-sync root anywhere in the path components.
    parts = set(p.parts)
    if any(hint in parts for hint in _SHARED_ROOT_HINTS):
        return True

    return False


def _rm_tree(path: Path, log: list[str]) -> None:
    """Remove a directory tree — but ONLY if it is a LOCAL path. Refuses anything
    shared (Dropbox-marked). Idempotent: missing path is a silent no-op."""
    if _is_shared_path(path):
        log.append(f"  SKIP (shared cabinet config, never deleted): {path}")
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        log.append(f"  removed dir: {path}")


def _rm_file(path: Path, log: list[str]) -> None:
    if _is_shared_path(path):
        log.append(f"  SKIP (shared cabinet config, never deleted): {path}")
        return
    if path.exists() or path.is_symlink():
        try:
            path.unlink()
            log.append(f"  removed: {path}")
        except Exception:
            pass


# ── 1) settings.json hook removal ─────────────────────────────────────────────
def _clean_settings(log: list[str]) -> None:
    """Remove ONLY Bubble Shield's hook entries from settings.json, matched by the
    SAME MARKER install uses. Preserve everything else exactly. Fail-safe: a missing
    or unreadable file is a clean no-op (never clobbered, never created)."""
    sp = _settings_path()
    if not sp.is_file():
        log.append(f"  no settings.json — nothing to clean ({sp})")
        return
    try:
        data = json.loads(sp.read_text(encoding="utf-8")) or {}
    except Exception:
        # Don't clobber an unreadable/shared settings file — bail fail-safe.
        log.append(f"  settings.json unreadable — left untouched ({sp})")
        return

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        log.append("  settings.json has no hooks — nothing to clean")
        return

    # Each hook array maps to the script-kind the installer tagged it with.
    KINDS = {
        "PreToolUse": "guard.py",
        "PostToolUse": "posttool_anonymize.py",
        "UserPromptSubmit": "tripwire.py",
        "SessionStart": "rearm-daemon",
    }
    removed = 0
    changed = False
    for arr_name, kind in KINDS.items():
        arr = hooks.get(arr_name)
        if not isinstance(arr, list):
            continue
        kept = [e for e in arr if not _entry_is_bubble_shield(e, kind)]
        if len(kept) != len(arr):
            removed += len(arr) - len(kept)
            hooks[arr_name] = kept
            changed = True

    if not changed:
        log.append("  no Bubble Shield hooks in settings.json (no-op)")
        return

    sp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.append(f"  removed {removed} Bubble Shield hook entr"
               f"{'y' if removed == 1 else 'ies'} from settings.json (other hooks preserved)")


# ── 2-3) LaunchAgent unload + remove ──────────────────────────────────────────
def _remove_launchagent(log: list[str]) -> None:
    any_found = False
    for plist in _launch_plists():
        if not plist.exists():
            continue
        any_found = True
        # Best-effort unload BEFORE removing the file (so launchd forgets it now).
        try:
            subprocess.run(["launchctl", "unload", str(plist)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        _rm_file(plist, log)
    if not any_found:
        log.append("  no LaunchAgent plists (no-op)")


def _check_shared_override(log: list[str]) -> list[str]:
    """If a shared-config path is configured (env override pointing OUTSIDE the local
    tree, or a Dropbox-marked folder), record that we explicitly skipped it. Returns
    the list of shared paths skipped (for the caller / tests to assert on)."""
    skipped: list[str] = []
    for env in ("BUBBLE_SHIELD_SHARED_CONFIG", "BUBBLE_SHIELD_HOME"):
        val = os.environ.get(env)
        if not val:
            continue
        p = Path(val)
        if _is_shared_path(p):
            skipped.append(str(p))
            log.append(f"  SKIP shared config ({env}={p}) — owned by the cabinet, "
                       f"NEVER deleted by a single client's uninstall")
    return skipped


def uninstall(purge_data: bool = False) -> list[str]:
    """Remove the LOCAL Bubble Shield footprint. Idempotent + fail-safe.

    Returns the list of SHARED paths it explicitly skipped (empty if none) — so a
    caller can prove the shared cabinet store survived.

    NEVER deletes or descends into a shared (Dropbox-marked) folder, even with
    purge_data=True. VAULTS are always local; --purge-data only removes the LOCAL
    ~/.bubble_shield, never a shared store.
    """
    log: list[str] = ["Bubble Shield uninstall — removing LOCAL footprint:"]

    # First: detect + record any shared-config path so we (and the operator) know it
    # is being deliberately left alone.
    skipped = _check_shared_override(log)

    # 1) settings.json hooks (the active interception layer) — by MARKER, others kept.
    _clean_settings(log)

    # 2) STABLE_DIR host scripts.
    _rm_tree(_stable_dir(), log)

    # 3) LaunchAgent — unload then rm.
    _remove_launchagent(log)

    # 4) plugin cache.
    _rm_tree(_plugin_cache(), log)

    # 5) ~/.config/bubble_shield (LOCAL config).
    _rm_tree(_config_dir(), log)

    # 6) Desktop app footprint.
    _rm_tree(_app_dir(), log)
    _rm_tree(_desktop() / "Bubble Shield.app", log)
    _rm_file(_desktop() / "Bubble Shield.command", log)

    # 7) LOCAL data dir (vaults + models) — gated behind --purge-data. _rm_tree
    #    refuses it anyway if it ever resolves to a shared path.
    data = _data_dir()
    if purge_data:
        if _is_shared_path(data):
            skipped.append(str(data))
            log.append(f"  REFUSED --purge-data on shared path: {data} "
                       f"(vaults are local-only; this looks shared — skipped)")
        else:
            _rm_tree(data, log)
            log.append("  (--purge-data) removed LOCAL ~/.bubble_shield vaults + models")
    else:
        log.append(f"  KEPT data dir (vaults + models): {data} "
                   f"— pass --purge-data to remove it (you lose decloak ability)")

    log.append("Done. Shared cabinet config (if any) was never touched.")
    print("\n".join(log))
    return skipped


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Uninstall Bubble Shield's LOCAL footprint (never touches shared cabinet config).")
    ap.add_argument("--purge-data", action="store_true",
                    help="also remove the LOCAL ~/.bubble_shield data dir (vaults + models). "
                         "You lose this machine's decloak ability. Never affects shared/Dropbox config.")
    args = ap.parse_args()
    uninstall(purge_data=args.purge_data)
    sys.exit(0)


if __name__ == "__main__":
    main()
