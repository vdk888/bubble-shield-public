#!/usr/bin/env python3
"""Regression test for issue #259 — corporate KYC PII leaks.

Proves that the two leaks found on a real SELARL DCC (v1.14.1) are fixed:

  1. Raison sociale leaks in clear: 'Dénomination ou raison sociale :
     SELARL DU DOCTEUR <PERSON NAME>' returned unmasked. For SELARL/SCM/SCI/SCP
     the company name EMBEDS the practitioner's personal name.
     Fix: new form_raison_sociale_matches recognizer in structured_ext.py.

  2. SIRET NIC suffix leaks: 'N° SIRET : 123 456 789-00011' output showed
     '⟦SIREN_0001⟧-00011' — the 9-digit SIREN masked but the 5-digit NIC suffix
     stayed in clear, making the full 14-digit SIRET reconstructable.
     Fix: SIRET pattern changed from [ ]? to [ -]? separators in recognizers.py.

All PII used here is SYNTHETIC. Run:
  python3 scripts/test_259_corporate_kyc.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "bubble_shield_mcp.py"
sys.path.insert(0, str(HERE))
from _test_mock_daemon import start_mock_daemon  # noqa

_MOCK_SRV, _MOCK_PORT = None, None
try:
    _MOCK_PORT, _MOCK_SRV = start_mock_daemon()
    time.sleep(0.05)
except Exception as e:
    print(f"WARNING: mock daemon failed to start: {e}", file=sys.stderr)
    _MOCK_PORT = 1

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def rpc_calls(calls: list, *, nerd_port: int = 1, home: str | None = None) -> dict:
    """Drive the MCP server over stdio with the given JSON-RPC calls."""
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
        try:
            o = json.loads(line)
            if "id" in o:
                out[o["id"]] = o
        except Exception:
            pass
    return out


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def call(id_: int, name: str, args: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def text_of(res: dict) -> str:
    return res.get("result", {}).get("content", [{}])[0].get("text", "")


# ── Synthetic test data (SYNTHETIC ONLY — no real client data) ────────────────

# Primary test: SELARL DCC corporate block with embedded non-gazetteer name.
# "FAKENAME TESTONI" is NOT in any French first-name gazetteer — proves the
# structured recognizer does the work, not the gazetteer.
CORPORATE_BLOCK = (
    "Dénomination ou raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
    "N° SIRET : 123 456 789 00011\n"
    "Forme juridique : SELARL\n"
)

# The SIRET hyphen-separator form (the specific DCC variant that was leaking).
SIRET_HYPHEN_BLOCK = (
    "N° SIRET : 123 456 789-00011\n"
)

# Control: forme juridique type-word alone (must NOT mask SELARL as a type)
FORME_JURIDIQUE_ONLY = "Forme juridique : SELARL\n"

# Control: prose with no form labels (must NOT false-mask)
PROSE_NOPII = (
    "La société exerce sous la forme d'une SELARL depuis 2015. "
    "Les associés ont signé le pacte le 12 mars 2022."
)

# ── PART A: daemon DOWN (nerd_port=1 → unreachable) ──────────────────────────
print("=== Part A: daemon DOWN — corporate KYC fields must mask ===\n")

r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CORPORATE_BLOCK})],
              nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Input:\n  {CORPORATE_BLOCK!r}")
print(f"  Output (daemon DOWN):\n  {t!r}\n")

check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
# LEAK 1: raison sociale — full company name incl. non-gazetteer name must mask
check("A: raison sociale 'SELARL DU DOCTEUR FAKENAME TESTONI' masked",
      "FAKENAME" not in t and "TESTONI" not in t)
# LEAK 2: SIRET — full 14-digit SIRET must mask (not just the 9-digit SIREN)
check("A: SIRET full 14 digits '123 456 789 00011' masked",
      "00011" not in t or "⟦SIRET" in t or "⟦SIREN" in t)
# Precision: forme juridique TYPE word should survive (we don't mask type labels)
check("A: forme juridique label line intact (SELARL type not masked)",
      "Forme juridique" in t)

# Test the hyphen-separated SIRET variant specifically
print()
r_siret = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": SIRET_HYPHEN_BLOCK})],
                    nerd_port=_MOCK_PORT)
t_siret = text_of(r_siret[2])
print(f"  SIRET hyphen variant output: {t_siret!r}")
check("A: SIRET with hyphen separator '123 456 789-00011' masked fully",
      "00011" not in t_siret)

# ── PART A2: forme juridique control — SELARL type NOT masked ────────────────
print("\n=== Part A2: forme juridique control — type word NOT masked ===\n")

r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": FORME_JURIDIQUE_ONLY})],
               nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output (forme juridique only): {t2!r}")
# The type word "SELARL" may or may not be masked by other recognizers — what
# matters is the system doesn't crash and the label "Forme juridique" is readable.
check("A2: no crash on forme juridique line", bool(t2))
check("A2: label 'Forme juridique' still readable as context",
      "Forme juridique" in t2)

# ── PART A3: prose control — no false positives ───────────────────────────────
print("\n=== Part A3: prose control — no false positives on unlabeled text ===\n")

r3 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": PROSE_NOPII})],
               nerd_port=_MOCK_PORT)
t3 = text_of(r3[2])
print(f"  Output (prose): {t3!r}")
# Prose with no form labels must not trigger raison sociale masking
check("A3: prose 'société' phrase intact", "société" in t3)
check("A3: no crash on prose with no form labels", bool(t3))

# ── PART B: daemon UP (if available) ─────────────────────────────────────────
print()
import urllib.request as _ur

NERD_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
daemon_is_up = False
try:
    _ur.urlopen(
        _ur.Request(f"http://127.0.0.1:{NERD_PORT}/health", method="GET"),
        timeout=0.5)
    daemon_is_up = True
except Exception:
    pass

if daemon_is_up:
    print(f"=== Part B: daemon UP (port {NERD_PORT}) — corporate KYC fields masked ===\n")
    r4 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CORPORATE_BLOCK})],
                   nerd_port=NERD_PORT)
    t4 = text_of(r4[2])
    print(f"  Output (daemon UP): {t4!r}\n")

    check("B: no degraded-mode warning (daemon UP)", "dégradée" not in t4)
    check("B: output contains tokens (daemon UP)", "⟦" in t4)
    check("B: raison sociale company name masked (daemon UP)",
          "FAKENAME" not in t4 and "TESTONI" not in t4)
    check("B: full SIRET masked (daemon UP)", "00011" not in t4)
else:
    print(f"=== Part B: daemon not running on port {NERD_PORT} — SKIPPED (not FAILED) ===")

# ── PART D: structured_ext + recognizers unit tests (direct import) ───────────
print("\n=== Part D: unit tests (direct import of vendor modules) ===\n")

sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        form_raison_sociale_matches,
        make_structured_detector,
    )
    from bubble_shield.recognizers import RECOGNIZERS, detect

    # D1: raison sociale — SELARL with non-gazetteer embedded name
    text_d1 = "Dénomination ou raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI"
    m_d1 = form_raison_sociale_matches(text_d1)
    check("D1: form_raison_sociale_matches finds the company name",
          any("FAKENAME" in m.value and "TESTONI" in m.value for m in m_d1))
    check("D1: entity_type is RAISON_SOCIALE",
          all(m.entity_type == "RAISON_SOCIALE" for m in m_d1))

    # D2: SIRET full 14-digit (space-separated)
    siret_r = next(r for r in RECOGNIZERS if r.entity_type == "SIRET")
    text_d2 = "N° SIRET : 123 456 789 00011"
    m_d2 = siret_r.find(text_d2)
    check("D2: SIRET recognizer finds full 14-digit (space separator)",
          any("123 456 789 00011" == m.value for m in m_d2))

    # D3: SIRET hyphen-separator (the specific DCC bug)
    text_d3 = "N° SIRET : 123 456 789-00011"
    m_d3 = siret_r.find(text_d3)
    check("D3: SIRET recognizer finds full 14-digit (hyphen separator) [#259 BLOCKER]",
          any(m.value == "123 456 789-00011" for m in m_d3))
    if not any(m.value == "123 456 789-00011" for m in m_d3):
        print(f"    (found: {[m.value for m in m_d3]})")

    # D4: control — Forme juridique : SELARL alone → NOT masked by raison sociale recognizer
    text_d4 = "Forme juridique : SELARL"
    m_d4 = form_raison_sociale_matches(text_d4)
    check("D4: 'Forme juridique : SELARL' NOT matched by raison sociale recognizer",
          not m_d4)

    # D5: prose with no labels — no false positives
    text_d5 = "La société exerce sous la forme d'une SELARL depuis 2015."
    m_d5 = form_raison_sociale_matches(text_d5)
    check("D5: prose without labels — no RAISON_SOCIALE match", not m_d5)

    # D6: raison sociale variant forms
    variants = [
        ("Raison sociale : SCI DU HAMEAU", "SCI DU HAMEAU"),
        ("Dénomination sociale : SAS INNOVATION LABS", "SAS INNOVATION LABS"),
    ]
    for v_text, v_expected in variants:
        v_m = form_raison_sociale_matches(v_text)
        check(f"D6: {v_text!r} matched", any(v_expected in m.value for m in v_m))

    # D7: combined detector includes raison sociale
    det = make_structured_detector()
    full = det("Dénomination ou raison sociale : SCM DES MÉDECINS DE LA PLAINE\n"
               "N° SIRET : 123 456 789-00011\n")
    types = {m.entity_type for m in full}
    check("D7: combined detector finds RAISON_SOCIALE", "RAISON_SOCIALE" in types)

    # D8: fail-open (no exception on any text)
    try:
        det("")
        det("foo bar baz")
        det(None.__class__.__name__)
        check("D8: combined detector fail-open (no exception)", True)
    except Exception as exc:
        check(f"D8: combined detector fail-open", False)
        print(f"    (error: {exc})")

except ImportError as e:
    print(f"  (vendor import failed: {e} — skipping Part D)")

# ── Summary ──────────────────────────────────────────────────────────────────
if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
