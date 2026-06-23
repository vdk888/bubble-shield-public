#!/usr/bin/env python3
"""Black-box tests for the Bubble Shield MCP server: drive it over stdio, assert the
tool contracts. Run: python3 test_bubble_shield_mcp.py  (regex-only; no daemon needed)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "bubble_shield_mcp.py"
passed = failed = 0


def rpc(calls, home=None, plugin_root=None, home2=None):
    """Send a list of JSON-RPC requests; return {id: result_or_error}."""
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root or str(HERE.parent)
    env["BUBBLE_SHIELD_HOME"] = home or str(Path(tempfile.mkdtemp()) / "home")
    env["HOME"] = home2 or str(Path(tempfile.mkdtemp()) / "fakehome")  # isolate ~/.config
    env["BUBBLE_SHIELD_NERD_PORT"] = "1"   # force daemon-unreachable → regex-only, deterministic
    lines = "\n".join(json.dumps(c) for c in calls) + "\n"
    r = subprocess.run([sys.executable, str(SERVER)], input=lines,
                       capture_output=True, text=True, env=env)
    out = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if "id" in o:
            out[o["id"]] = o
    return out


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name}")


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def call(id_, name, args):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def text(res):
    return res["result"]["content"][0]["text"]


# 1. handshake + tools/list lists all 4
r = rpc([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
check("initialize returns serverInfo", r[1]["result"]["serverInfo"]["name"] == "bubble_shield")
tools = {t["name"] for t in r[2]["result"]["tools"]}
check("all 8 tools listed (5 original + 3 custom-fields Phase 1)",
      tools == {"bubble_shield_read", "bubble_shield_anonymize_text", "bubble_shield_write",
                "bubble_shield_setup_ml", "bubble_shield_enable_global",
                "bubble_shield_add_field", "bubble_shield_list_fields",
                "bubble_shield_remove_field"})

# 2. bubble_shield_anonymize_text cloaks PII
home = str(Path(tempfile.mkdtemp()) / "h")
r = rpc([INIT, call(2, "bubble_shield_anonymize_text",
                    {"text": "Madame Sylvie Brunel IBAN FR76 3000 6000 0112 3456 7890 189"})], home=home)
t = text(r[2])
check("anonymize_text emits tokens", "⟦" in t)
check("anonymize_text removes raw IBAN", "FR76 3000" not in t)

# 3. bubble_shield_read on a PII file (same home → shared vault)
tf = Path(tempfile.mkdtemp()) / "doc.txt"
tf.write_text("Client Jean Dupont, jean@x.fr", encoding="utf-8")
r = rpc([INIT, call(2, "bubble_shield_read", {"path": str(tf)})], home=home)
check("bubble_shield_read cloaks file", "⟦" in text(r[2]) and "jean@x.fr" not in text(r[2]))

# 4. bubble_shield_read missing file → fail-closed (isError, no raw)
r = rpc([INIT, call(2, "bubble_shield_read", {"path": "/nope/x.txt"})], home=home)
check("bubble_shield_read missing → isError", r[2]["result"].get("isError") is True)

# 5. THE BIG ONE: write round-trip — file gets REAL PII, response does NOT
home2 = str(Path(tempfile.mkdtemp()) / "h2")
out = Path(tempfile.mkdtemp()) / "letter.txt"
r = rpc([INIT,
         call(2, "bubble_shield_anonymize_text", {"text": "Madame Sylvie Brunel, sylvie@x.fr"}),
         call(3, "bubble_shield_write", {"path": str(out),
              "content": "Lettre pour ⟦NOM_0001⟧ (⟦EMAIL_0001⟧)."})], home=home2)
resp = text(r[3])
disk = out.read_text(encoding="utf-8") if out.exists() else ""
check("bubble_shield_write succeeds", "✅" in resp)
check("write RESPONSE hides real PII", "Sylvie Brunel" not in resp and "sylvie@x.fr" not in resp)
check("written FILE has real PII", "Sylvie Brunel" in disk and "sylvie@x.fr" in disk)

# 6. bubble_shield_write with no vault → fail-closed, no file
home3 = str(Path(tempfile.mkdtemp()) / "empty")
out3 = Path(tempfile.mkdtemp()) / "x.txt"
r = rpc([INIT, call(2, "bubble_shield_write", {"path": str(out3), "content": "hi ⟦NOM_0001⟧"})], home=home3)
check("write without vault → isError", r[2]["result"].get("isError") is True)
check("write without vault → no file", not out3.exists())

# 7. bubble_shield_setup_ml status returns a state (works whether installed or not)
r = rpc([INIT, call(2, "bubble_shield_setup_ml", {"action": "status"})], home=str(Path(tempfile.mkdtemp())/"h"))
check("setup_ml status returns a state", text(r[2]).startswith("["))

# 8. bubble_shield_enable_global: on/off/status + MERGE preserves existing keys
fakehome = tempfile.mkdtemp()
cfgdir = Path(fakehome) / ".config" / "bubble_shield"
cfgdir.mkdir(parents=True)
(cfgdir / "bubble-shield.json").write_text(
    json.dumps({"protected_folders": ["/x/clients"], "block_bash": True}))
r = rpc([INIT, call(2, "bubble_shield_enable_global", {"action": "status"})], home2=fakehome)
check("enable_global status reads off initially", "[off]" in text(r[2]))
r = rpc([INIT, call(2, "bubble_shield_enable_global", {"action": "on"})], home2=fakehome)
check("enable_global on", "[on]" in text(r[2]))
cfg = json.loads((cfgdir / "bubble-shield.json").read_text())
check("enable_global set posttool_enabled", cfg.get("posttool_enabled") is True)
check("enable_global MERGED (kept protected_folders)", cfg.get("protected_folders") == ["/x/clients"])
check("enable_global MERGED (kept block_bash)", cfg.get("block_bash") is True)
r = rpc([INIT, call(2, "bubble_shield_enable_global", {"action": "off"})], home2=fakehome)
check("enable_global off", "[off]" in text(r[2]) and
      json.loads((cfgdir / "bubble-shield.json").read_text())["posttool_enabled"] is False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
