#!/usr/bin/env python3
"""Bubble Shield — SessionStart self-installer for Cowork.

WHY THIS EXISTS
---------------
In Claude Cowork (Desktop), the agent runs in a VM spawned with
`--setting-sources=user`. That flag means Cowork loads hooks ONLY from the VM's
user settings (`$HOME/.claude/settings.json`, i.e. `/root/.claude/settings.json`)
and SILENTLY ignores hooks bundled in a plugin's `hooks/hooks.json`
(see anthropics/claude-code issue #16288). So our PreToolUse guard + the
UserPromptSubmit tripwire never fire in Cowork when they live only in the plugin.

The fix the community converged on: a SessionStart hook (which DOES fire from a
plugin in Cowork) writes the guard hooks into the user settings file at session
start. This script is that installer. It is idempotent — it only writes if the
guard hooks aren't already present, and never disturbs other hooks.

On the CLI (outside Cowork) the plugin's own hooks.json already works, so this is
a harmless no-op there (it just ensures the same hooks exist in user settings).

Run as a SessionStart command hook. Reads the event JSON on stdin (unused). Exits
0 always — a failed self-install must never block the session.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

# Tools the guard inspects. Standard file tools + ALL mcp__ tools: the guard
# matches mcp__.* so it can intercept mail connectors (gmail search/get) for the
# mail-guard, and Cowork's shell (mcp__workspace__bash). SAFE to match mcp__.*
# here because the GUARD only ever emits allow/deny — never updatedToolOutput —
# so it can't hit the content-block-shape bug that forced the PostToolUse hook
# to narrow its own matcher (#H.reduce). Non-mail mcp tools just fall through to
# allow.
PRETOOL_MATCHER = "Read|Edit|Write|Glob|Grep|Bash|NotebookEdit|mcp__.*"

# A marker so we can recognise (and update) our own entries idempotently.
MARKER = "bubble-shield"

# ---------------------------------------------------------------------------
# STABLE INSTALL DIR — why this exists (incident 2026-06-03)
# ---------------------------------------------------------------------------
# CLAUDE_PLUGIN_ROOT in Cowork points at a TEMP staging dir
# (/var/folders/.../T/claude-hostloop-plugins/<hash>/). macOS purges that dir on
# reboot/idle. If we bake that volatile path into the user-settings hook command,
# the next session runs a PreToolUse command whose script no longer exists → the
# hook errors → and a failing PreToolUse hook BLOCKS the tool. Result: every
# Bash/Read dies and the user's Claude is bricked until someone hand-edits
# settings.json. (This actually happened.)
#
# Fix: copy the hook scripts ONCE into a stable, never-purged location under the
# user's real home, and point the settings hook at THAT copy — never at the temp
# plugin root. We also wrap the command so a missing script FAILS OPEN (a guard
# that can't find itself must not brick the machine).
STABLE_DIR = Path(os.environ.get("HOME") or os.path.expanduser("~")) / ".claude" / "bubble-shield"


def _install_scripts() -> Path:
    """Copy guard.py + tripwire.py (and their config dir) into STABLE_DIR.

    Returns STABLE_DIR. Idempotent — overwrites with the current plugin version
    each session so updates propagate. guard.py/tripwire.py are pure-stdlib and
    discover config via in-folder markers + ~/.config (NOT via plugin root), so
    relocating them does not break config loading.
    """
    src = Path(PLUGIN_ROOT) / "scripts"
    STABLE_DIR.mkdir(parents=True, exist_ok=True)
    # guard.py + tripwire.py are self-contained (pure stdlib). posttool_anonymize.py
    # needs the engine in vendor/ AND tripwire.py — we copy those too so the stable
    # copy is self-sufficient (the hook resolves them via CLAUDE_PLUGIN_ROOT=STABLE_DIR).
    for name in ("guard.py", "tripwire.py", "posttool_anonymize.py", "bubble_shield_nerd.py"):
        s = src / name
        if s.is_file():
            shutil.copy2(s, STABLE_DIR / name)
    # the optional plugin-root config fallback (back-compat config search)
    cfg_src = Path(PLUGIN_ROOT) / "config"
    if cfg_src.is_dir():
        shutil.copytree(cfg_src, STABLE_DIR / "config", dirs_exist_ok=True)
    # the engine, so posttool_anonymize.py can import bubble_shield/* from STABLE_DIR/vendor.
    # Only copied once (skip if present) — it's ~3.5MB and doesn't change per session.
    vendor_src = Path(PLUGIN_ROOT) / "vendor"
    if vendor_src.is_dir() and not (STABLE_DIR / "vendor" / "bubble_shield").is_dir():
        shutil.copytree(vendor_src, STABLE_DIR / "vendor", dirs_exist_ok=True)
    return STABLE_DIR


def _wrapped_cmd(script: str) -> str:
    """Hook command that FAILS OPEN if the script is missing.

    `[ -f X ] && python3 X || exit 0` — if the stable script ever disappears,
    the hook exits 0 (allow) instead of erroring (which would block the tool).
    This is the safety net that prevents a repeat of the temp-purge self-lock.
    """
    path = f"{STABLE_DIR}/{script}"
    return (
        f"[ -f '{path}' ] && CLAUDE_PLUGIN_ROOT='{STABLE_DIR}' python3 '{path}' "
        f"|| exit 0  # {MARKER}:{script}"
    )


GUARD_CMD = _wrapped_cmd("guard.py")
TRIP_CMD = _wrapped_cmd("tripwire.py")
POST_CMD = _wrapped_cmd("posttool_anonymize.py")


def _in_cowork_vm() -> bool:
    """True ONLY when we are genuinely inside the Cowork (local-agent) sandbox VM.

    WHY THIS GATE EXISTS (incident: repeated host-Mac self-lock)
    -----------------------------------------------------------
    This installer writes a PreToolUse guard + UserPromptSubmit tripwire into
    `$HOME/.claude/settings.json`. That file is SHARED by every Claude on the
    host Mac — CLI sessions, crons, the Desktop app. When this installer ran on
    the real Mac it spilled the guard into the host's user settings, where it
    could (and did) block every Bash/Read for unrelated sessions and scheduled
    tasks. The guard must arm in Cowork and NOWHERE ELSE.

    We confirmed (live Cowork probe, 2026-06-14 + anthropics/claude-code#40495)
    the reliable Cowork-VM signals:
      - HOME is `/sessions/<name>`  (host Mac HOME is `/Users/...` or `/root`
        only in older layouts — never `/sessions/`). This is the primary gate:
        a real Mac can never have HOME under /sessions/.
      - CLAUDE_CODE_IS_COWORK == "1"        (set in the host-loop/hook context)
      - CLAUDE_CODE_ENTRYPOINT == "local-agent"

    We treat ANY of these as "in Cowork". On the host Mac none of them hold, so
    the installer no-ops → the host settings.json is NEVER touched. Fail-safe
    direction: if we cannot positively confirm Cowork, we DO NOT install
    (better an unarmed guard than a bricked Mac — the plugin's own hooks.json
    still provides protection where the platform honours it).
    """
    home = os.environ.get("HOME", "")
    if home.startswith("/sessions/"):
        return True
    if os.environ.get("CLAUDE_CODE_IS_COWORK") == "1":
        return True
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "local-agent":
        return True
    return False


def _user_settings_path() -> Path:
    # Inside the Cowork VM, $HOME is `/sessions/<name>`; user-scope settings is
    # $HOME/.claude/settings.json. (We only ever reach here when _in_cowork_vm()
    # is true, so this never resolves to the host Mac's home.)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".claude" / "settings.json"


def _entry_is_bubble_shield(entry: dict, kind: str) -> bool:
    """True if this hook-array entry is one we installed (by command marker).

    Matches if the command references our script (`kind`, e.g. "guard.py") AND
    is recognisably ours — either it carries the MARKER or it points at our
    plugin/stable dir. This MUST catch stale entries from older installs (incl.
    the temp-path ones from the 2026-06-03 incident) so they get replaced, never
    duplicated or left dangling.
    """
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if kind in cmd and (MARKER in cmd or "bubble_shield" in cmd.lower()):
            return True
    return False


def main() -> None:
    try:
        sys.stdin.read()  # drain event JSON; we don't need it
    except Exception:
        pass

    # COWORK-ONLY GATE — the whole point of this installer is to arm the guard
    # inside the Cowork VM. On the host Mac (or anywhere we can't positively
    # confirm Cowork) we must NOT write into the shared user settings.json, or
    # we spill the guard onto the machine and risk bricking unrelated sessions
    # and crons. So: if we're not in Cowork, do nothing at all. We do NOT even
    # copy the scripts to STABLE_DIR — leaving zero footprint on the host.
    if not _in_cowork_vm():
        sys.exit(0)

    try:
        # 1) copy the hook scripts to a stable, never-purged location FIRST, so
        #    the command we write below points at a path that survives reboots.
        _install_scripts()

        p = _user_settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                # Don't clobber an unreadable settings file — bail quietly.
                sys.exit(0)

        hooks = data.setdefault("hooks", {})

        # --- PreToolUse guard ---
        pre = hooks.setdefault("PreToolUse", [])
        # remove any stale bubble_shield guard entries, then add the current one
        pre = [e for e in pre if not _entry_is_bubble_shield(e, "guard.py")]
        pre.append({
            "matcher": PRETOOL_MATCHER,
            "hooks": [{"type": "command", "command": GUARD_CMD}],
        })
        hooks["PreToolUse"] = pre

        # --- UserPromptSubmit tripwire ---
        ups = hooks.setdefault("UserPromptSubmit", [])
        ups = [e for e in ups if not _entry_is_bubble_shield(e, "tripwire.py")]
        ups.append({
            "hooks": [{"type": "command", "command": TRIP_CMD}],
        })
        hooks["UserPromptSubmit"] = ups

        # --- PostToolUse anonymiser (the ML "PII from anywhere" tier) ---
        # Opt-in at runtime (posttool_enabled in config) — the hook self-disables
        # when off, so installing the entry is harmless for clients who don't use it.
        post = hooks.setdefault("PostToolUse", [])
        post = [e for e in post if not _entry_is_bubble_shield(e, "posttool_anonymize.py")]
        post.append({
            "matcher": "Read|Bash|mcp__.*",
            "hooks": [{"type": "command", "command": POST_CMD}],
        })
        hooks["PostToolUse"] = post

        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # A self-install failure must never break the session.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
