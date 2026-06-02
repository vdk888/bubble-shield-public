#!/usr/bin/env python3
"""Caveau guard — PreToolUse hook.

Reads the PreToolUse event JSON on stdin. If the tool is about to touch a file
inside a *protected* client folder, it DENIES the call (permissionDecision:
"deny") and tells Claude to run the data through Caveau first.

Fail-closed by design:
  - a `deny` from a PreToolUse hook blocks the tool even under
    bypassPermissions / --dangerously-skip-permissions (per Claude Code docs);
  - if the config is missing/malformed, or anything goes wrong while deciding,
    we DENY rather than allow — a guard that fails open is no guard.

Pure stdlib. No import of the Caveau engine here: the guard's only job is the
gate. Anonymisation itself is the `caveau-anonymize` skill / the webapp.

Exit/΄output contract (Claude Code hooks):
  - print a JSON object with hookSpecificOutput.permissionDecision and exit 0.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))

# The in-folder marker filename. THIS is the Cowork-native way to protect a
# folder: drop a `.caveau-guard.json` inside the client folder. Cowork (which is
# sandboxed and refuses to write to ~/.config or other dotfile/system dirs) CAN
# write into a folder the user has connected to the session — so the marker lives
# with the data it governs (same idea as .gitignore / .editorconfig). The guard
# walks UP from each target file; if any ancestor holds a marker, that ancestor
# is a protected root. The marker may be empty ({}) or carry per-folder overrides
# (allow_paths / allow_extensions / message_fr).
MARKER_NAME = ".caveau-guard.json"

# Optional GLOBAL config (back-compat + multi-folder deployments via Claude Code
# CLI, where ~/.config IS writable). Search order, first hit wins. The global
# config and the in-folder markers COMPOSE — either can protect a folder.
CONFIG_LOCATIONS = [
    os.environ.get("CAVEAU_GUARD_CONFIG"),                       # explicit override
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".caveau-guard.json"),
    os.path.expanduser("~/.config/caveau/caveau-guard.json"),
    os.path.expanduser("~/.caveau-guard.json"),
    str(PLUGIN_ROOT / "config" / "caveau-guard.json"),
]

DEFAULT_MESSAGE = (
    "🔒 Caveau — accès bloqué. Ce fichier est dans un dossier client protégé. "
    "Lance d'abord l'anonymisation Caveau, puis travaille sur la copie anonymisée."
)


def _decide(decision: str, reason: str) -> None:
    """Emit the PreToolUse hook JSON and exit 0."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,          # "deny" | "allow"
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _deny(reason: str) -> None:
    _decide("deny", reason)


def _allow(reason: str = "") -> None:
    # No decision needed for the normal case: exit 0 with no JSON lets the
    # normal permission flow proceed. We only emit explicit "allow" when we
    # want to short-circuit (e.g. an explicitly allow-listed path).
    if reason:
        _decide("allow", reason)
    sys.exit(0)


def _load_config() -> dict:
    for loc in CONFIG_LOCATIONS:
        if not loc:
            continue
        p = Path(loc)
        if p.is_file():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                # Malformed config → fail CLOSED. A guard you can't parse must
                # not silently wave everything through.
                _deny(
                    f"🔒 Caveau guard: fichier de configuration illisible ({p}): {e}. "
                    "Par sécurité, l'accès est bloqué tant que la config n'est pas réparée."
                )
            cfg["_config_path"] = str(p)
            return cfg
    # No config found at all → treat as "guard installed but not configured".
    # We do NOT block everything (that would brick the session); we return an
    # empty protected set so the guard is inert until configured. Surfaced via
    # additionalContext is overkill here; a no-op is the least-surprise default
    # for an unconfigured install.
    return {"protected_folders": [], "_config_path": None}


