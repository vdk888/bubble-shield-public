#!/usr/bin/env python3
"""Bubble Shield guard — PreToolUse hook.

Reads the PreToolUse event JSON on stdin. If the tool is about to touch a file
inside a *protected* client folder, it DENIES the call (permissionDecision:
"deny") and tells Claude to run the data through Bubble Shield first.

Fail-closed by design:
  - a `deny` from a PreToolUse hook blocks the tool even under
    bypassPermissions / --dangerously-skip-permissions (per Claude Code docs);
  - if the config is missing/malformed, or anything goes wrong while deciding,
    we DENY rather than allow — a guard that fails open is no guard.

Pure stdlib. No import of the Bubble Shield engine here: the guard's only job is the
gate. Anonymisation itself is the `bubble-shield-anonymize` skill / the webapp.

Exit/΄output contract (Claude Code hooks):
  - print a JSON object with hookSpecificOutput.permissionDecision and exit 0.
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))

# The in-folder marker filename. THIS is the Cowork-native way to protect a
# folder: drop a `.bubble-shield.json` inside the client folder. Cowork (which is
# sandboxed and refuses to write to ~/.config or other dotfile/system dirs) CAN
# write into a folder the user has connected to the session — so the marker lives
# with the data it governs (same idea as .gitignore / .editorconfig). The guard
# walks UP from each target file; if any ancestor holds a marker, that ancestor
# is a protected root. The marker may be empty ({}) or carry per-folder overrides
# (allow_paths / allow_extensions / message_fr).
MARKER_NAME = ".bubble-shield.json"

# Optional GLOBAL config (back-compat + multi-folder deployments via Claude Code
# CLI, where ~/.config IS writable). Search order, first hit wins. The global
# config and the in-folder markers COMPOSE — either can protect a folder.
CONFIG_LOCATIONS = [
    os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG"),                       # explicit override
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".bubble-shield.json"),
    os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"),
    os.path.expanduser("~/.bubble-shield.json"),
    str(PLUGIN_ROOT / "config" / "bubble-shield.json"),
]

DEFAULT_MESSAGE = (
    "🔒 Bubble Shield — accès bloqué. Ce fichier est dans un dossier client protégé. "
    "N'utilise PAS Read/Bash dessus. À la place, appelle l'outil MCP "
    "`bubble_shield_read(path=\"…\")` : il te renvoie le contenu déjà anonymisé "
    "(jetons ⟦…⟧), les vraies valeurs ne touchent jamais ton contexte. Travaille "
    "sur ces jetons, puis produis le document final via `bubble_shield_write`."
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


def _allow_with_context(context: str) -> None:
    """Allow the tool to run, but inject a steering instruction the model sees
    alongside the result (PreToolUse supports allow + additionalContext). Used
    by the mail-guard: blocking the fetch is a catch-22 (the fetch is the only
    way to GET the mail to anonymise), so instead we let it through and forcefully
    instruct the model to anonymise the fetched text before using it."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Bubble Shield mail-guard: allowed with anonymise-first instruction.",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


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
                    f"🔒 Bubble Shield guard: fichier de configuration illisible ({p}): {e}. "
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


