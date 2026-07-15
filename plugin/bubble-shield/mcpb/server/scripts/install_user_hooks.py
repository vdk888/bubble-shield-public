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

    FIX 3 (2026-07-14): for guard.py we run it as an IMPORTED MODULE, not as a
    `__main__` script. `python3 X` compiles X from source EVERY fire (a `__main__`
    module is never byte-cached), so the 88KB guard was recompiled on every Read/
    Edit/Write/Bash/mcp__* call — pure CPU + a bigger transient-failure window under
    concurrent bursts. Importing it (`import guard; guard.main()`) writes/reuses
    guard.cpython-3XX.pyc in __pycache__, so after the first fire each subsequent
    fire skips the recompile. Behaviour is identical (guard.main() is the same
    entry point); only startup cost drops. Other scripts stay `python3 X` (small).
    """
    path = f"{STABLE_DIR}/{script}"
    if script == "guard.py":
        mod = script[:-3]  # "guard"
        inner = (
            "import sys; "
            f"sys.path.insert(0, '{STABLE_DIR}'); "
            f"import {mod}; {mod}.main()"
        )
        return (
            f"[ -f '{path}' ] && CLAUDE_PLUGIN_ROOT='{STABLE_DIR}' "
            f"python3 -c \"{inner}\" "
            f"|| exit 0  # {MARKER}:{script}"
        )
    return (
        f"[ -f '{path}' ] && CLAUDE_PLUGIN_ROOT='{STABLE_DIR}' python3 '{path}' "
        f"|| exit 0  # {MARKER}:{script}"
    )


GUARD_CMD = _wrapped_cmd("guard.py")
TRIP_CMD = _wrapped_cmd("tripwire.py")
POST_CMD = _wrapped_cmd("posttool_anonymize.py")

# SessionStart re-arm: health-check NER daemon and re-spawn if down.
# Inline Python so it works regardless of CLAUDE_PLUGIN_ROOT path. Fails open.
_REARM_SCRIPT = (
    "python3 -c \""
    "import sys,os;"
    "sys.path.insert(0,'{stable}/vendor');"
    "sys.path.insert(0,'{stable}');"
    "import posttool_anonymize as p;"
    "p._try_spawn_daemon();"
    "\" || true  # {marker}:rearm-daemon"
).format(stable=STABLE_DIR, marker=MARKER)
REARM_CMD = (
    f"[ -f '{STABLE_DIR}/posttool_anonymize.py' ] && {_REARM_SCRIPT} || true"
)


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


def _guard_armed_in_host_settings() -> bool:
    """True if the host settings.json ALREADY carries our PreToolUse guard entry.

    This is the opt-in signal for the host refresh: we only refresh STABLE_DIR
    scripts for a machine that has already chosen to run the guard. A machine
    that never armed it has no entry → returns False → no refresh → zero
    footprint. Fail-closed toward NO-refresh: any read/parse error → False (never
    touch a machine we can't positively confirm opted in)."""
    try:
        p = _user_settings_path()
        if not p.is_file():
            return False
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        pre = (data.get("hooks", {}) or {}).get("PreToolUse", []) or []
        return any(_entry_is_bubble_shield(e, "guard.py") for e in pre)
    except Exception:
        return False


def _refresh_stable_scripts_if_armed() -> None:
    """HOST last-mile fix: if the guard is already armed on this host, re-copy the
    current plugin's hook scripts into STABLE_DIR so a plugin/app UPDATE actually
    refreshes the live guard the hook runs — closing the gap where a shipped guard
    fix never reached STABLE_DIR/guard.py (v1.23.27 incident).

    Copy-ONLY: never writes settings.json, never ARMS the guard. Gated on
    `_guard_armed_in_host_settings()` so a machine that never opted in is left
    completely untouched (same zero-footprint guarantee as before). Also force-
    refreshes the vendored engine, because a guard/posttool fix may depend on
    engine changes and `_install_scripts` otherwise skips vendor once present."""
    if not _guard_armed_in_host_settings():
        return
    _install_scripts()
    _refresh_daemon_stable_dir_if_present()
    # `_install_scripts` skips vendor/ if already present (it's big + usually
    # unchanged). But a refresh is exactly when a stale vendored engine should be
    # updated too, so force it here.
    try:
        vendor_src = Path(PLUGIN_ROOT) / "vendor"
        if vendor_src.is_dir():
            shutil.copytree(vendor_src, STABLE_DIR / "vendor", dirs_exist_ok=True)
    except Exception:
        pass  # best-effort; the script refresh above is the critical part


_DAEMON_STABLE_ROOT = (Path(os.environ.get("BUBBLE_SHIELD_HOME")
                            or (Path.home() / ".bubble_shield")) / "daemon")
# The daemon files the launchd nerd/gemmad run (mirrors setup_ml's allowlists).
_DAEMON_STABLE_SCRIPTS = (
    "bubble_shield_nerd.py", "bubble_shield_setup_ml.py",
    "bubble_shield_gemmad.py", "gemma_classifier.py",
)
_DAEMON_LAUNCHD_LABELS = (
    "com.bubbleinvest.bubble-shield-nerd", "com.bubbleshield.gemmad",
)


def _refresh_daemon_stable_dir_if_present() -> None:
    """#644 (2026-07-15) — a plugin/app UPDATE must ALSO refresh the DAEMON stable dir.

    The launchd nerd/gemmad run from `~/.bubble_shield/daemon/{scripts,vendor}`, which
    `setup_ml.install_daemon_to_stable_path` copies out ONCE at ML-pack setup and NEVER
    refreshes on update. Verified live 2026-07-15: the daemon's vendored engine was 3
    days stale while the checkout was current — the SAME 'repo ≠ running code' class the
    guard host-refresh (v1.23.28) fixed, in a different location. A detection/verify fix
    (e.g. #643) shipped to the plugin would silently never reach the running daemon.

    Fix: on the armed host-refresh, ALSO re-copy the daemon's scripts (allowlist) + the
    whole vendor tree into the daemon stable dir — GATED on that dir already EXISTING (a
    machine without the ML pack has no daemon dir → untouched, zero footprint). Then
    KICKSTART the launchd daemons so a long-lived KeepAlive process picks up the new code
    instead of running stale in-memory for up to its idle window (4h). Copy-only + best-
    effort: never raises, never installs anything new."""
    try:
        if not _DAEMON_STABLE_ROOT.is_dir():
            return  # ML pack not installed on this host → nothing to refresh
        src_scripts = Path(PLUGIN_ROOT) / "scripts"
        dst_scripts = _DAEMON_STABLE_ROOT / "scripts"
        dst_scripts.mkdir(parents=True, exist_ok=True)
        changed = False
        for name in _DAEMON_STABLE_SCRIPTS:
            s = src_scripts / name
            if not s.is_file():
                continue
            d = dst_scripts / name
            # Only copy (and flag changed) when the content actually differs — so we
            # DON'T pointlessly kickstart the daemons on every SessionStart.
            if (not d.is_file()) or (s.read_bytes() != d.read_bytes()):
                shutil.copy2(s, d)
                changed = True
        vendor_src = Path(PLUGIN_ROOT) / "vendor"
        dst_vendor_engine = _DAEMON_STABLE_ROOT / "vendor" / "bubble_shield" / "engine.py"
        src_vendor_engine = vendor_src / "bubble_shield" / "engine.py"
        if vendor_src.is_dir():
            # Cheap staleness probe on the engine (the file that stranded live today);
            # if it differs, refresh the WHOLE vendor tree and flag changed.
            if (not dst_vendor_engine.is_file() or not src_vendor_engine.is_file()
                    or src_vendor_engine.read_bytes() != dst_vendor_engine.read_bytes()):
                shutil.copytree(vendor_src, _DAEMON_STABLE_ROOT / "vendor",
                                dirs_exist_ok=True)
                changed = True
        # Only restart the launchd daemons when code ACTUALLY changed, so a stale
        # KeepAlive process picks up the fix — but we don't churn them every session.
        if changed:
            _kickstart_daemons()
    except Exception:
        pass  # best-effort; a refresh failure must never break SessionStart


def _kickstart_daemons() -> None:
    """launchctl kickstart -k each daemon label so it restarts with fresh code. Uses
    the modern gui/<uid> domain; best-effort, never raises."""
    import subprocess
    try:
        uid = os.getuid()
    except Exception:
        return
    for label in _DAEMON_LAUNCHD_LABELS:
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                capture_output=True, timeout=10)
        except Exception:
            pass  # daemon not loaded / launchctl unavailable → skip


