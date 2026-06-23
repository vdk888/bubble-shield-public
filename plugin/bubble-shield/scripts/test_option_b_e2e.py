#!/usr/bin/env python3
"""End-to-end test for Option B — the Cowork containment strategy.

Proves the two halves of Option B work together (without relying on the broken
PostToolUse `updatedToolOutput` substitution for built-in tools, see
anthropics/claude-code#32105):

  1. The MCP tool `bubble_shield_read(path)` reads a PII file and returns ALREADY
     anonymised text (⟦…⟧ tokens). Since this is the MCP tool's OWN output, the
     harness shows the model the tokens, not raw PII — the one reliable channel.
  2. The PreToolUse guard DENIES a built-in Read of a protected-folder file AND
     the deny message NAMES `bubble_shield_read`, steering the model to the tool
     above instead of leaking via a bare Read.

Plus the fail-CLOSED guarantee: an error inside bubble_shield_read must surface
isError, never raw content.

Standalone script (NOT pytest-collected): run directly, prints PASS/FAIL.
Uses ONLY synthetic PII. Run: python3 test_option_b_e2e.py
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "guard.py"

# Synthetic PII only — never a real value.
SYNTH = "Marc Dubois, IBAN FR7630006000011234567890189, marc.dubois@example.com"
RAW_BITS = ["Marc Dubois", "FR7630006000011234567890189", "marc.dubois@example.com"]

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  {'✅' if cond else '❌ FAIL'} {name}")


def run_guard(event: dict, config_path: str | None) -> dict:
    """Invoke guard.py as the harness would: event JSON on stdin → decision JSON."""
    env = dict(os.environ)
    td = tempfile.mkdtemp()
    env["CLAUDE_PROJECT_DIR"] = td
    env["HOME"] = td
    env["BUBBLE_SHIELD_GUARD_CONFIG"] = config_path or str(Path(td) / "none.json")
    p = subprocess.run([sys.executable, str(GUARD)],
                       input=json.dumps(event), capture_output=True, text=True, env=env)
    out = p.stdout.strip()
    if not out:
        return {"_decision": "allow-noop"}
    d = json.loads(out)
    return {"_decision": d["hookSpecificOutput"]["permissionDecision"],
            "_reason": d["hookSpecificOutput"].get("permissionDecisionReason", "")}


def main():
    sys.path.insert(0, str(HERE))
    import bubble_shield_mcp as M

    work = tempfile.mkdtemp()
    home = os.path.join(work, "bs_home")
    os.environ["BUBBLE_SHIELD_HOME"] = home
    os.environ["BUBBLE_SHIELD_SESSION"] = "option-b-e2e"
    # rebind module-level paths that were read at import time
    M.BUBBLE_SHIELD_HOME = Path(home)
    M.VAULT_DIR = Path(home) / "vaults"

    # A protected client folder (marker-based, the Cowork-native path).
    client = os.path.join(work, "client-acme")
    os.makedirs(client)
    Path(client, ".bubble-shield.json").write_text("{}")
    pii_file = os.path.join(client, "releve.txt")
    Path(pii_file).write_text(SYNTH + "\n", encoding="utf-8")

    print("=== Half 1: bubble_shield_read returns tokens, not raw PII ===")
    anon = M._anonymise_file(pii_file)
    check("output carries ⟦…⟧ tokens", "⟦" in anon)
    check("no raw name/IBAN/email leaks", not any(b in anon for b in RAW_BITS))

    print("\n=== Half 2: guard DENIES built-in Read of protected file ===")
    ev = {"tool_name": "Read", "tool_input": {"file_path": pii_file}, "cwd": work}
    # marker-based protection needs no global config; pass a non-existent one
    g = run_guard(ev, None)
    check("decision is deny", g["_decision"] == "deny")
    check("deny reason STEERS to bubble_shield_read", "bubble_shield_read" in g["_reason"])

    print("\n=== Half 2b: own MCP tool is NOT denied (no self-block) ===")
    ev2 = {"tool_name": "mcp__plugin_bubble-shield_bubble_shield__bubble_shield_read",
           "tool_input": {"path": pii_file}, "cwd": work}
    g2 = run_guard(ev2, None)
    check("bubble_shield_read tool call allowed", g2["_decision"] == "allow-noop")

    print("\n=== Fail-closed: error path never leaks raw content ===")
    sent = []
    _orig_send = M._send
    M._send = lambda obj: sent.append(obj)
    _orig_anon = M._anonymise_file
    M._anonymise_file = lambda path: (_ for _ in ()).throw(
        RuntimeError("simulated extraction failure"))
    try:
        M._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "bubble_shield_read",
                              "arguments": {"path": pii_file}}})
    finally:
        M._anonymise_file = _orig_anon
        M._send = _orig_send
    res = sent[-1]["result"]
    check("error response sets isError", res.get("isError") is True)
    check("error response leaks no raw PII",
          not any(b in res["content"][0]["text"] for b in RAW_BITS))

    print("\n=== Round-trip: vault de-anonymises the tokens back ===")
    out_doc = os.path.join(work, "final.txt")
    summary = M._deanonymise_to_file(out_doc, anon)
    restored = Path(out_doc).read_text(encoding="utf-8")
    check("all real values restored on write", all(b in restored for b in RAW_BITS))
    check("write summary reports 3 tokens restored", summary["tokens_restored"] == 3)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{passed} passed, {total - passed} failed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
