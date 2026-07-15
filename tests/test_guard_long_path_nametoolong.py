"""
tests/test_guard_long_path_nametoolong.py — #561-C (2026-07-15).

THE real cause of the "guard blocks long messages with erreur interne" reports
(finally found via FIX-1's traceback log): a tool ARGUMENT that isn't a real path
— a long Bash commit body, a long Telegram message — gets treated as a path
candidate and handed to `_find_marker_root`, whose FIRST line called
`target.is_dir()` UNGUARDED. On a string longer than PATH_MAX the OS raises
OSError [Errno 63] ENAMETOOLONG, which escaped to the blanket-except → generic
"erreur interne" fail-CLOSED. This is deterministic on length (NOT the transient
race), which is exactly why "longer inputs trip it more."

Fix: a string that can't even be stat'd is NOT a real file, so it cannot be inside
a protected folder → `_find_marker_root` returns None instead of crashing, and the
whole decision proceeds (allow, since a non-path references no protected doc).

Security note: a path over PATH_MAX cannot exist on disk, so allowing it can never
leak a real protected file — there is no real file it could reference.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import guard  # noqa: E402


def test_find_marker_root_on_overlong_string_returns_none_not_crash():
    longp = Path("/Users/joris/" + "x" * 4000)  # >> PATH_MAX
    assert guard._find_marker_root(longp) is None, \
        "an over-PATH_MAX string is not a real file → None, never an OSError crash"


def _run(event: dict):
    sys.stdin = io.StringIO(json.dumps(event))
    out = io.StringIO()
    sys.stdout = out
    code = None
    try:
        guard.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.stdout = sys.__stdout__
    return code, out.getvalue()


def test_long_bash_commit_body_not_falseblocked():
    """A git commit with a very long message body must NOT trip erreur interne."""
    code, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": 'git commit -m "' + "A" * 3000 + '"'},
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
    })
    assert "erreur interne" not in out, "long commit body must not fail-closed"


def test_long_mcp_message_not_falseblocked():
    """A long Telegram/MCP message (the reported curl-send case) must not crash."""
    code, out = _run({
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": {"chat_id": "123", "text": "report " + "X" * 3000},
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
    })
    assert "erreur interne" not in out, "long MCP message must not fail-closed"


def test_a_real_protected_path_still_blocks(tmp_path, monkeypatch):
    """Guardrail: the fix must NOT weaken real protection — a normal-length path
    inside a marked folder is still blocked. (A too-long string is the ONLY thing
    newly allowed, and it can't reference a real file.)"""
    # Create a marked folder + a file inside it.
    (tmp_path / ".bubble-shield.json").write_text("{}")
    doc = tmp_path / "avis.txt"
    doc.write_text("x")
    hit = guard._find_marker_root(doc)
    assert hit is not None and hit[0] == tmp_path.resolve() or hit[0] == tmp_path, \
        "a real file inside a marked folder must still find its marker (protection intact)"