def _norm(path_str: str) -> Path | None:
    """Resolve a path to an absolute, symlink-resolved Path. None if empty."""
    if not path_str:
        return None
    try:
        return Path(os.path.expanduser(path_str)).resolve()
    except Exception:
        return None


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or lives inside it (after resolve)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _find_marker_root(target: Path) -> tuple[Path, dict] | None:
    """Walk UP from `target` looking for a `.caveau-guard.json` marker. Returns
    (protected_root, marker_data) for the NEAREST ancestor that holds one, or
    None. The protected root is the folder CONTAINING the marker.

    Fail-closed on a marker we can't parse: a corrupt marker still protects its
    folder (we return an empty override dict), rather than waving data through.
    The marker file itself is always readable (it's our own metadata, not PII).
    """
    # Start at the file's own directory (or the path itself if it's a dir).
    start = target if target.is_dir() else target.parent
    candidates = [start, *start.parents]
    for anc in candidates:
        marker = anc / MARKER_NAME
        try:
            if not marker.is_file():
                continue
        except OSError:
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}            # corrupt marker → still protects (fail-closed)
        return anc, data
    return None


def _discover_marker_roots(cwd: str, max_depth: int = 4) -> list[Path]:
    """Find folders carrying a marker, to build the Bash command-scan needles.
    For Bash we can't walk up from a single target path (it's buried in a command
    string), so we enumerate marker roots near the session: the cwd's ancestors
    (cheap) + a SHALLOW descent of the cwd (bounded, so a huge tree can't stall
    the hook). Best-effort: misses a marker far outside cwd, but the file-tool
    path (the real leak vector) still walks up correctly per-file."""
    roots: list[Path] = []
    if not cwd:
        return roots
    # Use the UN-resolved (expanduser only) base so discovered paths match how
    # they'd appear in a shell command (macOS /var vs /private/var symlink).
    base = Path(os.path.expanduser(cwd))
    # ancestors (incl. base)
    for anc in [base, *base.parents]:
        try:
            if (anc / MARKER_NAME).is_file():
                roots.append(anc)
        except OSError:
            continue
    # shallow descent (bounded breadth + depth so we never walk a giant tree)
    try:
        stack = [(base, 0)]
        seen = 0
        while stack and seen < 2000:
            d, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                seen += 1
                if e.name == MARKER_NAME and e.is_file():
                    r = Path(d)
                    if r not in roots:
                        roots.append(r)
                elif e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    stack.append((Path(e.path), depth + 1))
    except Exception:
        pass
    return roots


