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

# Cowork's HUMAN-VIEWER tools — the "render a file to the human screen" path.
# These are NOT our tools; they belong to the Cowork platform. We exempt them for
# ONE specific, load-bearing workflow (companion to Finding #40): the agent works
# on TOKENS, `bubble_shield_write` restores the real values into a GUARDED file on
# disk (the #40 fix REFUSES the allow-listed `clean/`, so the restored file always
# lands where a later agent Read is blocked), and then the agent asks a Cowork
# viewer to render that restored file to the HUMAN. The clear data goes to the
# human UI; the agent never sees it. Without this exemption the guard's `mcp__.*`
# matcher gates the viewer's file_path/html_path on the protected path and DENIES
# it — which would break the only way to show the client their own real document.
#
# SAFETY INVARIANT (verified live, and the reason this is safe):
#   Each of these viewers RETURNS TO THE AGENT only a path or a short confirmation
#   — NEVER the file body:
#     - present_files  → returns only the path (no "État civil"/passport/address);
#     - create_artifact → returns only 'Artifact "…" created.';
#     - update_artifact → returns only 'Artifact "…" updated.'.
#   So they are structurally identical to our own sanctioned bubble_shield_read/
#   write: the human sees the content, the AGENT does not. Exempting them cannot
#   leak PII into the agent's context.
#
#   ⚠️ This exemption is valid ONLY while that invariant holds. If Cowork ever
#   changes any of these tools to return the file BODY to the agent, this becomes a
#   PII leak and the tool must be removed from this list. A future maintainer MUST
#   re-verify the return shape before trusting it.
#
# ACCEPTED, KNOWN CONSEQUENCE: the agent could point create_artifact/update_artifact
# `html_path` (or present_files) at a protected client file DIRECTLY (not a
# restored-token artifact), rendering raw client data onto the human panel. That is
# acceptable BY DESIGN: the guard protects the AGENT's context, not the human's
# screen — the human is allowed to view their own client's data. The safety
# boundary (nothing clear reaches the model) is preserved either way.
#
# Matched by SUFFIX (like _OWN_MCP_TOOL_SUFFIXES) so the opaque-prefixed form
# (mcp__<opaque-server-id>__present_files) is covered too. Suffix match is exact-
# tail, so a look-alike like `present_files_and_dump_body` does NOT match.
_COWORK_VIEWER_TOOL_SUFFIXES = ("present_files", "create_artifact", "update_artifact")


def _is_cowork_viewer(tool_name: str) -> bool:
    """True if `tool_name` is one of Cowork's exempt human-viewer tools (matched by
    exact suffix — see _COWORK_VIEWER_TOOL_SUFFIXES). These render a file to the
    HUMAN and return only a path/confirmation to the agent, so they are ALLOWED
    even on a protected path. The suffix must be preceded by the mcp `__` separator
    (or be the whole leaf) so we match a tool NAME segment, never a partial word."""
    if not tool_name.startswith("mcp__"):
        return False
    leaf = tool_name.rsplit("__", 1)[-1]
    return leaf in _COWORK_VIEWER_TOOL_SUFFIXES

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
    elif tool_name == "Grep":
        # Grep returns file CONTENT (matching lines) → a content-leak vector, so it
        # MUST stay blocked on a protected folder. `path` is its search root; gate it.
        add(tool_input.get("path"))
    elif tool_name == "Glob":
        # Glob returns NAMES ONLY (matching paths) — a listing, never any file
        # CONTENT. Listing filenames is the sanctioned discovery capability (Joris
        # approved: the agent may SEE folder/file NAMES so it can find the file to
        # work on; filenames may be PII but that's an accepted, deferred decision).
        # So Glob is SAFE to allow even on a protected folder: emit NO candidate,
        # which means decide_block is never consulted and the call falls through to
        # _allow(). This is the ONE native tool freed here — Read/Edit/Write/
        # NotebookEdit stay blocked (content), Grep stays blocked (content), and the
        # Bash branch is untouched (unblocking ls/cat/find there would need
        # verb-parsing and reopen the v1.20.1 content-exfil hole). See CHANGE 1.
        pass
    elif tool_name.startswith("mcp__"):
        # FIX 3 (P0-SEC-3): the hooks.json matcher runs the guard for EVERY mcp__*
        # tool, but historically only the 6 native tools above yielded candidates,
        # so any OTHER mcp__* file tool (e.g. a filesystem MCP server's
        # mcp__filesystem__read_file) fell through to _allow() — a silent leak.
        # Mail tools and *__bash tools are handled on their own code paths BEFORE
        # _candidate_paths is called, so reaching here means a generic MCP tool:
        # scan its input for path-shaped values and gate every one.
        #
        # Cowork's HUMAN-VIEWER tools (present_files / create_artifact /
        # update_artifact) are EXEMPT: they render the file to the human and return
        # only a path/confirmation to the agent (never the body), so they are safe
        # on a protected path — emit NO candidate, so decide_block is never consulted
        # and the call falls through to _allow(). This is the load-bearing "render
        # restored file to the human" path (companion to Finding #40). See
        # _COWORK_VIEWER_TOOL_SUFFIXES for the full rationale + safety invariant.
        if _is_cowork_viewer(tool_name):
            return out  # empty → allowed
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


# Bare shell WORDS with NO slash (`link`, `avis.txt`, `readme`). The command-path
# extractors above deliberately IGNORE these (emitting every bare word as a path
# would over-block routine shell use). But FINDING #20 shows one dangerous class:
# a bare token that is a SYMLINK (or, via cwd, any name) resolving INTO a protected
# folder while cwd is OUTSIDE it — `ln -s <protected>/f.txt link; cat link` leaks.
# We tokenise bare words here so the Bash branch can resolve each with realpath
# (follow symlinks) and gate ONLY the ones that land inside a protected folder.
# Kept intentionally loose (any non-flag, non-path shell word); the NARROW filter
# that prevents over-block lives at the call site (must resolve + land in a marker).
_BARE_WORD_RE = re.compile(r"""
    (?:^|[\s=:,;()<>&|"'`])          # a boundary before the word
    (                                # capture the bare word
      (?![-/~])                      # not a flag (-x) and not an abs/home path
      (?:\\.|[^\s'";`|&<>()/=])+     # word chars, NO slash (bare name only)
    )
""", re.VERBOSE)