def _norm(path_str: str, base: Path | None = None) -> Path | None:
    """Resolve a path to an absolute, symlink-resolved Path. None if empty.

    A RELATIVE entry is resolved against `base` when given (the folder it was
    declared in — e.g. a marker's own directory), NOT the guard process CWD.
    This matters for marker `allow_paths`/`allow_extensions`: the marker
    documents them as "relative to THIS marker's folder", so `_norm("clean",
    base=marker_root)` must become `<marker_root>/clean`, never `<cwd>/clean`.
    Absolute and `~/`-anchored entries ignore `base`. `.resolve()` still follows
    symlinks, so an allow-listed path that symlinks OUT of the folder resolves
    to its real target and won't spuriously match a protected file.
    """
    if not path_str:
        return None
    try:
        p = Path(os.path.expanduser(path_str))
        if base is not None and not p.is_absolute():
            p = base / p
        return p.resolve()
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
    """Walk UP from `target` looking for a `.bubble-shield.json` marker. Returns
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


# Our OWN sanctioned MCP tools — these are the safe read/write path (they return
# ALREADY-anonymised content), so they must NEVER be treated as candidates to
# block. Matched by suffix so the opaque-prefixed form (mcp__<server>__bubble_shield_read)
# is covered too.
_OWN_MCP_TOOL_SUFFIXES = ("bubble_shield_read", "bubble_shield_write",
                          "bubble_shield_anonymize_text")

# Input keys that commonly carry a filesystem path across third-party MCP file
# servers (filesystem, text-editor, etc.). Scanned for generic mcp__* tools.
_MCP_PATH_KEYS = ("path", "file_path", "notebook_path", "uri", "filename",
                  "target", "file", "src", "source", "destination", "dest")
_MCP_PATH_LIST_KEYS = ("paths", "files", "sources", "targets")


def _candidate_paths(tool_name: str, tool_input: dict, cwd: str) -> list[Path]:
    """Extract the filesystem path(s) a tool call would touch."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(raw):
        if not raw or not isinstance(raw, str):
            return
        # A file:// URI (some MCP file servers use them) → strip the scheme.
        if raw.startswith("file://"):
            raw = raw[len("file://"):]
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        try:
            p = p.resolve()
        except Exception:
            pass
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        add(tool_input.get("file_path") or tool_input.get("notebook_path"))
    elif tool_name in ("Glob", "Grep"):
        # `path` is the search root; the pattern itself may also be a path-ish glob
        add(tool_input.get("path"))
    elif tool_name.startswith("mcp__"):
        # FIX 3 (P0-SEC-3): the hooks.json matcher runs the guard for EVERY mcp__*
        # tool, but historically only the 6 native tools above yielded candidates,
        # so any OTHER mcp__* file tool (e.g. a filesystem MCP server's
        # mcp__filesystem__read_file) fell through to _allow() — a silent leak.
        # Mail tools and *__bash tools are handled on their own code paths BEFORE
        # _candidate_paths is called, so reaching here means a generic MCP tool:
        # scan its input for path-shaped values and gate every one.
        #
        # Never treat our OWN sanctioned read/write tools as candidates — they are
        # the safe path (they return already-anonymised content).
        if not any(tool_name.endswith(s) for s in _OWN_MCP_TOOL_SUFFIXES):
            # 1) common path-bearing scalar keys
            for k in _MCP_PATH_KEYS:
                add(tool_input.get(k))
            # 2) common path-bearing list keys
            for k in _MCP_PATH_LIST_KEYS:
                v = tool_input.get(k)
                if isinstance(v, list):
                    for item in v:
                        add(item)
            # 3) BACKSTOP: run the same path regex used for shell commands over
            #    EVERY string value in tool_input, catching path-shaped values
            #    under keys we didn't enumerate. Over-matching is safe: a token
            #    that doesn't resolve under a marker is simply ignored downstream.
            for raw in _extract_paths_from_values(tool_input, cwd):
                add(str(raw))
    # Bash is handled separately (substring scan of the command string).
    return out


