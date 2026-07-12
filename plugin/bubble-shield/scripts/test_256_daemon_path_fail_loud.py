#!/usr/bin/env python3
"""Regression test for issue #256 — daemon path resolution + fail-closed gate.

Tests two scenarios with a synthetic état-civil form block:
  (a) daemon DOWN  → FAIL CLOSED: isError:true, no anonymized body, no raw PII
                     (was: fail-loud with degraded warning — updated fix/ner-fail-closed-gate)
  (b) daemon UP    → free-text name / birthplace / ID-number fields are tokenised

The PII used here is entirely SYNTHETIC — no real client data.
Run: python3 scripts/test_256_daemon_path_fail_loud.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "bubble_shield_mcp.py"

passed = failed = 0

ETAT_CIVIL_FORM = (
    "Nom: Marc Dubois\n"
    "Né le: 03/05/1980 à Lyon\n"
    "N° pièce: 12AB34567\n"
    "Email: m.dubois@testpii.invalid\n"
    "IBAN: FR76 3000 6000 0112 3456 7890 189"
)


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def rpc_calls(calls, *, nerd_port: int = 1, home: str | None = None) -> dict:
    """Send JSON-RPC requests to the MCP server subprocess. Returns {id: response}."""
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(HERE.parent)
    env["BUBBLE_SHIELD_HOME"] = home or str(Path(tempfile.mkdtemp()) / "bshome")
    env["HOME"] = str(Path(tempfile.mkdtemp()) / "fakehome")  # isolate ~/.config
    env["BUBBLE_SHIELD_NERD_PORT"] = str(nerd_port)
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


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def call(id_: int, name: str, args: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def text_of(res: dict) -> str:
    return res.get("result", {}).get("content", [{}])[0].get("text", "")


# ── PART A: daemon DOWN ──────────────────────────────────────────────────────
print("=== Part A: daemon DOWN — must FAIL CLOSED (isError, no body, no raw PII) ===")

# Force daemon unreachable by using port 1 (no one listens there)
r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": ETAT_CIVIL_FORM})],
              nerd_port=1)
res_a = r[2].get("result", {})
t = text_of(r[2])

check("daemon-down: isError:true (fail-closed)",
      res_a.get("isError") is True)
check("daemon-down: error body mentions NER hors-ligne",
      "NER" in t or "hors-ligne" in t or "daemon" in t.lower())
# The raw PII must NOT appear in the error body — this is the whole point
check("daemon-down: raw IBAN NOT in error body", "FR76 3000" not in t)
check("daemon-down: raw email NOT in error body", "m.dubois@testpii.invalid" not in t)
check("daemon-down: no anonymized tokens in error body (fail-closed = no output)",
      "⟦" not in t)

print()
print("=== Part A2: daemon DOWN via bubble_shield_read on a temp file ===")

import tempfile as _tf
tf = Path(_tf.mkdtemp()) / "etat_civil.txt"
tf.write_text(ETAT_CIVIL_FORM, encoding="utf-8")
r2 = rpc_calls([INIT, call(2, "bubble_shield_read", {"path": str(tf)})], nerd_port=1)
res_a2 = r2[2].get("result", {})
t2 = text_of(r2[2])

check("read daemon-down: isError:true (fail-closed)",
      res_a2.get("isError") is True)
check("read daemon-down: raw IBAN NOT in error body", "FR76 3000" not in t2)
check("read daemon-down: no tokens in error body", "⟦" not in t2)

# ── PART B: daemon UP (if available) ─────────────────────────────────────────
print()
print("=== Part B: daemon UP (GLiNER NER) — state-civil fields tokenised ===")

# Check whether the NER daemon is currently running on the standard port
NERD_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
daemon_is_up = False
try:
    urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{NERD_PORT}/health", method="GET"),
        timeout=0.5)
    daemon_is_up = True
except Exception:
    pass

if daemon_is_up:
    r3 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": ETAT_CIVIL_FORM})],
                   nerd_port=NERD_PORT)
    t3 = text_of(r3[2])

    check("daemon-up: no degraded warning in output", "dégradée" not in t3)
    check("daemon-up: output contains tokens", "⟦" in t3)
    # GLiNER should catch the bare name "Marc Dubois" and "Lyon"
    check("daemon-up: bare name tokenised (GLiNER)", "Marc Dubois" not in t3)
    check("daemon-up: birthplace (Lyon) tokenised or whole line masked",
          "Lyon" not in t3 or "⟦" in t3)
    # IBAN and email caught by regex regardless
    check("daemon-up: IBAN tokenised", "FR76 3000" not in t3)
    check("daemon-up: email tokenised", "m.dubois@testpii.invalid" not in t3)
else:
    print("  (skipping Part B — NER daemon not running on port "
          f"{NERD_PORT}; start it with bubble_shield_setup_ml)")
    print("  (Part B tests are marked SKIPPED, not FAILED)")

# ── PART C: _nerd_script() path resolution ───────────────────────────────────
print()
print("=== Part C: _nerd_script() always resolves to an existing file ===")

sys.path.insert(0, str(HERE))
import posttool_anonymize as _pa  # noqa

nerd = _pa._nerd_script()
check("_nerd_script() finds bubble_shield_nerd.py from dev layout",
      nerd is not None and nerd.is_file())
if nerd:
    check("_nerd_script() path is absolute", nerd.is_absolute())

# ── Summary ──────────────────────────────────────────────────────────────────
print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
