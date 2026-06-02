#!/usr/bin/env python3
"""Caveau — SessionStart self-installer for Cowork.

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
import sys
from pathlib import Path

PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

# Tools Cowork/CLI expose for file access. We include BOTH the standard names
# (Read/Edit/Write/Glob/Grep/Bash/NotebookEdit) AND Cowork's MCP shell tool
# (mcp__workspace__bash), which is how Cowork runs shell commands — a plain
# "Bash" matcher would miss it.
PRETOOL_MATCHER = "Read|Edit|Write|Glob|Grep|Bash|NotebookEdit|mcp__workspace__bash"

# A marker so we can recognise (and update) our own entries idempotently.
MARKER = "caveau-guard"

GUARD_CMD = f"CLAUDE_PLUGIN_ROOT={PLUGIN_ROOT} python3 {PLUGIN_ROOT}/scripts/guard.py"
TRIP_CMD = f"CLAUDE_PLUGIN_ROOT={PLUGIN_ROOT} python3 {PLUGIN_ROOT}/scripts/tripwire.py"


def _user_settings_path() -> Path:
    # $HOME inside the Cowork VM is /root; on the CLI it's the real home. Either
    # way, user-scope settings is $HOME/.claude/settings.json.
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".claude" / "settings.json"


def _entry_is_caveau(entry: dict, kind: str) -> bool:
    """True if this hook-array entry is one we installed (by command marker)."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if MARKER in cmd and (kind in cmd):
            return True
    return False


def main() -> None:
    try:
        sys.stdin.read()  # drain event JSON; we don't need it
    except Exception:
        pass

    try:
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
        # remove any stale caveau guard entries, then add the current one
        pre = [e for e in pre if not _entry_is_caveau(e, "guard.py")]
        pre.append({
            "matcher": PRETOOL_MATCHER,
            "hooks": [{"type": "command", "command": GUARD_CMD}],
        })
        hooks["PreToolUse"] = pre

        # --- UserPromptSubmit tripwire ---
        ups = hooks.setdefault("UserPromptSubmit", [])
        ups = [e for e in ups if not _entry_is_caveau(e, "tripwire.py")]
        ups.append({
            "hooks": [{"type": "command", "command": TRIP_CMD}],
        })
        hooks["UserPromptSubmit"] = ups

        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # A self-install failure must never break the session.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