def _extract_bare_word_tokens(command: str) -> list[str]:
    """Return bare (slash-free) word tokens from a shell command, de-escaped.

    Excludes flags (`-x`), absolute/home paths (handled elsewhere), and anything
    with a slash. These are CANDIDATES only — the caller must resolve each via
    `os.path.realpath(os.path.join(cwd, tok))` and keep it ONLY when the resolved
    real path exists and lands inside a protected/marked folder (FINDING #20).
    Also sweeps quoted slash-free spans (`cat "link"`)."""
    if not command:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(w: str) -> None:
        tok = re.sub(r"\\(.)", r"\1", w).strip()
        if not tok or "/" in tok or tok.startswith("-") or tok.startswith("~"):
            return
        if tok in seen:
            return
        seen.add(tok)
        out.append(tok)

    for m in _BARE_WORD_RE.finditer(command):
        _add(m.group(1))
    for q in re.findall(r"'([^']*)'|\"([^\"]*)\"", command):
        span = q[0] or q[1]
        if span and ("/" not in span):
            _add(span)
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


# --- FINDING #553 (D): SIMPLE LITERAL var-assignment splice of a mount path -----
# A leak can be assembled with NO `cd` and with the `/sessions/*/mnt/` literal
# NEVER appearing in the command text — by stashing the mount prefix in a simple
# literal shell variable, then splicing it into the read path:
#     S=/sessions/foo; cat "$S/mnt/Dropbox/clients/f.pdf"   → literal spliced from $S
#     DIR="/sessions/foo/mnt"; cat "$DIR/Dropbox/x.pdf"     → literal in $DIR
# There is no `cd` (so #553-C's unresolvable-cd gate never fires) and the literal
# `/sessions/*/mnt/` substring is absent from the command text (it lives in the
# variable), so `_mentions_session_mnt` / the mnt-token classifier both miss it.
#
# FIX: a variable RESOLVER pre-pass. Parse SIMPLE, LITERAL assignments made earlier
# IN THE SAME command (`VAR=<literal>`, bare or quoted, RHS a static string with no
# `$`, no `$(…)`, no backtick, no glob), build a {VAR: value} map, then substitute
# `$VAR` / `${VAR}` occurrences with their literal value. The RESOLVED command is
# passed to the EXISTING detection (mnt-token classification, path extraction,
# marker walk-up) so the hidden literal becomes visible and the existing literal-mnt
# gate denies it. This DUPLICATES NO decision logic — the resolver only makes the
# hidden literal visible.
#
# SUBSTITUTION IS FOR DETECTION ONLY: the original command is what runs; the
# resolved form is only what the guard inspects.
#
# SCOPE (do-not-over-block): only LITERAL RHS values are resolved. `VAR=$(cmd)`,
# `VAR=$OTHER` (indirection), `VAR=`glob → NOT resolved (left as-is; they stay
# covered by the #553-C unresolvable-cd/opaque gates, or are a deeper residual). If
# a substitution can't happen, the command falls through the existing gates
# unchanged, so a benign `S=/tmp; cat "$S/x"` resolves to `cat /tmp/x` (ALLOW) and
# an unresolved RHS creates no new block.
_ASSIGN_RE = re.compile(r"""
    (?:^|[;&|]|&&|\|\|)\s*             # a command-start boundary (start or separator)
    ([A-Za-z_][A-Za-z0-9_]*)          # (1) the VAR name
    =                                 # the =
    (                                 # (2) the RHS, up to an unquoted boundary
      "[^"]*"                         #   double-quoted run
      | '[^']*'                       #   single-quoted run
      | [^\s;&|<>()"'`]*              #   bare run (stops at whitespace / separators)
    )
""", re.VERBOSE)

# RHS values we REFUSE to resolve (not a static literal): contains a `$` (var/cmd
# subst), a backtick (cmd subst), or a glob metachar. Such a VAR is left unresolved.
_NONLITERAL_RHS_RE = re.compile(r"[$`*?\[\]{}]")


def _resolve_simple_var_assignments(command: str) -> str:
    """Resolve SIMPLE literal `VAR=value` assignments made earlier in the SAME
    command and substitute `$VAR` / `${VAR}` occurrences with their literal value.

    Returns a "resolved command" string used ONLY for guard DETECTION (never for
    execution). Only LITERAL RHS values are resolved (no `$`, `$(…)`, backtick, or
    glob in the RHS); a non-literal RHS leaves that VAR unresolved so the command
    falls through the existing gates unchanged. Idempotent-ish and bounded: a single
    left-to-right pass; each `$VAR` is replaced by the value assigned to the LATEST
    preceding literal assignment of that VAR (later assignments shadow earlier ones).

    Multi-var chains resolve because each simple literal assignment is captured, so
    `A=/sessions; B=foo; cat "$A/$B/mnt/x"` → `cat "/sessions/foo/mnt/x"`.
    """
    if not command or "=" not in command:
        return command
    var_map: dict[str, str] = {}
    for m in _ASSIGN_RE.finditer(command):
        name, raw_rhs = m.group(1), m.group(2)
        # strip one layer of surrounding quotes
        if len(raw_rhs) >= 2 and raw_rhs[0] in "\"'" and raw_rhs[-1] == raw_rhs[0]:
            value = raw_rhs[1:-1]
        else:
            value = raw_rhs
        if _NONLITERAL_RHS_RE.search(value):
            # non-literal RHS (var/cmd-subst/glob) → do NOT resolve; drop any stale
            # earlier literal binding so a later `$VAR` isn't mis-substituted with an
            # outdated literal value (the reassignment shadows it as unresolvable).
            var_map.pop(name, None)
            continue
        var_map[name] = value
    if not var_map:
        return command

    # Substitute ${VAR} and $VAR occurrences (longest-name-first is unnecessary
    # because $VAR matching is anchored by a non-word boundary after the name).
    def _sub(mm: "re.Match[str]") -> str:
        name = mm.group(1) or mm.group(2)
        return var_map.get(name, mm.group(0))

    # ${VAR}  OR  $VAR (VAR = identifier; the negative-lookahead stops $FOOBAR from
    # matching a $FOO binding). Only names in the map are replaced (else left as-is).
    var_ref_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
    return var_ref_re.sub(_sub, command)


# --- FINDING #553: `cd`-compound bypass of the mnt-alias fail-closed -----------
# The mnt-alias classification (`_iter_session_mnt_tokens`) and the relative-token
# resolution (`_extract_command_paths`) both anchor a RELATIVE token against
# `event.cwd`. That is only correct when `event.cwd` is already the effective
# directory. But a compound command can do the `cd` ITSELF:
#     cwd = /sessions/<s>                 (the untracked session ROOT)
#     cmd = cd /sessions/<s>/mnt/outputs && cat "../Dropbox/clients/f.pdf"
# The guard never parses `cd`, so `../Dropbox/...` joins against the session ROOT
# (`/sessions/<s>/../Dropbox/...` → `/sessions/Dropbox/...`) instead of the mount
# dir — it no longer matches `/sessions/*/mnt/...`, Fix C never fires, LEAK.
#
# FIX: parse a leading `cd X` / `cd X && cd Y && …` / `cd X; …` chain to compute
# the EFFECTIVE cwd, then resolve relative tokens + re-run the mnt-classification
# and marker walk-up against THAT effective cwd. Reuses the existing decision
# logic unchanged — we only correct the base the relative tokens resolve against.
_CD_SEP_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")