def main() -> None:
    try:
        sys.stdin.read()  # drain event JSON; we don't need it
    except Exception:
        pass

    # COWORK-ONLY GATE — arming the guard (writing hook entries into the shared
    # user settings.json) is Cowork-only. On the host Mac we must NOT write into
    # settings.json or we'd spill the guard onto a machine that never opted in,
    # risking unrelated sessions and crons.
    #
    # HOST REFRESH (2026-07-15) — but there is a last-mile gap: if the user HAS
    # already armed the guard on their host (Mac install), a plugin/app UPDATE
    # refreshes the checkout yet NEVER refreshes the live guard the hook actually
    # runs (STABLE_DIR/guard.py). So a shipped guard fix silently never reaches
    # the running hook — the exact failure that stranded v1.23.27 (guard.py at
    # STABLE_DIR stayed on old flaky code while the checkout was fixed). Fix:
    # when NOT in Cowork, if the guard is ALREADY armed in the host settings
    # (user opted in previously), REFRESH the STABLE_DIR scripts from the current
    # plugin — WITHOUT touching settings.json. This is copy-only (no arming), so
    # it cannot spill onto a machine that never opted in: no armed entry → no
    # refresh → zero footprint, exactly as before. It only ever updates code the
    # user is already running.
    if not _in_cowork_vm():
        try:
            _refresh_stable_scripts_if_armed()
        except Exception:
            pass  # best-effort: a refresh failure must never break SessionStart
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

        # --- SessionStart NER daemon re-arm ---
        # Best-effort: health-check the NER daemon at session start and re-spawn
        # if down. Fail-OPEN — a failed spawn must never block the session. This
        # ensures every new Cowork session re-arms NER so the first
        # bubble_shield_read call has a live daemon (not a stale/dead one).
        sess = hooks.setdefault("SessionStart", [])
        sess = [e for e in sess if not _entry_is_bubble_shield(e, "rearm-daemon")]
        sess.append({
            "hooks": [{"type": "command", "command": REARM_CMD}],
        })
        hooks["SessionStart"] = sess

        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # A self-install failure must never break the session.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