def _iter_string_values(obj) -> "list[str]":
    """Recursively collect all string values from a JSON-ish structure."""
    found: list[str] = []
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_iter_string_values(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(_iter_string_values(v))
    return found


def _extract_paths_from_values(tool_input: dict, cwd: str) -> list[Path]:
    """Backstop for generic MCP tools: run the shell-command path regexes over
    every string value in the tool input and return resolved path candidates.
    Reuses _extract_command_paths so absolute/home AND slash-relative tokens are
    caught with identical semantics to the Bash scan."""
    out: list[Path] = []
    for s in _iter_string_values(tool_input):
        out.extend(_extract_command_paths(s, cwd))
    return out


# Path-shaped tokens inside a shell command. We are NOT a shell parser; we just
# need to catch the realistic, path-bearing commands that exfiltrate a file —
# `tesseract /Users/x/dossier/avis.jpg stdout`, `cat "~/Clients/Dupont/x.pdf"`,
# `file '/path with spaces/secret.pdf'`, `xxd /a/b/c`. We pull every absolute
# (`/…`) and home (`~/…`) token out of the command, then resolve each one and run
# it through the SAME robust per-path marker walk-up that the file-tool path uses
# (`decide_block` → `_find_marker_root`). This is cwd-INDEPENDENT for absolute
# paths, which is the whole point: the old cwd-anchored `_discover_marker_roots`
# missed a marker whenever the bash tool's cwd was the session/workspace root
# rather than the connected client folder (the proven client exfil case).
#
# Tokenisation: split on unquoted whitespace and shell metacharacters, but keep
# single/double-quoted runs intact (so a path with spaces survives). We then scan
# each token for an absolute/home path substring. Common backslash-escaped spaces
# (`/path\ with\ space/x`) are un-escaped. This is deliberately permissive — a
# false candidate that doesn't resolve under a marker is simply ignored, so the
# cost of over-matching is zero, while under-matching is a security hole.
# Absolute / home path tokens (`/…`, `~/…`). These are resolvable WITHOUT a cwd,
# so they are the security-critical case the cwd-anchored scan used to miss.
_ABS_TOKEN_RE = re.compile(r"""
    (?:^|[\s=:,;()<>&|"'`])          # a boundary before the path
    (                                # capture the path
      ~?/                            # absolute (/…) or home (~/…)
      (?:\\.|[^\s'";`|&<>()])+       # path chars; \. consumes an escaped char
    )
""", re.VERBOSE)

# Relative path tokens with at least one slash AND a file-extension-ish tail
# (e.g. `Dupont/avis.jpg`, `sub/dir/secret.pdf`). Resolved against cwd. We keep
# this tight (must contain a `/` and end in a short alnum extension) so we don't
# treat every bare word / flag value as a path — relative resolution is only
# meaningful when cwd anchors it, and over-emitting plain words would be noise.
_REL_TOKEN_RE = re.compile(r"""
    (?:^|[\s=:,;()<>&|"'`])
    (
      (?:\./)?                        # optional leading ./
      (?:\\.|[^\s'";`|&<>()/])+       # first segment (no leading slash)
      (?:/(?:\\.|[^\s'";`|&<>()])+)+  # at least one more /segment
    )
""", re.VERBOSE)


# Shell glob metacharacters. A path token containing any of these in a PARENT
# segment used to bypass the guard: `Path("/…/cl*/Dupont/x.txt").resolve()` keeps
# the literal `cl*` segment, so the marker walk-up never finds the marker (which
# lives under the REAL expanded folder), while the shell expands the glob at
# runtime and reads the real file. See FIX 2 (P0-SEC-2).
_GLOB_META_RE = re.compile(r"[*?\[\]{}]")


def _longest_globfree_prefix(path_str: str) -> str:
    """Return the longest leading run of path SEGMENTS that contain no glob
    metachar. `/a/b/cl*/Dupont/x` → `/a/b`. Used as the fail-closed fallback
    root when a glob can't be expanded on disk."""
    segs = path_str.split(os.sep)
    keep: list[str] = []
    for s in segs:
        if _GLOB_META_RE.search(s):
            break
        keep.append(s)
    prefix = os.sep.join(keep)
    return prefix or os.sep


def _markers_under(root: Path, max_depth: int = 6, budget: int = 5000) -> list[Path]:
    """Discover marker-carrying folders at or below `root` (bounded). Used as the
    fail-closed fallback for an un-expandable glob whose glob-free prefix sits
    ABOVE a marked folder: we can't resolve the concrete file, so we protect any
    marked subtree the glob could reach."""
    out: list[Path] = []
    try:
        if (root / MARKER_NAME).is_file():
            out.append(root)
    except OSError:
        pass
    stack = [(root, 0)]
    seen = 0
    while stack and seen < budget:
        d, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            try:
                if e.name == MARKER_NAME and e.is_file():
                    r = Path(e.path).parent
                    if r not in out:
                        out.append(r)
                elif e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return out


def _extract_command_paths(command: str, cwd: str) -> list[Path]:
    """Pull path-shaped tokens out of a shell command and resolve them:
      - absolute/home tokens (`/…`, `~/…`) → resolved as-is (cwd-INDEPENDENT;
        this is the security-critical case the old cwd scan missed);
      - bare-relative tokens containing a `/` (`Dupont/avis.jpg`) → resolved
        against cwd, so a relative ref into a marked folder is also caught when
        cwd anchors it;
      - GLOB tokens (`cl*/Dupont/avis.txt`, `client?/`, `[c]lients/…`, `{a,b}/…`)
        → expanded against the real filesystem so each REAL match runs through the
        marker walk-up (FIX 2); if expansion yields nothing, we fall back to
        marker-discovery under the longest glob-free prefix (fail-closed — a glob
        we can't resolve near a protected area denies rather than allows).
    Best-effort, permissive: a token that doesn't resolve under any marker is
    simply ignored (zero cost), while a missed path would be a security hole.
    Returns resolved Paths.
    """
    if not command:
        return []
    out: list[Path] = []
    seen: set[str] = set()

    def emit(resolved: Path) -> None:
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        out.append(resolved)

    candidates: list[str] = [m.group(1) for m in _ABS_TOKEN_RE.finditer(command)]
    candidates += [m.group(1) for m in _REL_TOKEN_RE.finditer(command)]
    # Quoted paths with spaces (the regexes stop at the space): sweep quoted spans.
    for q in re.findall(r"'([^']*)'|\"([^\"]*)\"", command):
        span = q[0] or q[1]
        if span and ("/" in span):
            candidates.append(span)
    for raw in candidates:
        # Un-escape backslash-escaped chars (e.g. "/path\ with\ space").
        tok = re.sub(r"\\(.)", r"\1", raw).strip()
        if not tok or "/" not in tok:
            continue
        expanded = os.path.expanduser(tok)
        if not os.path.isabs(expanded):
            # relative → anchor against cwd (only meaningful with a cwd)
            if not cwd:
                continue
            expanded = os.path.join(cwd, expanded)

        if _GLOB_META_RE.search(expanded):
            # FIX 2: this token contains a glob metachar. `Path.resolve()` would
            # keep the literal glob segment and defeat the marker walk-up. Expand
            # it against the real filesystem instead.
            matched = False
            try:
                # brace expansion isn't done by glob.glob — expand {a,b} first.
                for pat in _expand_braces(expanded):
                    for m in _glob.glob(pat, recursive=True):
                        matched = True
                        try:
                            emit(Path(m).resolve())
                        except Exception:
                            emit(Path(m))
            except Exception:
                matched = False
            if not matched:
                # Nothing on disk matched (e.g. a `?`/`[…]`/`**` that only the
                # runtime shell would expand, or the file isn't present in the
                # guard's view). Fail closed: protect any marked subtree the glob
                # could reach, discovered under the longest glob-free prefix.
                prefix = _longest_globfree_prefix(expanded)
                try:
                    proot = Path(prefix).resolve()
                except Exception:
                    proot = Path(prefix)
                for mroot in _markers_under(proot):
                    # emit a path INSIDE the marked folder so decide_block's
                    # walk-up finds the marker and blocks (fail-closed).
                    emit(mroot / "\x00glob-unresolved")
            continue

        try:
            resolved = Path(expanded).resolve()
        except Exception:
            resolved = Path(expanded)
        emit(resolved)
    return out


def _expand_braces(pattern: str) -> list[str]:
    """Minimal brace expansion (`{a,b}` → [a, b]) so glob.glob can handle
    `{clients}/…` and `{a,b}/…` tokens. Only the FIRST brace group is expanded
    recursively; nested/adjacent groups are handled by recursion. glob.glob does
    NOT do brace expansion itself, so we do it here before globbing."""
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    end = pattern.find("}", start)
    if end == -1:
        return [pattern]
    pre, body, post = pattern[:start], pattern[start + 1:end], pattern[end + 1:]
    out: list[str] = []
    for opt in body.split(","):
        for tail in _expand_braces(post):
            out.append(pre + opt + tail)
    return out


def main() -> None:
    raw = sys.stdin.read()
    try:
        _main(raw)
    except SystemExit:
        # _deny / _allow legitimately call sys.exit(0) — let those through.
        raise
    except Exception:
        # FIX 1 (P0-SEC-1): ANY uncaught exception in the decision path must
        # fail CLOSED. Without this backstop, an unhandled error → Python exits
        # code 1 with NO deny JSON → per Claude Code hook semantics (only exit 2
        # or an explicit deny-JSON blocks; exit 1 is non-blocking) the tool RUNS,
        # leaking raw PII. This blanket wrapper enforces the guard's own stated
        # "anything goes wrong → we DENY" invariant. It is a BACKSTOP, not a
        # replacement — the explicit deny paths below (malformed event, etc.)
        # remain and give better messages; this only catches what they miss
        # (e.g. tool_input being a list, cwd being an int — both reproduced).
        _deny("🔒 Bubble Shield — erreur interne du guard, accès bloqué par sécurité.")


def _main(raw: str) -> None:
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Can't even parse the event → fail closed.
        _deny("🔒 Bubble Shield guard: évènement hook illisible. Accès bloqué par sécurité.")
        return
    if not isinstance(event, dict):
        _deny("🔒 Bubble Shield guard: évènement hook mal formé. Accès bloqué par sécurité.")
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

    # --- Mail-tool interception (steer raw mail reads through Bubble Shield) ---------
    # Mail lives behind a third-party connector (Gmail etc.) that Bubble Shield can't
    # fetch itself, so we can't transparently anonymise it. What we CAN do: when
    # the agent calls a mail connector's read/search, DENY it with a forceful
    # instruction to pipe the fetched text through bubble_shield_anonymize_text before
    # using it. This is a STEERING block, not containment: the agent will re-call
    # the tool to actually fetch (only it has creds), so raw mail still transits
    # the tool result once — but the deny interrupts the silent "just summarise
    # the raw mail" path (which was observed leaking a real e-mail address).
    # Opt-out via mail_guard:false. Mail tools are matched by name pattern.
    if cfg.get("mail_guard", True):
        tl = tool_name.lower()
        # Mail connectors carry an OPAQUE id (e.g. mcp__0ef9bd27-..__search_threads)
        # so "mail"/"gmail" is NOT in the name. Detect by the mail-specific ACTION:
        # 'thread' and 'message' are strongly mail-specific; also a mail/gmail/imap
        # token if present. Custom mail tools: add patterns via mail_tool_patterns.
        extra = [str(p).lower() for p in cfg.get("mail_tool_patterns", [])]
        is_mail = (tl.startswith("mcp__") and (
            "thread" in tl or "message" in tl or "mailbox" in tl
            or "mail" in tl or "gmail" in tl or "imap" in tl
            or any(p in tl for p in extra)))
        if is_mail:
            # ALLOW the fetch (blocking it is a catch-22 — the fetch is the only
            # way to obtain the mail text to anonymise), but inject a forceful
            # anonymise-first instruction the model sees with the result.
            _allow_with_context(
                "⚠️ Bubble Shield mail-guard : ce résultat contient des e-mails BRUTS, "
                "qui peuvent renfermer des données identifiantes d'un client "
                "(nom, e-mail, IBAN, téléphone…). Avant de lire, résumer, citer "
                "ou agir sur ce contenu, tu DOIS d'abord faire passer le texte de "
                "chaque message par l'outil `bubble_shield_anonymize_text`, puis ne "
                "travailler que sur la version anonymisée (jetons ⟦…⟧). Ne "
                "reproduis JAMAIS une valeur identifiante brute dans ta réponse. "
                "[mail-guard: " + tool_name + "]")
            return

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
            # Marker allow_paths/allow_extensions are documented as relative to
            # THIS marker's folder → resolve them against `root`, not process CWD.
            m_allow_paths = [q for q in (_norm(x, base=root) for x in mdata.get("allow_paths", [])) if q]
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

        # --- PRIMARY: per-path marker walk-up on paths extracted from the command.
        # This is the robust, cwd-INDEPENDENT mechanism. We pull every absolute/home
        # path-shaped token out of the command and run each through the EXACT SAME
        # `decide_block` walk-up the file-tool path uses (which correctly finds the
        # nearest marker walking UP from a concrete path, regardless of cwd). This
        # closes the proven exfil gap: `tesseract /a/b/Dossier/avis.jpg stdout` with
        # cwd=/Users/joris (an unrelated session root) now resolves the marker on
        # the FILE'S OWN ancestry instead of relying on cwd-anchored discovery.
        for p in _extract_command_paths(command, cwd):
            blocked, message = decide_block(p)
            if not blocked:
                continue
            # Honour a per-marker `block_bash:false` opt-out (the marker.example.json
            # documents block_bash as a folder-level setting; previously only the
            # GLOBAL config's block_bash was read, so a marker that set it false was
            # silently ignored). If THIS path's nearest marker explicitly disables
            # bash-blocking, respect it for this path (the read/write guard still
            # protects the file; the operator opted bash out deliberately). Absent
            # marker (global protected_folders) keeps the global block_bash gate.
            hit = _find_marker_root(p)
            if hit is not None and hit[1].get("block_bash") is False:
                continue
            _deny(f"{message}\n[Bubble Shield guard: commande shell touchant {p}]")
            return

        # --- DEFENSE-IN-DEPTH: the legacy cwd-anchored needle scan. Cheap, and it
        # still catches the cases the path extractor can't: RELATIVE-path commands
        # where cwd IS informative (e.g. `cat avis.pdf` run with cwd inside the
        # marked folder — there's no absolute token to extract, but cwd discovery
        # finds the marker). Kept as a secondary signal, no longer the ONLY one.
        home = os.path.expanduser("~")
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
                _deny(f"{g_message}\n[Bubble Shield guard: commande shell touchant {n}]")
                return

        # --- RESIDUAL-PATH POLICY (explicit decision, documented):
        # If we reach here, block_bash is true but nothing matched. What's covered
        # and what's left:
        #   COVERED by the PRIMARY walk-up (cwd-INDEPENDENT): any absolute/home path
        #     into a marked folder (the proven client exfil shape).
        #   COVERED by the PRIMARY walk-up (cwd-anchored): any slash-bearing relative
        #     path (`Dupont/avis.jpg`) resolved against cwd that lands under a marker.
        #   COVERED by the needle scan: commands that literally name a discoverable
        #     marker root by its absolute path.
        #   RESIDUAL (deliberately ALLOWED): a BARE filename with no slash
        #     (`cat avis.jpg`) whose cwd is itself inside a marked folder. The
        #     resolved path WOULD be under a marker, but extracting "avis.jpg" as a
        #     path candidate is indistinguishable from extracting every bare word/
        #     subcommand/flag-value in the command — emitting all of them would
        #     either over-deny benign commands (`cat readme`, `make build`) or
        #     require a real shell lexer + per-arg cwd-join we don't have. We accept
        #     this narrow residual gap rather than fail-closed-on-every-bare-word,
        #     which would brick routine shell use (`ls`, `git status`). The exposure
        #     is small: it requires cwd to ALREADY be inside the protected folder
        #     (an agent that deep in the dossier is the in-folder workflow the marker
        #     governs, and the file-tool Read path covers the same files), and it
        #     does NOT cover the dangerous absolute-path-from-an-unrelated-cwd case,
        #     which is now always denied. Documented in STATUS.md "block_bash cwd".
        _allow()
        return

    # --- File tools: check each candidate path ---
    for p in _candidate_paths(tool_name, tool_input, cwd):
        blocked, message = decide_block(p)
        if blocked:
            _deny(f"{message}\n[Bubble Shield guard: {p}]")
            return

    _allow()


if __name__ == "__main__":
    main()