def _unwrap_cd_target(tok: str) -> str:
    """Strip surrounding quotes and de-escape a `cd` target token."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
        tok = tok[1:-1]
    else:
        # de-escape backslash-escaped chars (e.g. `cd cl\ ients`)
        tok = re.sub(r"\\(.)", r"\1", tok)
    return tok


def _effective_cwd_from_cd(command: str, cwd: str) -> str:
    """Compute the EFFECTIVE cwd after applying any leading/chained `cd` commands.

    Splits the command on shell separators (`&&`, `||`, `;`) and, for every
    segment that is a bare `cd <target>`, applies it to a running cwd (joining a
    relative target against the current running cwd, `..`/`.` collapsed lexically
    with `os.path.normpath` — we do NOT `realpath`, because the target may be a
    `/sessions/*/mnt/...` alias that doesn't exist on the host and must stay in
    the alias namespace to be classified). A `cd` with no argument, with `-`, or
    with options/globs/vars we can't resolve statically STOPS the walk (we can't
    know the resulting cwd → keep the last known-good, conservative). Returns the
    effective cwd string; falls back to the original `cwd` if no `cd` applies.

    This is intentionally conservative and SIDE-EFFECT-FREE: it only reads the
    command string. It is used solely to give the EXISTING relative-token and
    mnt-alias logic the correct base to resolve against.
    """
    if not command or "cd" not in command:
        return cwd
    eff = cwd or ""
    for seg in _CD_SEP_RE.split(command):
        seg = seg.strip()
        if not seg:
            continue
        # Only a segment whose FIRST word is `cd` moves the cwd. Any other command
        # (cat, grep, echo…) doesn't change cwd; keep walking to catch a later `cd`.
        parts = seg.split(None, 1)
        if parts[0] != "cd":
            continue
        if len(parts) == 1:
            # bare `cd` → HOME; can't resolve to a mount reliably. Stop: treat the
            # remaining relative tokens as un-anchored (they'll be handled by the
            # absolute-token paths / needle scan, not mis-anchored against a mount).
            return eff
        target = _unwrap_cd_target(parts[1].strip())
        # A `cd` target that itself carries a separator's tail shouldn't happen
        # (we already split), but guard against embedded spaces/args: take the
        # first whitespace-delimited token of the (unwrapped) target.
        if target[:1] not in ("/", "~", ".") and "/" not in target:
            # single bare segment like `cd Dropbox` — a relative dir move; keep it.
            pass
        else:
            # take only the leading path token if extra args slipped in
            target = target.split()[0] if target.split() else target
        if target in ("-",) or target.startswith("$") or _GLOB_META_RE.search(target):
            # `cd -`, `cd $VAR`, `cd glob*` → can't resolve statically. Stop the
            # walk at the last known-good cwd (conservative; do not guess).
            return eff
        target = os.path.expanduser(target)
        if os.path.isabs(target):
            eff = os.path.normpath(target)
        else:
            eff = os.path.normpath(os.path.join(eff, target)) if eff else target
    return eff


# --- FINDING #553 (B): cwd-HIDING construct + mnt token → fail-close the CLASS --
# The primary #553 fix parses a LEADING/CHAINED `cd X && …` to compute the
# effective cwd, so a *resolvable, non-hidden* `cd` is handled precisely. But the
# `cd` can be HIDDEN from that lexical parse inside a construct the guard cannot
# cleanly resolve — a subshell `(...)`, a nested shell (`bash -c "…"`), `pushd`,
# `eval`, or a command substitution `$(…)`/backticks. In every such case the
# effective-cwd walk never sees the inner `cd`, so a relative read that lands in a
# protected mount evades the mnt-alias fail-closed:
#     (cd /sessions/<s>/mnt && cat "Dropbox/clients/f.pdf")      → LEAK
#     bash -c "cd /sessions/<s>/mnt && cat Dropbox/clients/f.pdf" → LEAK
#     pushd /sessions/<s>/mnt && cat "Dropbox/clients/f.pdf"      → LEAK
#     eval "cd /sessions/<s>/mnt && cat Dropbox/clients/f.pdf"    → LEAK
#
# DECISION (Joris approved, option B): do NOT chase each construct with a parser.
# FAIL-CLOSE on the CLASS. Rule:
#   If a Bash command contains a cwd-HIDING / cwd-CHANGING construct the guard
#   cannot cleanly resolve AND the command ALSO contains ANY `/sessions/*/mnt/`
#   token (literal, ANYWHERE in the string) → DENY the whole command.
#
# ACCEPTED over-block (Joris approved): within a hiding construct we CANNOT prove
# a `mnt/outputs` reference stays in outputs (the construct could `cd` elsewhere),
# so ANY `/sessions/*/mnt/` token — EVEN an infra one — inside a hiding construct
# → DENY. A hiding construct with NO `/sessions/*/mnt/` token at all → ALLOW (we
# do NOT over-block `bash -c "echo hi"` or `(ls /tmp)`).
#
# The literal-token scan is deliberately SUBSTRING-based (not cwd-anchored): inside
# a hiding construct we can't anchor a relative token, but any absolute alias
# reference still carries the literal `/sessions/<name>/mnt/` prefix, and a relative
# read only leaks when paired with a `cd` INTO the mount whose alias prefix is
# itself present in the command. If NO literal `/sessions/*/mnt/` substring appears
# anywhere, there is nothing for the hiding construct to escape into → ALLOW.
# Match `/sessions/<name>/mnt` as a whole segment: `/mnt` must be followed by a
# `/` (a deeper path), OR a token boundary (whitespace, quote, separator, or end).
# This catches BOTH the absolute-alias read (`/sessions/x/mnt/Dropbox/f`) AND the
# `cd` target with no trailing slash (`cd /sessions/x/mnt && …`).
_SESSION_MNT_SUBSTR_RE = re.compile(r"/sessions/[^/\s'\"]+/mnt(?:/|[\s'\";&|)`>]|$)")

# cwd-HIDING / cwd-CHANGING constructs the guard cannot cleanly resolve. Presence
# of ANY of these makes the effective-cwd computation unreliable, so paired with a
# mnt token we fail closed. Detected by cheap, over-inclusive patterns (a false
# positive only costs an over-block, and ONLY when an mnt token is ALSO present):
#   - subshell `(...)`                        — a bare `(` group
#   - nested shell `bash -c` / `sh -c` / `zsh -c` (any `<shell> -c`)
#   - `pushd` / `popd`                        — directory-stack moves
#   - `eval`                                  — re-parses a built string
#   - command substitution `$(...)` / backticks
#   - `cd -`                                  — to OLDPWD (unknowable statically)
#   - `cd $VAR` / `cd "$..."` / `cd ${...}`   — env-var target
#   - `cd $(...)` / `cd `...``                — command-substitution target
#   - `cd <glob>`                             — a `cd` target with a glob metachar
_HIDING_CONSTRUCT_RES = (
    re.compile(r"\("),                                   # subshell group open
    re.compile(r"\$\("),                                 # $(...) command subst
    re.compile(r"`"),                                    # backtick command subst
    re.compile(r"(?:^|[\s;&|])(?:ba|z)?sh\s+-[a-zA-Z]*c\b"),  # bash/sh/zsh -c
    re.compile(r"(?:^|[\s;&|])pushd\b"),                 # pushd
    re.compile(r"(?:^|[\s;&|])popd\b"),                  # popd
    re.compile(r"(?:^|[\s;&|])eval\b"),                  # eval
    re.compile(r"(?:^|[\s;&|])cd\s+-(?:\s|;|&|\||$)"),   # cd -
    re.compile(r"(?:^|[\s;&|])cd\s+[\"']?\$"),           # cd $VAR / cd "$..." / cd ${...}
    re.compile(r"(?:^|[\s;&|])cd\s+[^\s;&|]*[*?\[\]{}]"),  # cd <glob-target>
)


def _has_hiding_construct(command: str) -> bool:
    """True if `command` contains a cwd-HIDING / cwd-CHANGING construct the guard
    cannot cleanly resolve (subshell, nested shell, pushd/popd, eval, command
    substitution, `cd -`/`cd $VAR`/`cd $(...)`/`cd <glob>`). Over-inclusive by
    design — a false positive only over-blocks, and ONLY when an mnt token is also
    present (see the CLASS gate). The already-handled resolvable `cd X && …` (no
    metachar target, no separator-hiding) is NOT flagged here."""
    if not command:
        return False
    return any(rx.search(command) for rx in _HIDING_CONSTRUCT_RES)


def _mentions_session_mnt(command: str) -> bool:
    """True if a literal `/sessions/<name>/mnt/` substring appears ANYWHERE in the
    command (cwd-INDEPENDENT). This is the token a hiding construct could escape
    into; its mere presence alongside a hiding construct triggers the fail-close."""
    return bool(command) and bool(_SESSION_MNT_SUBSTR_RE.search(command))


# --- FINDING #553 (C): UNRESOLVABLE cd + relative read → fail-close (no literal) -
# The #553-B CLASS gate above only fires when a hiding construct is paired with a
# LITERAL `/sessions/*/mnt/` substring. That literal is DEFEATED when the mount
# path is built from a variable / command-substitution / decoded blob so the token
# never appears in the command text:
#     cd "$SESS/mnt" && cat "Dropbox/clients/f.pdf"          → literal absent → LEAK
#     p=$(printf /x/y); cd $p && cat "Dropbox/clients/f.pdf" → literal absent → LEAK
#     cd $(echo /whatever) && cat relative/f.pdf             → literal absent → LEAK
#     eval "$(echo <b64> | base64 -d)"                       → decoded cd invisible → LEAK
#
# KEY INSIGHT (Joris approved, option 2): a `cd` whose target the guard CANNOT
# RESOLVE (env var `$VAR`/`${VAR}`, `$(…)`/backtick command-substitution, `cd -`,
# glob target, or a decoded `eval "$(…)"`) is ALREADY a hiding construct. In a
# Cowork session the guard cannot know where that `cd` lands; if it might land in a
# mounted protected folder, a SUBSEQUENT RELATIVE file read would hit protected
# content. So: UNRESOLVABLE cd  +  a relative (cwd-dependent) file read  → DENY,
# EVEN with no literal `/sessions/*/mnt/` token. An ABSOLUTE read after the same cd
# is cwd-INDEPENDENT (its target is fixed regardless of the unknown cwd) → ALLOW —
# only relative reads are endangered by an unknown cwd.
#
# Detectors, deliberately narrow to avoid bricking normal shell use:
#   _has_unresolvable_cd  → a `cd` whose target we can't statically resolve to a
#     concrete path: `cd $VAR` / `cd "$..."` / `cd ${...}` / `cd $(...)` /
#     `cd `...`` / `cd -` / `cd <glob>`. (A plain `cd /tmp` or `cd sub/dir` is
#     RESOLVABLE and is NOT flagged — the effective-cwd walk handles it precisely.)
#   _has_opaque_eval      → `eval "$(...)"` / `eval $(...)` / ``eval `...``` — the
#     decoded command text is INVISIBLE, so we can't prove it contains no relative
#     read. Judgment call (documented): fail-close on opaque eval regardless of a
#     visible read, since the hidden text could read anything relative.
#   _has_relative_read    → a subsequent file access on a RELATIVE / cwd-dependent
#     path: a known content-reading verb (cat/head/tail/…) whose arg is a relative
#     token, OR any relative slash-bearing path token anywhere. Absolute (`/…`,
#     `~/…`) reads do NOT count (cwd-independent).

# A `cd` target we can't statically resolve to a concrete path. Mirrors (a superset
# of) the `cd`-hiding entries in _HIDING_CONSTRUCT_RES, but SCOPED to `cd` itself so
# a benign subshell/`$()` elsewhere does not, on its own, count as an unresolvable cd.
_UNRESOLVABLE_CD_RES = (
    re.compile(r"(?:^|[\s;&|(])cd\s+[\"']?\$"),            # cd $VAR / cd "$..." / cd ${...} / cd $(...)
    re.compile(r"(?:^|[\s;&|(])cd\s+[\"']?`"),            # cd `...`  (backtick target)
    re.compile(r"(?:^|[\s;&|(])cd\s+-(?:\s|;|&|\||$)"),    # cd -  (OLDPWD, unknowable)
    re.compile(r"(?:^|[\s;&|(])cd\s+[^\s;&|]*[*?\[\]{}]"), # cd <glob-target>
)

# `eval` of a command-substitution whose decoded text we can't see. Fail-close.
_OPAQUE_EVAL_RE = re.compile(r"(?:^|[\s;&|(])eval\s+[\"']?(?:\$\(|`)")

# Content-reading shell verbs. A relative-path arg to any of these after an
# unresolvable cd is the dangerous read. Kept broad on the read side (missing a
# reader is a hole; an extra verb only matters when an unresolvable cd is ALSO
# present, i.e. exactly the obfuscation shape).
_READ_VERBS = (
    "cat", "head", "tail", "less", "more", "bat", "nl", "tac",
    "base64", "xxd", "od", "hexdump", "strings", "grep", "egrep", "fgrep",
    "rg", "ag", "awk", "sed", "cut", "sort", "uniq", "wc", "tr", "col",
    "python", "python3", "perl", "ruby", "node", "cp", "mv", "install",
    "tesseract", "pdftotext", "pdfimages", "file", "stat", "open", "dd",
)


def _has_unresolvable_cd(command: str) -> bool:
    """True if `command` contains a `cd` whose target the guard CANNOT statically
    resolve to a concrete path (env-var / command-substitution / backtick / `cd -`
    / glob target). A plain resolvable `cd /abs` or `cd rel/dir` is NOT flagged —
    the effective-cwd walk handles those precisely. Over-inclusive is safe: this
    only bites when a subsequent RELATIVE read is ALSO present (the CLASS-C gate)."""
    if not command or "cd" not in command:
        return False
    return any(rx.search(command) for rx in _UNRESOLVABLE_CD_RES)


def _has_opaque_eval(command: str) -> bool:
    """True if `command` contains `eval "$(...)"` / `eval $(...)` / ``eval `...``` —
    an eval whose decoded content is invisible to the guard. Documented judgment
    call: fail-close, because the hidden text could perform a relative read into a
    mount and we cannot inspect it. A plain `eval "echo hi"` (literal, no command
    substitution) is NOT flagged here (it is visible; #553-B handles it if it names
    an mnt token)."""
    return bool(command) and bool(_OPAQUE_EVAL_RE.search(command))


def _has_relative_read(command: str) -> bool:
    """True if `command` performs a file access on a RELATIVE (cwd-dependent) path.

    Two signals, either suffices:
      1) a known content-reading verb (_READ_VERBS) whose FIRST non-flag argument is
         a relative token (not `/…`, not `~/…`, not a pure option) — catches
         `cat Dropbox/x`, `cat x`, `grep foo notes.txt`, `head client/f`;
      2) ANY relative slash-bearing path token anywhere (`_REL_TOKEN_RE`) — catches
         redirections / arg positions the verb-scan misses (`< notes/secret`).

    ABSOLUTE / home reads (`/…`, `~/…`) are cwd-INDEPENDENT and do NOT count: an
    unresolvable `cd` cannot change where an absolute path points. `ls`, `pwd`,
    `echo`, `date` are not readers → emit nothing (no over-block).

    Heuristic, err toward detection: when an unresolvable cd is present, ANY
    relative file read is treated as the obfuscation shape and denied."""
    if not command:
        return False
    # 1) relative slash-bearing path token anywhere (Dropbox/x, notes/secret.txt).
    if _REL_TOKEN_RE.search(command):
        return True
    # 2) a read-verb applied to a relative bare arg (cat x, grep foo bar.txt).
    #    Split on shell separators so we inspect each simple command's head+args.
    for seg in re.split(r"(?:&&|\|\||;|\||\n)", command):
        toks = seg.strip().split()
        if not toks:
            continue
        # strip a leading env-assignment prefix (FOO=bar cat x)
        i = 0
        while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
            i += 1
        if i >= len(toks):
            continue
        verb = os.path.basename(toks[i])
        if verb not in _READ_VERBS:
            continue
        # inspect the args for a relative (cwd-dependent) path-ish token
        for arg in toks[i + 1:]:
            a = arg.strip("'\"")
            if not a or a.startswith("-"):
                continue
            if a.startswith("/") or a.startswith("~"):
                # absolute/home read → cwd-independent → not endangered.
                # keep scanning: a later arg could still be relative.
                continue
            if a.startswith("$") or a.startswith("`") or "$(" in a:
                # an unresolvable arg itself — treat as relative-ish (unknown).
                return True
            # a bare or slash-bearing relative token used as a read target.
            # (`secret`, `Dropbox/x`, `client/notes.txt`) → cwd-dependent read.
            return True
    return False


# --- Cowork sandbox-mount-alias handling (FIX B / FIX C) -----------------------
# Cowork runs the agent in a sandbox VM and mounts each folder the user connected
# to the session under a DYNAMIC per-session alias:
#     /sessions/<random-session-name>/mnt/<subpath>
# e.g. `/sessions/pensive-dreamy-goldberg/mnt/clients/note.txt`. The <session-name>
# is random per session (cannot be hardcoded). The HOST guard matches command
# strings against REAL Mac paths, so an alias token like the above resolves to no
# marker on the Mac (`/sessions/...` doesn't exist there) and the legacy needle
# scan (Mac-path needles only) never matches the alias prefix → the command was
# ALLOWED and PII leaked in clear. This is a mount-NAMESPACE mismatch, distinct
# from the cwd-exfil path-EXTRACTION fix.
#
# The mount exposes a protected folder's CONTENTS under `mnt/<basename>/…`, so a
# mount-relative path whose first segment is a protected folder's basename is
# INSIDE that protected folder → DENY (Fix B). Any *other* `mnt/<X>` token we
# can't classify is failed CLOSED (Fix C), EXCEPT the known Cowork infra mounts
# below, which are the agent's own workspace and must stay usable.
_SESSION_MNT_RE = re.compile(r"^/sessions/[^/]+/mnt/(?P<rest>.+)$")

# Cowork infra mounts observed under /sessions/<name>/mnt/ in the live probe:
#   .claude, .remote-plugins, outputs, uploads  → infrastructure (agent's own
#     workspace: its config, plugins, work outputs, and user uploads).
#   clients (in the probe) → the USER-SELECTED protected folder (Fix B denies it
#     via its basename anyway; it is NOT infra).
# We ALLOW these infra mounts so the fail-closed backstop (Fix C) doesn't brick
# the agent's own workspace, and fail-closed on every OTHER unknown `mnt/<X>`
# (we cannot prove such a subtree is not a protected user folder → err toward
# blocking within the mnt/ mount tree, which is exactly where user folders mount).
_COWORK_INFRA_MNT = ("outputs", "uploads", ".claude", ".remote-plugins")


def _iter_session_mnt_tokens(command: str, cwd: str = "") -> "list[tuple[str, str]]":
    """Extract `/sessions/<name>/mnt/<rest>` tokens from a shell command.

    Returns a list of (full_token, rest) pairs where `rest` is the mount-relative
    path (e.g. "clients/clean/note.txt"). Reuses the SAME tokenisation as
    `_extract_command_paths`: absolute-token regex, relative-token regex, and a
    quoted-span sweep — so quoted/escaped alias paths are caught. These tokens
    were previously IGNORED downstream (they resolve to no marker on the Mac);
    here we classify them explicitly against the mount namespace instead.

    FINDING #19 (`..`-traversal into a mounted folder): a RELATIVE token like
    `../Dropbox/x` does NOT match `_SESSION_MNT_RE` on its own, but when the
    session cwd is itself a mount path (`/sessions/foo/mnt/outputs`), joining +
    `os.path.normpath` collapses the `..` into `/sessions/foo/mnt/Dropbox/x` — a
    real mnt-alias path that MUST re-enter the Fix-C classification. So before
    matching, we normalise each relative token against `cwd` (collapsing `..`),
    and match the normalised absolute form. Absolute tokens are normpath'd too so
    an embedded `..` (`/sessions/x/mnt/outputs/../clients/f`) is collapsed as well.
    """
    if not command:
        return []
    raw_candidates: list[str] = [m.group(1) for m in _ABS_TOKEN_RE.finditer(command)]
    raw_candidates += [m.group(1) for m in _REL_TOKEN_RE.finditer(command)]
    for q in re.findall(r"'([^']*)'|\"([^\"]*)\"", command):
        span = q[0] or q[1]
        if span and ("/" in span):
            raw_candidates.append(span)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        # Un-escape backslash-escaped chars (e.g. "/sessions/x/mnt/cl\ ients/…").
        tok = re.sub(r"\\(.)", r"\1", raw).strip()
        if not tok:
            continue
        # FINDING #19: resolve the token to an absolute, `..`-collapsed form so a
        # relative traversal into the mount namespace is caught by Fix C. We use
        # os.path.normpath (NOT realpath) here on purpose: `/sessions/...` does not
        # exist on the host, so realpath can't follow it; normpath is a pure lexical
        # collapse of `..`/`.` that keeps us in the alias namespace to classify.
        norm = os.path.expanduser(tok)
        if not os.path.isabs(norm):
            if not cwd:
                # No cwd → can't anchor a relative token into the mnt namespace.
                # Only an already-absolute token can be an mnt alias, so skip.
                continue
            norm = os.path.join(cwd, norm)
        norm = os.path.normpath(norm)
        m = _SESSION_MNT_RE.match(norm)
        if not m:
            continue
        rest = m.group("rest")  # mount-relative path; normalised above
        if norm in seen:
            continue
        seen.add(norm)
        out.append((norm, rest))
    return out


def _mnt_first_segment(rest: str) -> str:
    """First path segment of a mount-relative path ("clients/x/y" → "clients")."""
    return rest.strip("/").split("/", 1)[0] if rest.strip("/") else ""


def _ext_exempt(p: Path, exts: tuple) -> bool:
    """True if `p`'s extension is on the `exts` allow-list (case-insensitive).
    Matches both the full multi-suffix tail (`.anon.txt`) and any single trailing
    extension. Pure — module-level so both main() and the write gate share it."""
    if not exts:
        return False
    if "".join(p.suffixes).lower().endswith(exts):
        return True
    name_lower = p.name.lower()
    return any(name_lower.endswith(e) for e in exts)


def decide_block_for_path(path, config: dict | None = None) -> "tuple[bool, str]":
    """SINGLE SOURCE OF TRUTH for "would a built-in Read/Bash of `path` be BLOCKED?"

    Returns (blocked, message). A path is blocked if it sits under a folder
    carrying a `.bubble-shield.json` marker OR under a global `protected_folders`
    entry, unless it is exempted by that folder's `allow_paths` / `allow_extensions`
    (marker overrides fall back to the global defaults when unset). The marker file
    itself is NEVER blocked (it is our own metadata, no PII).

    This is the module-level function that BOTH the guard hook (`main()` →
    `decide_block`) and the MCP write gate (`bubble_shield_mcp._path_is_guarded`)
    call, so the two can never drift. `path` may be a str or a Path; it is
    expanduser'd + resolved here. Pass a pre-loaded `config` (as `main()` does, to
    reuse the config it already read) or let it load the global config itself.
    """
    p = path if isinstance(path, Path) else Path(os.path.expanduser(str(path)))
    try:
        p = p.resolve()
    except Exception:
        pass

    cfg = config if config is not None else _load_config()
    protected = [q for q in (_norm(x) for x in cfg.get("protected_folders", [])) if q]
    g_allow_paths = [q for q in (_norm(x) for x in cfg.get("allow_paths", [])) if q]
    g_allow_exts = tuple(e.lower() for e in cfg.get("allow_extensions", []) if e)
    g_message = cfg.get("message_fr") or DEFAULT_MESSAGE

    # The marker file itself is never blocked — it's our own metadata (no PII),
    # and onboarding / the skill must be able to read & write it.
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

    # --- INPUT ROBUSTNESS (FINDING #553-B, part 2): NORMALISE/COERCE the event
    # shape BEFORE any decision logic. The harness legitimately passes some events
    # in a structured shape (tool_input as a LIST, cwd as an INT, command as a
    # LIST). Historically these hit the blanket-except and fail-closed with a scary
    # "🔒 erreur interne du guard", blocking the user's REAL work. We coerce them to
    # the expected types so they reach a NORMAL decision instead. SECURITY: this is
    # coerce-THEN-decide, never coerce-to-allow — a command passed as a list that
    # touches a protected path must STILL be denied (see below: the list is joined
    # into a scannable command string, not dropped). The blanket-except in main()
    # stays as the ultimate backstop for a TRULY unexpected error.
    tool_name = tool_name if isinstance(tool_name, str) else str(tool_name or "")

    # tool_input: anything that isn't a dict → treat as empty dict. A list/str/None
    # tool_input carries no path-bearing keys we can gate, so {} is the safe
    # normalisation (the Bash-command path below re-derives `command` defensively).
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # cwd: coerce to str. An INT/None/other cwd must not crash the join logic.
    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        cwd = "" if cwd is None else str(cwd)
    if not cwd:
        cwd = os.getcwd()

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

    def decide_block(p: Path) -> tuple[bool, str]:
        """Return (blocked, message) for path `p`. DELEGATES to the module-level
        `decide_block_for_path` — the SINGLE source of truth also called by the
        MCP write gate — passing the config this session already loaded so the two
        can never drift. (This nested wrapper is kept so the call sites below read
        unchanged; it just forwards `p` + `cfg`.)"""
        return decide_block_for_path(p, cfg)

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
        # ROBUSTNESS (FINDING #553-B, part 2): a structured command may arrive as a
        # LIST (["cat", "/path"]) rather than a str. Join it with spaces so it is
        # still SCANNED (coerce-then-decide, never coerce-to-allow: a list touching
        # a protected path must still be denied). Any non-str/non-list → "".
        if isinstance(command, (list, tuple)):
            command = " ".join(str(x) for x in command)
        elif not isinstance(command, str):
            command = "" if command is None else str(command)

        # FINDING #553-D: resolve SIMPLE literal `VAR=value` assignments made earlier
        # in this same command and splice their values into `$VAR`/`${VAR}` refs, so a
        # mount literal hidden in a variable (`S=/sessions/foo; cat "$S/mnt/…"`)
        # becomes VISIBLE to every gate below. DETECTION ONLY — the ORIGINAL command
        # runs; this resolved form is solely what the guard inspects. A non-literal
        # RHS ($(…)/backtick/var/glob) is left unresolved, so benign/unresolved cases
        # fall through the existing gates unchanged (no over-block). See
        # _resolve_simple_var_assignments.
        command = _resolve_simple_var_assignments(command)

        # FINDING #553: a compound command can `cd` INTO a mount before reading a
        # relative token, which would otherwise anchor against the untracked
        # session cwd and bypass the mnt-alias fail-closed. Compute the EFFECTIVE
        # cwd after applying any leading/chained `cd`, and resolve relative tokens
        # (path extraction, bare-word, and mnt-alias classification) against THAT.
        # For an absolute path or the mac-path scan, `cwd` is irrelevant, so this
        # is purely additive — it only corrects the base of RELATIVE resolution.
        eff_cwd = _effective_cwd_from_cd(command, cwd)

        # --- FINDING #553 (B): cwd-HIDING construct + mnt token → fail-close CLASS.
        # If the command hides the effective `cd` from our lexical parse (subshell,
        # bash/sh/zsh -c, pushd/popd, eval, $()/backticks, cd -/cd $VAR/cd $(…)/
        # cd <glob>) AND ALSO references any `/sessions/*/mnt/` token, we cannot
        # prove where a relative read lands — DENY the whole command. This runs
        # BEFORE the finer-grained resolvable-cd logic so a hidden construct can
        # never fall through to an ALLOW. A hiding construct with NO mnt token, or
        # an mnt token with NO hiding construct, is handled by the paths below (no
        # over-block). See _has_hiding_construct / _mentions_session_mnt.
        if _has_hiding_construct(command) and _mentions_session_mnt(command):
            _deny(
                f"{g_message}\n[Bubble Shield guard: construction masquant le "
                "répertoire courant (sous-shell / bash -c / pushd / eval / $(…) / "
                "cd non résoluble) combinée à une référence de montage sandbox "
                "/sessions/*/mnt/ — le guard ne peut pas prouver où atterrit la "
                "lecture, bloqué par sécurité (fail-closed sur la classe #553-B).]")
            return

        # --- FINDING #553 (C): UNRESOLVABLE cd + RELATIVE read → fail-close.
        # #553-B above needs a LITERAL `/sessions/*/mnt/` token; that literal is
        # DEFEATED when the mount path is built from a var / `$(…)` / decoded blob.
        # KEY INSIGHT: an unresolvable `cd` target is ALREADY a hiding construct —
        # the guard can't know where it lands. If it MIGHT land in a mounted
        # protected folder and a SUBSEQUENT RELATIVE read would then hit protected
        # content, the only safe posture is DENY, EVEN with no literal mnt token.
        # An ABSOLUTE read after the same cd is cwd-INDEPENDENT (its target is fixed
        # regardless of the unknown cwd) → NOT denied here. An unresolvable cd with
        # NO relative read (`cd "$D" && echo done`, `cd "$D" && ls`) → NOT denied.
        # Runs AFTER the #553-B literal gate and BEFORE the resolvable-cd path logic
        # so the obfuscated shape can never fall through to an ALLOW. See
        # _has_unresolvable_cd / _has_relative_read / _has_opaque_eval.
        if _has_opaque_eval(command):
            # `eval "$(…)"` — decoded text invisible; can't prove it performs no
            # relative read. Documented judgment call: fail-close on opaque eval.
            _deny(
                f"{g_message}\n[Bubble Shield guard: eval d'une substitution de "
                "commande opaque (eval \"$(…)\") — le contenu décodé est invisible "
                "pour le guard et pourrait lire un fichier relatif dans un montage "
                "protégé, bloqué par sécurité (fail-closed #553-C).]")
            return
        if _has_unresolvable_cd(command) and _has_relative_read(command):
            _deny(
                f"{g_message}\n[Bubble Shield guard: `cd` vers une cible non "
                "résoluble (variable / $(…) / backtick / cd - / glob) suivie d'une "
                "lecture de fichier par chemin RELATIF — le guard ne peut pas "
                "prouver que la lecture n'atterrit pas dans un montage protégé, "
                "bloqué par sécurité (fail-closed #553-C). Une lecture par chemin "
                "ABSOLU après le même cd resterait autorisée.]")
            return

        # --- PRIMARY: per-path marker walk-up on paths extracted from the command.
        # This is the robust, cwd-INDEPENDENT mechanism. We pull every absolute/home
        # path-shaped token out of the command and run each through the EXACT SAME
        # `decide_block` walk-up the file-tool path uses (which correctly finds the
        # nearest marker walking UP from a concrete path, regardless of cwd). This
        # closes the proven exfil gap: `tesseract /a/b/Dossier/avis.jpg stdout` with
        # cwd=/Users/joris (an unrelated session root) now resolves the marker on
        # the FILE'S OWN ancestry instead of relying on cwd-anchored discovery.
        for p in _extract_command_paths(command, eff_cwd):
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

        # --- FINDING #20: bare-name SYMLINK (or cwd-relative bare name) into a
        # protected folder. The path extractors above skip slash-free tokens on
        # purpose (emitting every bare word would over-block routine shell use), so
        # `ln -s <protected>/f.txt link; cat link` from an UNRELATED cwd bypassed the
        # guard and leaked the protected file. Here we handle bare words NARROWLY:
        # for each bare token we join it to cwd and `os.path.realpath` it (following
        # symlinks to the real target); we DENY only when the resolved real path
        #   (a) EXISTS on disk,
        #   (b) lands inside a protected/marked folder (decide_block says blocked),
        #   (c) AND cwd is NOT itself inside that same protected root.
        # (c) is what preserves the DELIBERATE in-folder residual: `cd protected &&
        # cat avis.txt` has cwd INSIDE the marker root, so it stays ALLOWED (the
        # documented, accepted residual). A bare word that doesn't resolve, or
        # resolves OUTSIDE any protected folder (`cat readme`, `ls`, a benign
        # `link2 -> /tmp/x`), emits nothing → no over-block.
        if cwd:
            try:
                cwd_real = os.path.realpath(cwd)
            except Exception:
                cwd_real = cwd
            for tok in _extract_bare_word_tokens(command):
                try:
                    joined = os.path.join(cwd, os.path.expanduser(tok))
                    real = os.path.realpath(joined)   # follows symlinks
                except Exception:
                    continue
                # Must resolve to something that actually exists (a live symlink
                # target or a real file); a non-existent bare word is not a leak.
                if not os.path.exists(real):
                    continue
                rp = Path(real)
                blocked, message = decide_block(rp)
                if not blocked:
                    continue
                # (c) cwd itself inside the SAME protected root → deliberate
                # in-folder residual, keep ALLOWED. Determine the protected root of
                # the resolved target and check whether cwd_real lives under it.
                root = None
                hit = _find_marker_root(rp)
                if hit is not None:
                    root = hit[0]
                else:
                    for prot in protected:
                        if _is_within(rp, prot):
                            root = prot
                            break
                if root is not None and _is_within(Path(cwd_real), root):
                    # cwd is INSIDE the protected folder → in-folder residual, allow.
                    continue
                # Honour a per-marker block_bash:false opt-out on the target.
                if hit is not None and hit[1].get("block_bash") is False:
                    continue
                _deny(
                    f"{message}\n[Bubble Shield guard: nom nu '{tok}' résolu vers "
                    f"{real} dans un dossier protégé (symlink/relatif depuis un cwd "
                    "hors dossier) — bloqué par sécurité.]")
                return

        # --- FIX B + FIX C: Cowork sandbox-mount-alias namespace -------------------
        # The PRIMARY walk-up above never fires for `/sessions/<name>/mnt/…` tokens:
        # that namespace doesn't exist on the Mac, so `decide_block` finds no marker
        # and ALLOWS — the confirmed live leak. Here we classify those alias tokens
        # explicitly against the mount namespace instead of the host filesystem.
        #
        # Build the set of protected-folder BASENAMES reachable this session:
        #   - global `protected_folders` config entries (resolved + raw), and
        #   - marker-carrying roots discovered near the session cwd.
        # The mount exposes each such folder's contents under `mnt/<basename>/…`.
        protected_basenames: set[str] = set()
        for prot in protected:
            if prot.name:
                protected_basenames.add(prot.name)
        for raw in protected_raw:
            bn = os.path.basename(str(raw).rstrip("/"))
            if bn:
                protected_basenames.add(bn)
        marker_roots = _discover_marker_roots(cwd)
        for mroot in marker_roots:
            if mroot.name:
                protected_basenames.add(mroot.name)
        # NOTE: Fix C below is now UNCONDITIONAL on the mnt/ namespace (no
        # `any_protection` gate). The host cannot see markers on the sandbox-FS
        # inode, so it must fail closed on every non-infra mount subtree even when
        # no protected_folders/markers are known to the host. See Fix C comment.

        # Classify mnt-alias tokens against BOTH the original event.cwd AND the
        # `cd`-derived effective cwd, and DENY if EITHER anchoring lands a relative
        # token in a protected/non-infra mount (fail-closed union). This closes
        # FINDING #553 (`cd` into a mount before a relative read) without letting a
        # trailing/out-of-order `cd` re-anchor a would-be-blocked read into infra.
        mnt_tokens: "list[tuple[str, str]]" = []
        _seen_mnt: set[str] = set()
        for base_cwd in (cwd, eff_cwd):
            for tok, rest in _iter_session_mnt_tokens(command, base_cwd):
                if tok in _seen_mnt:
                    continue
                _seen_mnt.add(tok)
                mnt_tokens.append((tok, rest))
        for tok, rest in mnt_tokens:
            first = _mnt_first_segment(rest)   # e.g. "clients" from "clients/x/y"
            if not first:
                continue
            # FIX B — the alias's first mnt segment IS a protected folder's
            # basename → this token refers INTO that protected folder → DENY.
            # (`rest == basename` is the dir itself; `basename + "/"` is inside it —
            # both are captured by comparing the first segment.)
            if first in protected_basenames:
                _deny(
                    f"{g_message}\n[Bubble Shield guard: commande shell touchant un "
                    f"dossier protégé via le montage sandbox Cowork {tok}]")
                return
            # FIX C — UNCONDITIONAL fail-closed backstop on the mnt/ mount tree.
            # Any `/sessions/*/mnt/<X>` whose first segment is NOT a known Cowork
            # infra mount is DENIED — regardless of `any_protection`, regardless of
            # whether ANY protected_folders/markers are known to the host.
            #
            # WHY UNCONDITIONAL (the residual-leak hardening): in a real Cowork
            # session there is NO host global config (protected_folders EMPTY) and
            # the session cwd is a HOST outputs path (e.g. `.../local_<id>/outputs`),
            # NOT inside the marked folder — so `protected` is empty AND
            # `_discover_marker_roots(cwd)` finds nothing → `any_protection` is
            # FALSE and the OLD gated Fix C stayed inert while `cat
            # /sessions/foo/mnt/clients/secret.txt` LEAKED. The deeper reason the
            # host guard cannot do better: any `mnt/<user-folder>` is a folder the
            # user connected to THIS session, and its `.bubble-shield.json` marker
            # lives on the sandbox-FS inode — structurally UNREACHABLE from the Mac
            # path namespace (there is no `/sessions/...` on the host). The host can
            # never see whether a given `mnt/<X>` carries a marker. Therefore the
            # only safe posture for a privacy tool is: block Bash on every non-infra
            # mount subtree by default. Better to block a legitimate Bash op on a
            # user mount (the user can use `bubble_shield_read`/Read, or re-run the
            # command with the REAL Mac path, which resolves the marker via the
            # PRIMARY walk-up) than to leak PII in clear.
            #
            # Known infra mounts (outputs/uploads/.claude/.remote-plugins) are the
            # agent's OWN workspace and stay allowed so we don't brick it. Non-`mnt/`
            # paths (`/sessions/<name>/outputs/…`, `/tmp/…`, any host path) are NOT
            # touched here. And a GLOBAL `block_bash:false` is the operator's
            # deliberate opt-out: it skips the whole Bash branch upstream
            # (`if not block_bash: _allow()`), so Fix C never runs in that case.
            if first not in _COWORK_INFRA_MNT:
                _deny(
                    f"{g_message}\n[Bubble Shield guard: chemin de montage sandbox "
                    f"non-infra ({tok}) — le host ne peut PAS voir le marqueur sur "
                    "l'inode sandbox, bloqué par sécurité (fail-closed).]")
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
