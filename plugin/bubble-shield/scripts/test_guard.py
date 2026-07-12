#!/usr/bin/env python3
"""Black-box tests for the Bubble Shield guard: feed it event JSON on stdin, assert the
permissionDecision. Run: python3 test_guard.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "guard.py"


def run(event: dict, config: dict | None) -> dict:
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        if config is not None:
            cfgp = Path(td) / "bubble-shield.json"
            cfgp.write_text(json.dumps(config))
            env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(cfgp)
        else:
            env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(Path(td) / "does-not-exist.json")
        # neutralise other config locations
        env["CLAUDE_PROJECT_DIR"] = td
        env["HOME"] = td
        p = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(event), capture_output=True, text=True, env=env,
        )
    out = p.stdout.strip()
    if not out:
        return {"_decision": "allow-noop", "_stdout": "", "_code": p.returncode}
    try:
        d = json.loads(out)
        return {"_decision": d["hookSpecificOutput"]["permissionDecision"], **d}
    except Exception:
        return {"_decision": "PARSE_ERROR", "_stdout": out, "_code": p.returncode}


PROT = "/tmp/bubble-shield-test-clients"
CFG = {
    "protected_folders": [PROT],
    "allow_paths": [f"{PROT}/dossier-x/clean"],
    "allow_extensions": [".anon.txt"],
    "block_bash": True,
}

CASES = []
def case(name, event, config, expect):
    CASES.append((name, event, config, expect))

# --- DENY: read inside protected folder ---
case("read protected file", {"tool_name": "Read", "tool_input": {"file_path": f"{PROT}/dossier-x/der.pdf"}, "cwd": "/tmp"}, CFG, "deny")
case("grep protected dir", {"tool_name": "Grep", "tool_input": {"path": f"{PROT}/dossier-x"}, "cwd": "/tmp"}, CFG, "deny")
# Glob is names-only (a listing, no file CONTENT) → ALLOWED even on a protected
# folder (the sanctioned discovery path). Grep above returns matching LINES
# (content) so it stays denied. See guard.py CHANGE 1.
case("glob protected dir ALLOWED (names only)", {"tool_name": "Glob", "tool_input": {"path": PROT}, "cwd": "/tmp"}, CFG, "allow-noop")
case("write into protected", {"tool_name": "Write", "tool_input": {"file_path": f"{PROT}/x.txt"}, "cwd": "/tmp"}, CFG, "deny")
case("bash cat protected", {"tool_name": "Bash", "tool_input": {"command": f"cat {PROT}/dossier-x/der.pdf"}, "cwd": "/tmp"}, CFG, "deny")

# --- ALLOW: outside, or explicitly exempted ---
case("read outside protected", {"tool_name": "Read", "tool_input": {"file_path": "/tmp/other/note.txt"}, "cwd": "/tmp"}, CFG, "allow-noop")
case("read allow_paths clean/", {"tool_name": "Read", "tool_input": {"file_path": f"{PROT}/dossier-x/clean/der.anon.txt"}, "cwd": "/tmp"}, CFG, "allow-noop")
case("read .anon.txt ext", {"tool_name": "Read", "tool_input": {"file_path": f"{PROT}/dossier-y/der.anon.txt"}, "cwd": "/tmp"}, CFG, "allow-noop")
case("bash unrelated", {"tool_name": "Bash", "tool_input": {"command": "ls /tmp"}, "cwd": "/tmp"}, CFG, "allow-noop")
case("bash blocked off", {"tool_name": "Bash", "tool_input": {"command": f"cat {PROT}/x.pdf"}, "cwd": "/tmp"}, {**CFG, "block_bash": False}, "allow-noop")

# --- FAIL CLOSED ---
case("malformed config", {"tool_name": "Read", "tool_input": {"file_path": f"{PROT}/x.pdf"}, "cwd": "/tmp"}, "MALFORMED", "deny")
case("unparseable event", "NOT JSON", CFG, "deny")

# --- inert when unconfigured ---
case("no config = inert", {"tool_name": "Read", "tool_input": {"file_path": "/anything"}, "cwd": "/tmp"}, None, "allow-noop")
case("empty protected = inert", {"tool_name": "Read", "tool_input": {"file_path": "/anything"}, "cwd": "/tmp"}, {"protected_folders": []}, "allow-noop")

# --- mail-guard: ALLOW raw mail reads but inject anonymise-first context ------
# (blocking the fetch is a catch-22 — the fetch is the only way to GET the mail
# text to anonymise; so we allow + steer instead)
EMPTY = {"protected_folders": []}   # mail-guard is on by default even with no folders
case("mail search_threads allow+ctx", {"tool_name": "mcp__0ef9bd27-855a__search_threads", "tool_input": {}, "cwd": "/tmp"}, EMPTY, "allow")
case("mail get_thread allow+ctx", {"tool_name": "mcp__abc__get_thread", "tool_input": {}, "cwd": "/tmp"}, EMPTY, "allow")
case("mail list_messages allow+ctx", {"tool_name": "mcp__abc__list_messages", "tool_input": {}, "cwd": "/tmp"}, EMPTY, "allow")
# non-mail mcp tools must NOT be caught
case("notion search allowed", {"tool_name": "mcp__notion__search", "tool_input": {}, "cwd": "/tmp"}, EMPTY, "allow-noop")
case("bubble_shield_read allowed (own tool)", {"tool_name": "mcp__plugin_bubble-shield_bubble_shield__bubble_shield_read", "tool_input": {}, "cwd": "/tmp"}, EMPTY, "allow-noop")
case("workspace bash allowed", {"tool_name": "mcp__workspace__bash", "tool_input": {"command": "ls"}, "cwd": "/tmp"}, EMPTY, "allow-noop")
# opt-out
case("mail_guard:false → allowed", {"tool_name": "mcp__abc__search_threads", "tool_input": {}, "cwd": "/tmp"}, {"protected_folders": [], "mail_guard": False}, "allow-noop")


def main():
    fails = 0
    for name, event, config, expect in CASES:
        # special handling for the two odd inputs
        if config == "MALFORMED":
            # write a deliberately broken config
            import tempfile
            td = tempfile.mkdtemp()
            cfgp = Path(td) / "bubble-shield.json"
            cfgp.write_text("{ this is not json ")
            env = dict(os.environ, BUBBLE_SHIELD_GUARD_CONFIG=str(cfgp), HOME=td, CLAUDE_PROJECT_DIR=td)
            p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), capture_output=True, text=True, env=env)
            got = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]
        elif event == "NOT JSON":
            res = run("NOT JSON_PLACEHOLDER", config)  # run() will json.dumps it; emulate raw below
            # run() dumps the string as JSON string -> guard parses it as a str, tool_name missing -> allow.
            # We want a truly unparseable stdin: do it directly.
            import tempfile
            td = tempfile.mkdtemp()
            cfgp = Path(td) / "bubble-shield.json"; cfgp.write_text(json.dumps(CFG))
            env = dict(os.environ, BUBBLE_SHIELD_GUARD_CONFIG=str(cfgp), HOME=td, CLAUDE_PROJECT_DIR=td)
            p = subprocess.run([sys.executable, str(GUARD)], input="}{ broken", capture_output=True, text=True, env=env)
            got = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]
        else:
            got = run(event, config)["_decision"]
        ok = got == expect
        fails += not ok
        print(f"{'✓' if ok else '✗ FAIL'}  {name}: got={got} expect={expect}")
    print(f"\n{len(CASES)-fails}/{len(CASES)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
