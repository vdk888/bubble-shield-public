#!/usr/bin/env python3
"""Black-box tests for the PostToolUse anonymiser: feed it event JSON on stdin,
assert the updatedToolOutput contract. Run: python3 test_posttool_anonymize.py

These cover the REGEX-ONLY path (no daemon needed → runs in CI / bare python).
The ML-daemon chain is proven separately (needs the ML pack + a running daemon).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "posttool_anonymize.py"

passed = failed = 0


def run(event: dict, *, enabled: bool, tools=None) -> str:
    """Run the hook with a temp config; return raw stdout."""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"posttool_enabled": enabled}
        if tools is not None:
            cfg["posttool_tools"] = tools
        cfgp = Path(td) / "bubble-shield.json"
        cfgp.write_text(json.dumps(cfg))
        env = dict(os.environ)
        env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(cfgp)
        env["BUBBLE_SHIELD_HOME"] = str(Path(td) / "home")
        env["BUBBLE_SHIELD_NERD_PORT"] = "1"  # force daemon-unreachable → regex-only
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"hook exited {r.returncode}: {r.stderr}"
        return r.stdout.strip()


def check(name: str, cond: bool):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


PII = {"tool_name": "Read", "session_id": "s1",
       "tool_response": {"type": "text",
       "text": "Client Jean Dupont, IBAN FR76 3000 6000 0112 3456 7890 189, jean@x.fr"}}

# 1. PII present + enabled → rewrite with tokens, raw values gone
out = run(PII, enabled=True)
d = json.loads(out) if out else {}
txt = d.get("hookSpecificOutput", {}).get("updatedToolOutput", {}).get("text", "")
check("PII present → updatedToolOutput emitted", bool(txt))
check("tokens present in rewrite", "⟦" in txt)
check("raw IBAN removed", "FR76 3000" not in txt)
check("raw email removed", "jean@x.fr" not in txt)
check("hookEventName correct", d.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")

# 2. opt-in OFF → no output
check("opt-in off → no-op", run(PII, enabled=False) == "")

# 1b. A MAIL structured result is now CONTAINED (default-on), preserving shape —
# NOT clobbered with a flat string (the H.reduce regression). Assert the rewrite
# keeps the dict shape rather than emitting {type,text}.
gmail_structured = {"tool_name": "mcp__gmail__search_threads", "session_id": "struct",
                    "tool_response": {"threads": [
                        {"id": "a", "from": "paul.riviere@email.fr", "snippet": "Paul Rivière"}]}}
gs_out = run(gmail_structured, enabled=True)
gs_u = json.loads(gs_out).get("hookSpecificOutput", {}).get("updatedToolOutput", {}) if gs_out else {}
check("mail structured contained, shape preserved (no flat-string H.reduce)",
      isinstance(gs_u, dict) and "threads" in gs_u and gs_u["threads"][0]["id"] == "a")
# A NON-mail structured result must still be left untouched (the simple-text
# safe-gate bails → connector safe).
mixed = {"tool_name": "mcp__notion__query", "session_id": "mix",
         "tool_response": {"results": [{"title": "Paul Rivière", "email": "p@x.fr"}]}}
check("non-mail structured untouched", run(mixed, enabled=True) == "")

# 1c. NON-mail structured MCP result (notion-style) with containment OFF → MUST
# stay untouched even though the matcher now includes mcp__.* (the crash vector).
notion = {"tool_name": "mcp__notion__search", "session_id": "n",
          "tool_response": {"results": [{"title": "Paul Rivière", "email": "p@x.fr"}]}}
check("non-mail structured mcp untouched", run(notion, enabled=True) == "")

# 1d. MAIL containment (opt-in): structured mail result → rewritten PRESERVING
# the dict shape (not flattened), PII cloaked, structure intact.
def run_cfg(event, cfg):
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        cfgp = Path(td) / "bubble-shield.json"; cfgp.write_text(json.dumps(cfg))
        env = dict(os.environ); env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(cfgp)
        env["BUBBLE_SHIELD_HOME"] = str(Path(td) / "home"); env["BUBBLE_SHIELD_NERD_PORT"] = "1"
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                           capture_output=True, text=True, env=env)
        return r.stdout.strip()
mail_ev = {"tool_name": "mcp__0ef9bd27__search_threads", "session_id": "mc",
           "tool_response": {"threads": [{"id": "a", "from": "p@x.fr",
                              "snippet": "IBAN FR14 2004 1010 0505 0001 3M02 606"}]}}
# containment is ON BY DEFAULT and runs WITHOUT posttool_enabled (mail is
# high-risk; protection on unless explicitly disabled).
mc_out = run_cfg(mail_ev, {})   # empty config — containment default true, posttool off
mc = json.loads(mc_out) if mc_out else {}
u = mc.get("hookSpecificOutput", {}).get("updatedToolOutput", {})
check("mail containment default-on (no posttool_enabled)", bool(mc_out))
check("mail containment PRESERVES dict shape", isinstance(u, dict) and "threads" in u)
check("mail containment cloaks PII", "FR14 2004" not in json.dumps(u) and "p@x.fr" not in json.dumps(u))
check("mail containment keeps structure (id)", u.get("threads", [{}])[0].get("id") == "a")
check("mail containment explicitly OFF → untouched", run_cfg(mail_ev, {"mail_containment": False}) == "")

# 3. benign output (no PII), daemon down → no rewrite
benign = {"tool_name": "Bash", "session_id": "s3",
          "tool_response": {"type": "text", "text": "All 42 tests passed in 3.1s"}}
check("benign output untouched", run(benign, enabled=True) == "")

# 4. tool-scope filter → non-matching tool is skipped
check("tool not in posttool_tools → no-op",
      run(PII, enabled=True, tools=["mcp__gmail__"]) == "")
gmail_ev = {"tool_name": "mcp__gmail__read", "session_id": "s4b",
            "tool_response": PII["tool_response"]}
gmail_out = run(gmail_ev, enabled=True, tools=["mcp__gmail__"])
gtxt = json.loads(gmail_out).get("hookSpecificOutput", {}).get(
    "updatedToolOutput", {}).get("text", "") if gmail_out else ""
check("tool in posttool_tools → rewritten", "⟦" in gtxt)

# 5. malformed / empty event → fail-open (no crash, no output)
with tempfile.TemporaryDirectory() as td:
    env = dict(os.environ); env["BUBBLE_SHIELD_GUARD_CONFIG"] = "/nonexistent"
    r = subprocess.run([sys.executable, str(HOOK)], input="not json",
                       capture_output=True, text=True, env=env)
    check("malformed event → exit 0, no output", r.returncode == 0 and not r.stdout.strip())

# 6. oversized blob → skipped (perf guard)
big = {"tool_name": "Read", "session_id": "s6",
       "tool_response": {"type": "text", "text": "x" * 250_000}}
check("oversized output skipped", run(big, enabled=True) == "")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