def _candidate_paths(tool_name: str, tool_input: dict, cwd: str) -> list[Path]:
    """Extract the filesystem path(s) a tool call would touch."""
    out: list[Path] = []

    def add(raw):
        if not raw or not isinstance(raw, str):
            return
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        try:
            out.append(p.resolve())
        except Exception:
            out.append(p)

    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        add(tool_input.get("file_path") or tool_input.get("notebook_path"))
    elif tool_name in ("Glob", "Grep"):
        # `path` is the search root; the pattern itself may also be a path-ish glob
        add(tool_input.get("path"))
    # Bash is handled separately (substring scan of the command string).
    return out


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Can't even parse the event → fail closed.
        _deny("🔒 Caveau guard: évènement hook illisible. Accès bloqué par sécurité.")
        return

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    cwd = event.get("cwd", "") or os.getcwd()

    # GLOBAL config (optional; back-compat + CLI multi-folder). Markers compose
    # with it. The guard is NO LONGER inert just because the global config is
    # empty — in Cowork there is no global config at all, only in-folder markers.
    cfg = _load_config()
    protected_raw = list(cfg.get("protected_folders", []))
    protected = [p for p in (_norm(x) for x in protected_raw) if p]
    g_allow_paths = [p for p in (_norm(x) for x in cfg.get("allow_paths", [])) if p]
    g_allow_exts = tuple(e.lower() for e in cfg.get("allow_extensions", []) if e)
    block_bash = bool(cfg.get("block_bash", True))
    g_message = cfg.get("message_fr") or DEFAULT_MESSAGE

    def _ext_exempt(p: Path, exts: tuple) -> bool:
        if not exts:
            return False
        if "".join(p.suffixes).lower().endswith(exts):
            return True
        name_lower = p.name.lower()
        return any(name_lower.endswith(e) for e in exts)

    def decide_block(p: Path) -> tuple[bool, str]:
        """Return (blocked, message). A path is blocked if it sits under a
        global protected_folder OR under a folder carrying a marker. Per-folder
        marker overrides (allow_paths/allow_extensions/message_fr) apply when the
        protection came from a marker; otherwise the global ones apply."""
        # The marker file itself is never blocked — it's our own metadata (no
        # PII), and onboarding / the skill must be able to read & write it.
        if p.name == MARKER_NAME:
            return False, ""
        # 1) In-folder marker (the Cowork-native path). Nearest ancestor wins.
        hit = _find_marker_root(p)
        if hit is not None:
            root, mdata = hit
            m_allow_paths = [q for q in (_norm(x) for x in mdata.get("allow_paths", [])) if q]
            m_allow_exts = tuple(e.lower() for e in mdata.get("allow_extensions", []) if e)
            # marker overrides fall back to global defaults when unset
            allow_paths = m_allow_paths or g_allow_paths
            allow_exts = m_allow_exts or g_allow_exts
            message = mdata.get("message_fr") or g_message
            if any(_is_within(p, ap) for ap in allow_paths):
                return False, ""
            if _ext_exempt(p, allow_exts):
                return False, ""
            return True, message

        # 2) Global protected_folders (CLI / back-compat).
        if protected:
            if any(_is_within(p, ap) for ap in g_allow_paths):
                return False, ""
            if any(_is_within(p, prot) for prot in protected):
                if _ext_exempt(p, g_allow_exts):
                    return False, ""
                return True, g_message
        return False, ""

    # --- Bash: scan the command string for any protected path mention ---
    # Cowork runs shell via `mcp__workspace__bash` (not `Bash`); treat both. The
    # command may be under `command` (CLI Bash) or `script`/`code` (some MCP bash).
    if tool_name in ("Bash", "mcp__workspace__bash") or tool_name.endswith("__bash"):
        if not block_bash:
            _allow()
            return
        command = (tool_input.get("command")
                   or tool_input.get("script")
                   or tool_input.get("code") or "")
        home = os.path.expanduser("~")
        # Protected roots to scan for: global config folders + any marker folders
        # we can discover. We can't walk up from a "candidate file" for Bash (the
        # path is buried in a command string), so we enumerate marker roots by
        # scanning the folders referenced in global config plus the cwd's tree.
        roots: list[tuple[Path, str]] = []
        for prot, raw in zip(protected, protected_raw):
            roots.append((prot, raw))
        for mroot in _discover_marker_roots(cwd):
            roots.append((mroot, str(mroot)))
        needles: set[str] = set()
        for prot, raw in roots:
            prot_str = str(prot)
            # both the symlink-resolved form AND the un-resolved one: on macOS
            # /var → /private/var and /tmp → /private/tmp, so a command written
            # with the un-resolved path wouldn't match the resolved needle.
            variants = {prot_str, raw, os.path.expanduser(raw),
                        os.path.realpath(prot_str), os.path.abspath(os.path.expanduser(raw))}
            for v in list(variants):
                if v and v.startswith(home):
                    variants.add("~" + v[len(home):])
            needles |= {v for v in variants if v}
        for n in needles:
            if n and n in command:
                _deny(f"{g_message}\n[Caveau guard: commande shell touchant {n}]")
                return
        _allow()
        return

    # --- File tools: check each candidate path ---
    for p in _candidate_paths(tool_name, tool_input, cwd):
        blocked, message = decide_block(p)
        if blocked:
            _deny(f"{message}\n[Caveau guard: {p}]")
            return

    _allow()


if __name__ == "__main__":
    main()
