#!/usr/bin/env python3
"""Black-box tests for the Bubble Shield MCP server: drive it over stdio, assert the
tool contracts.

Run: python3 test_bubble_shield_mcp.py

NER daemon gate changes (fix/ner-fail-closed-gate):
  - daemon DOWN  → bubble_shield_read / bubble_shield_anonymize_text return isError:true
                    NO anonymized body, NO raw PII in the response.
  - daemon UP    → existing token behaviour unchanged.
  - bubble_shield_status → returns ner/model/ml_pack_installed/daemon_reachable/launchagent_loaded
  - bubble_shield_write  → unchanged (vault-based, no daemon dependency).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "bubble_shield_mcp.py"
passed = failed = 0


# ---------------------------------------------------------------------------
# Mock NER daemon — a tiny HTTP server that returns empty matches on /detect
# and 200 on /health. Runs in a background thread for the daemon-UP tests.
# ---------------------------------------------------------------------------

class _MockNERHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # silence access log

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/detect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            text = body.get("text", "")
            # Detect bare all-caps two-word sequences as PERSON (simulating GLiNER)
            import re
            matches = []
            for m in re.finditer(r"\b([A-Z]{2,}\s+[A-Z]{2,})\b", text):
                matches.append({
                    "start": m.start(), "end": m.end(),
                    "entity_type": "PERSON", "value": m.group(),
                    "score": 0.91,
                })
            resp = json.dumps({"matches": matches}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()


def start_mock_daemon():
    """Start the mock NER daemon on a random port. Returns (port, server)."""
    srv = HTTPServer(("127.0.0.1", 0), _MockNERHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return port, srv


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def rpc(calls, home=None, plugin_root=None, home2=None, nerd_port="1", enable_mail=False):
    """Send a list of JSON-RPC requests; return {id: result_or_error}.

    nerd_port="1" (default) → daemon unreachable (fail-closed path).
    Pass an actual port to exercise the daemon-UP path.
    enable_mail=True → sets BUBBLE_SHIELD_ENABLE_MAIL=1 (mail path is
    off-by-default/reserve in the shipped product; see test_mail_disabled.py).
    """
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root or str(HERE.parent)
    env["BUBBLE_SHIELD_HOME"] = home or str(Path(tempfile.mkdtemp()) / "home")
    env["HOME"] = home2 or str(Path(tempfile.mkdtemp()) / "fakehome")  # isolate ~/.config
    env["BUBBLE_SHIELD_NERD_PORT"] = str(nerd_port)
    if enable_mail:
        env["BUBBLE_SHIELD_ENABLE_MAIL"] = "1"
    else:
        env.pop("BUBBLE_SHIELD_ENABLE_MAIL", None)
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
        passed += 1; print(f"  PASS {name}")
    else:
        failed += 1; print(f"  FAIL {name}")


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def call(id_, name, args):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def text(res):
    return res["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# 1. Handshake + tools/list — mail path (bubble_shield_mail_read/_apply) is
#    DISABLED from the shipped product by default (V1 is docs-only), gated
#    behind BUBBLE_SHIELD_ENABLE_MAIL — see test_mail_disabled.py for the
#    on/off tools/list contract.
# ---------------------------------------------------------------------------
print("\n--- 1. Handshake + tools/list ---")
r = rpc([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
check("initialize returns serverInfo", r[1]["result"]["serverInfo"]["name"] == "bubble_shield")
tools = {t["name"] for t in r[2]["result"]["tools"]}
check("core tools listed (mail path absent by default)",
      tools == {"bubble_shield_read", "bubble_shield_list", "bubble_shield_anonymize_text",
                "bubble_shield_write", "bubble_shield_setup_ml", "bubble_shield_setup_ocr",
                "bubble_shield_enable_global", "bubble_shield_add_field",
                "bubble_shield_add_known_pii", "bubble_shield_list_fields",
                "bubble_shield_remove_field", "bubble_shield_status"})
check("mail tools NOT listed by default", "bubble_shield_mail_read" not in tools
      and "bubble_shield_mail_apply" not in tools)

# ---------------------------------------------------------------------------
# 2. DAEMON DOWN → fail-closed: anonymize_text returns isError, no body, no raw PII
# ---------------------------------------------------------------------------
print("\n--- 2. Daemon DOWN fail-closed (anonymize_text) ---")
home_down = str(Path(tempfile.mkdtemp()) / "h_down")
r = rpc([INIT, call(2, "bubble_shield_anonymize_text",
                    {"text": "LEMAIRE JEAN CLAUDE et CHARPENTIER ANNE, IBAN FR76 3000 6000 0112 3456 7890 189"})],
        home=home_down, nerd_port="1")
res2 = r[2]["result"]
check("daemon-down anonymize_text → isError:true",
      res2.get("isError") is True)
body2 = res2["content"][0]["text"]
check("daemon-down anonymize_text → no anonymized tokens in body",
      "⟦" not in body2)
check("daemon-down anonymize_text → no raw PII name in body",
      "LEMAIRE" not in body2 and "CHARPENTIER" not in body2)
check("daemon-down anonymize_text → no raw IBAN in body",
      "FR76" not in body2)
check("daemon-down anonymize_text → mentions NER hors-ligne",
      "NER" in body2 or "hors-ligne" in body2 or "daemon" in body2.lower())

# ---------------------------------------------------------------------------
# 3. SHADOW-INDEX REDESIGN (B1) — bubble_shield_read is a fast hash→serve path
#    that runs ZERO models at read time. On a cache MISS it serves the RAW
#    extracted text (accepted B1 gap, client-agreed): the read path deliberately
#    does NOT run NER/Gemma/OCR and does NOT fail-closed on a daemon-down —
#    because it never touches the daemon. Full anonymisation happens later in the
#    background sweep. (Pre-redesign this asserted read fail-closed with the
#    daemon down; that contract is retired by the B1 product decision.)
# ---------------------------------------------------------------------------
print("\n--- 3. Shadow-index B1: read serves raw on miss, runs no models ---")
tf_down = Path(tempfile.mkdtemp()) / "tax_doc.txt"
tf_down.write_text(
    "LEMAIRE JEAN CLAUDE\nOU CHARPENTIER ANNE\nAdresse: 10 rue des lilas\n"
    "IBAN FR76 3000 6000 0112 3456 7890 189",
    encoding="utf-8")
r = rpc([INIT, call(2, "bubble_shield_read", {"path": str(tf_down)})],
        home=home_down, nerd_port="1")
res3 = r[2]["result"]
# Read succeeds even with the daemon down — it never calls the daemon (B1).
check("shadow-index read (daemon down) → no isError (never touches daemon)",
      not res3.get("isError"))
body3 = res3["content"][0]["text"]
# MISS → raw extracted text is served verbatim (no tokens, no masking at read).
check("shadow-index read miss → serves raw extracted text",
      "LEMAIRE" in body3 and "FR76" in body3)
check("shadow-index read miss → no ⟦tokens⟧ (no models ran at read)",
      "⟦" not in body3)

# ---------------------------------------------------------------------------
# 4. bubble_shield_read missing file → isError (unchanged)
# ---------------------------------------------------------------------------
print("\n--- 4. Missing file → isError ---")
r = rpc([INIT, call(2, "bubble_shield_read", {"path": "/nope/x.txt"})],
        home=home_down, nerd_port="1")
check("bubble_shield_read missing → isError", r[2]["result"].get("isError") is True)

# ---------------------------------------------------------------------------
# 5. DAEMON UP → tokens produced (anonymize_text)
# ---------------------------------------------------------------------------
print("\n--- 5. Daemon UP → tokens produced ---")
mock_port, mock_srv = start_mock_daemon()
time.sleep(0.05)  # let the server bind

home_up = str(Path(tempfile.mkdtemp()) / "h_up")
r = rpc([INIT, call(2, "bubble_shield_anonymize_text",
                    {"text": "MARC DURAND, jean@example.fr, IBAN FR76 3000 6000 0112 3456 7890 189"})],
        home=home_up, nerd_port=str(mock_port))
res5 = r[2]["result"]
check("daemon-up anonymize_text → no isError", not res5.get("isError"))
body5 = text(r[2])
check("daemon-up anonymize_text → emits tokens", "⟦" in body5)
check("daemon-up anonymize_text → removes raw IBAN", "FR76 3000" not in body5)
check("daemon-up anonymize_text → removes raw email", "jean@example.fr" not in body5)
check("daemon-up anonymize_text → GLiNER caught all-caps name",
      "MARC DURAND" not in body5)

# ---------------------------------------------------------------------------
# 6. SHADOW-INDEX REDESIGN (B1) — bubble_shield_read on a cache MISS serves RAW,
#    with or without the daemon. The read path runs no models; masking is the
#    sweep's job, not the read's. (Pre-redesign this asserted read tokenised
#    live via the daemon; retired by B1.) `bubble_shield_anonymize_text` still
#    tokenises live (see section 5) for the interactive text path.
# ---------------------------------------------------------------------------
print("\n--- 6. Shadow-index B1: read miss serves raw even with daemon up ---")
tf_up = Path(tempfile.mkdtemp()) / "doc_up.txt"
tf_up.write_text("Client MARC DURAND, jean@example.fr", encoding="utf-8")
r = rpc([INIT, call(2, "bubble_shield_read", {"path": str(tf_up)})],
        home=home_up, nerd_port=str(mock_port))
res6 = r[2]["result"]
check("shadow-index read (daemon up) → no isError", not res6.get("isError"))
body6 = text(r[2])
# MISS → raw text served; no live tokenisation on the read path.
check("shadow-index read miss → serves raw extracted text",
      "MARC DURAND" in body6 and "jean@example.fr" in body6)
check("shadow-index read miss → no ⟦tokens⟧ (no models ran at read)",
      "⟦" not in body6)

# ---------------------------------------------------------------------------
# 7. WRITE ROUND-TRIP (daemon UP) — file gets REAL PII, response does NOT
# ---------------------------------------------------------------------------
print("\n--- 7. Write round-trip (daemon UP) ---")
home_write = str(Path(tempfile.mkdtemp()) / "h_write")
out_write = Path(tempfile.mkdtemp()) / "letter.txt"
r = rpc([INIT,
         call(2, "bubble_shield_anonymize_text", {"text": "Madame SYLVIE BRUNEL, sylvie@x.fr"}),
         call(3, "bubble_shield_write", {"path": str(out_write),
              "content": "Lettre pour ⟦NOM_0001⟧ (⟦EMAIL_0001⟧)."})],
        home=home_write, nerd_port=str(mock_port))
resp7 = text(r[3])
disk7 = out_write.read_text(encoding="utf-8") if out_write.exists() else ""
check("bubble_shield_write succeeds", "✅" in resp7)
check("write RESPONSE hides real PII", "SYLVIE BRUNEL" not in resp7 and "sylvie@x.fr" not in resp7)
check("written FILE has real email", "sylvie@x.fr" in disk7)

# ---------------------------------------------------------------------------
# 8. WRITE WITHOUT VAULT → fail-closed (unchanged)
# ---------------------------------------------------------------------------
print("\n--- 8. Write without vault → isError ---")
home3 = str(Path(tempfile.mkdtemp()) / "empty")
out3 = Path(tempfile.mkdtemp()) / "x.txt"
r = rpc([INIT, call(2, "bubble_shield_write", {"path": str(out3), "content": "hi ⟦NOM_0001⟧"})],
        home=home3, nerd_port="1")
check("write without vault → isError", r[2]["result"].get("isError") is True)
check("write without vault → no file", not out3.exists())

# ---------------------------------------------------------------------------
# 9. bubble_shield_status (daemon DOWN) → correct shape
# ---------------------------------------------------------------------------
print("\n--- 9. bubble_shield_status (daemon DOWN) ---")
r = rpc([INIT, call(2, "bubble_shield_status", {})],
        home=str(Path(tempfile.mkdtemp()) / "h_stat_down"), nerd_port="1")
res9 = r[2]["result"]
check("bubble_shield_status → no isError", not res9.get("isError"))
body9 = text(r[2])
check("bubble_shield_status (down) → contains NER hors-ligne indicator",
      "NER hors-ligne" in body9 or "down" in body9)
# parse the JSON block embedded in the summary
try:
    json_block = body9[body9.index("{"):]
    st9 = json.loads(json_block)
    check("bubble_shield_status (down) → ner=down", st9.get("ner") == "down")
    check("bubble_shield_status (down) → daemon_reachable=false", st9.get("daemon_reachable") is False)
    check("bubble_shield_status (down) → ml_pack_installed key present", "ml_pack_installed" in st9)
    check("bubble_shield_status (down) → launchagent_loaded key present", "launchagent_loaded" in st9)
except Exception as e:
    check(f"bubble_shield_status JSON parse: {e}", False)
    check("bubble_shield_status (down) → ner=down", False)
    check("bubble_shield_status (down) → daemon_reachable=false", False)
    check("bubble_shield_status (down) → ml_pack_installed key present", False)
    check("bubble_shield_status (down) → launchagent_loaded key present", False)

# ---------------------------------------------------------------------------
# 10. bubble_shield_status (daemon UP) → ner=active
# ---------------------------------------------------------------------------
print("\n--- 10. bubble_shield_status (daemon UP) ---")
r = rpc([INIT, call(2, "bubble_shield_status", {})],
        home=str(Path(tempfile.mkdtemp()) / "h_stat_up"), nerd_port=str(mock_port))
res10 = r[2]["result"]
check("bubble_shield_status (up) → no isError", not res10.get("isError"))
body10 = text(r[2])
try:
    json_block10 = body10[body10.index("{"):]
    st10 = json.loads(json_block10)
    check("bubble_shield_status (up) → ner=active", st10.get("ner") == "active")
    check("bubble_shield_status (up) → daemon_reachable=true", st10.get("daemon_reachable") is True)
except Exception as e:
    check(f"bubble_shield_status (up) JSON parse: {e}", False)
    check("bubble_shield_status (up) → ner=active", False)
    check("bubble_shield_status (up) → daemon_reachable=true", False)

# ---------------------------------------------------------------------------
# 11. Setup tools (unchanged)
# ---------------------------------------------------------------------------
print("\n--- 11. Setup tools ---")
r = rpc([INIT, call(2, "bubble_shield_setup_ml", {"action": "status"})],
        home=str(Path(tempfile.mkdtemp()) / "h"))
check("setup_ml status returns a state", text(r[2]).startswith("["))

r = rpc([INIT, call(2, "bubble_shield_setup_ocr", {"action": "status"})],
        home=str(Path(tempfile.mkdtemp()) / "h"))
check("setup_ocr status returns a state", text(r[2]).startswith("["))

# ---------------------------------------------------------------------------
# 12. bubble_shield_enable_global: on/off/status + MERGE (unchanged)
# ---------------------------------------------------------------------------
print("\n--- 12. enable_global ---")
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

# ---------------------------------------------------------------------------
# 13. bubble_shield_mail_read — DISABLED by default (BUBBLE_SHIELD_ENABLE_MAIL
#     unset) → isError with the "module désactivé" message, no dispatch into
#     the mail path at all.
# ---------------------------------------------------------------------------
print("\n--- 13a. mail_read disabled by default → isError (gate) ---")
home_mail_gate = str(Path(tempfile.mkdtemp()) / "h_mail_gate")
r = rpc([INIT, call(2, "bubble_shield_mail_read", {"query": "ALL", "max": 3})],
        home=home_mail_gate, nerd_port="1")
res13a = r[2]["result"]
check("mail_read disabled by default → isError", res13a.get("isError") is True)
check("mail_read disabled by default → mentions désactivé",
      "désactivé" in text(r[2]))

# ---------------------------------------------------------------------------
# 13b. bubble_shield_mail_read (BUBBLE_SHIELD_ENABLE_MAIL=1) — no creds
#      configured → isError (config fail-closed, reserve path still works)
#      (full fetch+anonymise+fail-closed wiring is covered in
#      test_bubble_shield_mail.py)
# ---------------------------------------------------------------------------
print("\n--- 13c. mail_read (flag on) no creds → isError ---")
home_mail = str(Path(tempfile.mkdtemp()) / "h_mail")
r = rpc([INIT, call(2, "bubble_shield_mail_read", {"query": "ALL", "max": 3})],
        home=home_mail, nerd_port="1", enable_mail=True)
res13 = r[2]["result"]
check("mail_read without creds → isError", res13.get("isError") is True)
body13 = text(r[2])
check("mail_read without creds → mentions IMAP/identifiants (no raw mail)",
      "IMAP" in body13 or "identifiant" in body13)

# ---------------------------------------------------------------------------
# Teardown mock daemon
# ---------------------------------------------------------------------------
mock_srv.shutdown()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
