"""
test_guard_599_failopen.py — #599: a guard-INTERNAL error on a NON-FILE MCP
tool (Telegram reply, etc.) must fail-OPEN, not block the user's work.

Observed 2026-07-10: a Telegram reply was blocked by guard.py's catch-all
('erreur interne') when the NER daemon threw mid-idle-shutdown — even though
the plugin was 'disabled'. A Telegram reply reads no protected file, so a
guard-internal error there has nothing to protect and must not fail-closed.

We force the catch-all deterministically via BUBBLE_SHIELD_TEST_FORCE_ERROR=1
(a test-only trigger; the v1.20.4 input-robustness coercion otherwise handles
malformed shapes, so there is no other reliable way to reach the catch-all).
For each tool class:
  - NON-FILE mcp tool  → ALLOW (fail-open, the #599 fix)
  - FILE-touching tool → still DENY (fail-closed invariant preserved)

Runs the REAL guard.py as a subprocess. Synthetic values only.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard.py"


def _run(event, *, force=True, raw=False):
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "bubble-shield.json"
        cfg.write_text(json.dumps({"protected_folders": [str(Path(td) / "prot")]}))
        env = dict(os.environ, BUBBLE_SHIELD_GUARD_CONFIG=str(cfg), HOME=td,
                   CLAUDE_PROJECT_DIR=td, BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN="1")
        if force:
            env["BUBBLE_SHIELD_TEST_FORCE_ERROR"] = "1"
        stdin = event if raw else json.dumps(event)
        p = subprocess.run([sys.executable, str(GUARD)], input=stdin,
                           capture_output=True, text=True, env=env)
    out = p.stdout.strip()
    if not out:
        return "allow-noop"
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return "parse-error:" + out[:120]


def _ev(tool_name, tool_input=None):
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name,
            "tool_input": tool_input if tool_input is not None else {}, "cwd": "/tmp"}


# ── fail-OPEN: non-file MCP tools ────────────────────────────────────────────

def test_telegram_reply_fails_open():
    assert _run(_ev("mcp__plugin_telegram_telegram__reply",
                    {"chat_id": "123", "text": "bonjour"})) in ("allow", "allow-noop")


def test_scheduled_task_tool_fails_open():
    assert _run(_ev("mcp__scheduled-tasks__list_scheduled_tasks")) in ("allow", "allow-noop")


# ── fail-CLOSED invariant preserved ──────────────────────────────────────────

def test_read_still_fails_closed():
    assert _run(_ev("Read", {"file_path": "/x/y.pdf"})) == "deny"


def test_bash_still_fails_closed():
    assert _run(_ev("Bash", {"command": "cat /x/y.pdf"})) == "deny"


def test_mcp_tool_with_path_input_still_fails_closed():
    # A generic MCP file tool carrying an absolute path token → NOT file-free →
    # must still fail-closed even though the shape forced the catch-all.
    assert _run(_ev("mcp__filesystem__read_file",
                    {"path": "/Users/x/Dropbox/clients/secret.pdf"})) == "deny"


def test_telegram_reply_mentioning_a_path_fails_closed():
    # Conservative-correct: if the non-file tool's INPUT carries an absolute path
    # token, we can't prove it's file-free → fail-closed (over-block a message
    # that names a path rather than risk a leak).
    assert _run(_ev("mcp__plugin_telegram_telegram__reply",
                    {"text": "regarde /Users/joris/Dropbox/clients/x.pdf"})) == "deny"


def test_unparseable_event_still_fails_closed():
    # force=False: the parse failure itself fails closed, before the test trigger.
    assert _run("}{ broken", force=False, raw=True) == "deny"


# ── the fix is inert in normal operation (no forced error) ───────────────────

def test_no_error_telegram_reply_allowed_normally():
    assert _run(_ev("mcp__plugin_telegram_telegram__reply",
                    {"text": "bonjour"}), force=False) in ("allow", "allow-noop")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
