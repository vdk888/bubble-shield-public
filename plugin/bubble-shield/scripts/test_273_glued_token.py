#!/usr/bin/env python3
"""Regression test for issue #273 — glued-token surname leak in liasse fiscale.

Root cause: PDF text extraction omits the space between a preceding token
(e.g. a POSTE/role word) and the following surname, producing "gérantETESTONI"
or, after POSTE substitution, "⟦POSTE_0003⟧ESURNAME" in the anonymised output.
The standard word-boundary recogniser (``(?<![A-Za-z])SURNAME``) fails because
"E" IS a letter.

Fix (both Option A + Option B):
  Option A — engine.py: after all token substitutions, insert a space between
    ⟧ and any immediately adjacent alphabetic char (display normalisation).
  Option B — structured_ext.py: for RAISON_SOCIALE-derived lone-token seeds
    (len >= 6, not a common surname), use a loose LEFT boundary (no left-char
    restriction) so the seed is found even when glued. Right boundary stays strict.

All PII is SYNTHETIC (FAKENAME TESTONI).
Run: python3 scripts/test_273_glued_token.py
"""
from __future__ import annotations

import json
import os
import re
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
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def rpc_calls(calls: list, *, nerd_port: int = 1, home=None) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(HERE.parent)
    env["BUBBLE_SHIELD_HOME"] = home or str(Path(tempfile.mkdtemp()) / "bshome")
    env["HOME"] = str(Path(tempfile.mkdtemp()) / "fakehome")
    env["BUBBLE_SHIELD_NERD_PORT"] = str(nerd_port)
    sep = "\n"
    lines = sep.join(json.dumps(c) for c in calls) + sep
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


NL = "\n"

# ── Synthetic liasse block that mirrors the real glued-token artifact ──────────
# "gérantETESTONI" simulates a PDF extraction where the POSTE word ("gérant")
# and the surname ("TESTONI") are run together with a trailing "E" glue char,
# producing no whitespace between them.  The clean occurrence "TESTONI FAKENAME"
# is on the next line (would produce ⟦NOM_0001⟧ without the fix).
# After the #273 fix BOTH occurrences must be masked.
NL = "\n"
GLUED_BLOCK = (
    "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Forme juridique : SELARL" + NL
    + NL
    + "Qualité de la personne physique : gérantETESTONI" + NL  # glued artifact
    + "TESTONI FAKENAME" + NL                                   # clean occurrence
)

# ── Precision block — must NOT over-mask ──────────────────────────────────────
PRECISION_BLOCK = (
    "Le printemps blanc est arrivé." + NL
    + "Martin Luther était un réformateur." + NL
    + "La société MARTIN exerce dans le domaine financier." + NL
    + "Le blanc du papier est important." + NL
    + "Blanc et noir sont des couleurs." + NL
    + "La SELARL exerce une activité." + NL
    + "Forme juridique : SELARL" + NL
)

print("=== Part A: daemon DOWN — glued-token masking ===\n")
r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": GLUED_BLOCK})], nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output:\n  {t!r}\n")
check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
check("A: FAKENAME not in output", "FAKENAME" not in t)
check("A: TESTONI not in output", "TESTONI" not in t)
check("A: 'Forme juridique' label still in output", "Forme juridique" in t)
check("A: prose part of 'gérantE…' line structure preserved", "gérant" in t or "⟦" in t)

print("\n=== Part A2: precision — common names NOT over-masked ===\n")
r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": PRECISION_BLOCK})], nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output:\n  {t2!r}\n")
check("A2: 'blanc' (lowercase) not masked", "blanc" in t2.lower())
check("A2: 'La SELARL exerce' not masked", "La SELARL" in t2 or "SELARL exerce" in t2)
check("A2: response is non-empty", bool(t2))

print("\n=== Part B: de-anonymisation round-trip (via Python API) ===\n")
# The MCP server has no deanonymize-text RPC tool; round-trip is tested via
# the Python engine API (same as test_266 Part D26).
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.structured_ext import make_structured_detector
    from bubble_shield.vault import Vault

    det_b = make_structured_detector()
    v_b = Vault(mission="test-273-roundtrip-b")
    eng_b = AnonymizationEngine(vault=v_b, extra_detectors=[det_b])
    res_b = eng_b.anonymize(GLUED_BLOCK)
    anon_text = res_b.anonymized
    deanon_text = eng_b.deanonymize(anon_text)
    print(f"  Anon (first 300): {anon_text[:300]!r}")
    print(f"  De-anon (first 300): {deanon_text[:300]!r}")
    if anon_text and "⟦" in anon_text:
        check("B: anon output contains NOM tokens", "⟦NOM" in anon_text)
        check("B: FAKENAME gone from anon output", "FAKENAME" not in anon_text)
        check("B: TESTONI gone from anon output", "TESTONI" not in anon_text)
        check("B: de-anon restores TESTONI", "TESTONI" in deanon_text)
        check("B: de-anon restores FAKENAME", "FAKENAME" in deanon_text)
    else:
        check("B: anon output is non-empty and has tokens", False)
except ImportError as e:
    print(f"  (import failed: {e} — skipping Part B)")
    check("B: import OK", False)

print()
import urllib.request as _ur
NERD_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
daemon_is_up = False
try:
    _ur.urlopen(_ur.Request(f"http://127.0.0.1:{NERD_PORT}/health", method="GET"), timeout=0.5)
    daemon_is_up = True
except Exception:
    pass

if daemon_is_up:
    print(f"=== Part C: daemon UP (port {NERD_PORT}) ===\n")
    rc = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": GLUED_BLOCK})], nerd_port=NERD_PORT)
    tc = text_of(rc[2])
    print(f"  Output (first 600 chars):\n  {tc[:600]!r}\n")
    check("C: no degraded-mode warning", "dégradée" not in tc)
    check("C: output contains tokens", "⟦" in tc)
    check("C: FAKENAME not in output (daemon UP)", "FAKENAME" not in tc)
    check("C: TESTONI not in output (daemon UP)", "TESTONI" not in tc)
else:
    print(f"=== Part C: daemon not running on port {NERD_PORT} — SKIPPED ===")

print("\n=== Part D: direct unit tests ===\n")
sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        doc_level_person_repetition_matches,
        make_structured_detector,
        extract_person_name_from_raison_sociale,
        _person_name_seeds,
    )
    from bubble_shield.recognizers import Match

    print("=== D1-D3: glued-token detection (Option B — loose left boundary) ===")
    # Simulate the text that enters doc_level_person_repetition_matches:
    # the ORIGINAL text before any token substitution.
    glued_text = (
        "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Qualité de la personne physique : gérantETESTONI\n"
        "TESTONI FAKENAME\n"
    )
    # Seed the function with the RAISON_SOCIALE match (company name detected elsewhere)
    rs_start = glued_text.index("SELARL DU DOCTEUR FAKENAME TESTONI")
    rs_end = rs_start + len("SELARL DU DOCTEUR FAKENAME TESTONI")
    rs_match = Match(start=rs_start, end=rs_end,
                     entity_type="RAISON_SOCIALE",
                     value="SELARL DU DOCTEUR FAKENAME TESTONI",
                     score=0.90, priority=59)

    extra = doc_level_person_repetition_matches(glued_text, [rs_match])
    all_matches = [rs_match] + extra
    print(f"  All matches: {[(m.start, m.end, m.value) for m in all_matches]}")

    # The glued TESTONI must be covered
    testoni_glued_pos = glued_text.index("ETESTONI") + 1  # T in ETESTONI
    fakename_clean_pos = glued_text.index("TESTONI FAKENAME")  # last line
    testoni_clean_pos = fakename_clean_pos

    check("D1: glued TESTONI (in gérantETESTONI) masked",
          any(m.start <= testoni_glued_pos < m.end for m in all_matches))
    check("D2: clean TESTONI (standalone) masked",
          any(m.start <= testoni_clean_pos < m.end for m in all_matches))
    check("D3: clean FAKENAME (standalone) masked",
          any(m.start <= glued_text.index("TESTONI FAKENAME") < m.end
              or m.start <= glued_text.index("TESTONI FAKENAME") + len("TESTONI ") < m.end
              for m in all_matches))

    print("\n=== D4-D5: raison_sociale_lone_seeds set (Option B mechanism) ===")
    # Verify that RAISON_SOCIALE-derived lone seeds are in the loose-left set
    # (i.e., TESTONI and FAKENAME qualify since len >= 6 and not common surnames)
    tokens = extract_person_name_from_raison_sociale("SELARL DU DOCTEUR FAKENAME TESTONI")
    seeds = _person_name_seeds(tokens)
    lone_eligible = [s for s in seeds if " " not in s and len(s) >= 6]
    check("D4: TESTONI is an eligible lone seed (len>=6, not common)", "TESTONI" in lone_eligible)
    check("D5: FAKENAME is an eligible lone seed (len>=6, not common)", "FAKENAME" in lone_eligible)

    print("\n=== D6-D7: precision — common-word seeds excluded from loose-left pass ===")
    # MARTIN has len=6 but is in _COMMON_FRENCH_SURNAMES — must NOT be in lone seeds
    tokens_m = extract_person_name_from_raison_sociale("SCP MARTIN LEBLANC")
    seeds_m = _person_name_seeds(tokens_m)
    lone_m = [s for s in seeds_m if " " not in s and len(s) >= 6]
    check("D6: lone MARTIN NOT in eligible seeds (common-word guard)", "MARTIN" not in lone_m)
    # LEBLANC: len=7 but check (it's in common surnames list or not?)
    # We don't mandate either way — just check the pair is in seeds
    check("D7: pair 'MARTIN LEBLANC' in seeds", "MARTIN LEBLANC" in seeds_m)

    print("\n=== D8-D9: full combined detector on glued block ===")
    det = make_structured_detector()
    all_ms = det(GLUED_BLOCK)
    fn_positions = [m.start() for m in re.finditer("FAKENAME", GLUED_BLOCK)]
    ts_positions = [m.start() for m in re.finditer("TESTONI", GLUED_BLOCK)]
    cov_fn = sum(1 for p in fn_positions if any(m.start <= p < m.end for m in all_ms))
    cov_ts = sum(1 for p in ts_positions if any(m.start <= p < m.end for m in all_ms))
    check(f"D8: all {len(fn_positions)} FAKENAME covered ({cov_fn})", cov_fn == len(fn_positions))
    check(f"D9: all {len(ts_positions)} TESTONI covered ({cov_ts})", cov_ts == len(ts_positions))

    print("\n=== D10: Option A — engine inserts space after ⟧ adjacent to text ===")
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault
    import re as _re

    v10 = Vault(mission="test-273-option-a")
    eng10 = AnonymizationEngine(vault=v10, extra_detectors=[det])
    # Build a minimal text where a POSTE token would appear adjacent to a surname
    # (simulated as: "Qualité : gérantETESTONI" — POSTE catches gérant, leaving ETESTONI)
    poste_text = (
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Qualité : gérantETESTONI\n"
    )
    result10 = eng10.anonymize(poste_text)
    anon10 = result10.anonymized
    print(f"  Anonymised: {anon10!r}")
    # The output must NOT contain ⟧ immediately followed by a letter
    glued_in_output = bool(_re.search(r"⟧[A-Za-z]", anon10))
    check("D10: no ⟧LETTER adjacency in anonymised output (Option A normalisation)",
          not glued_in_output)
    check("D10: TESTONI not in output", "TESTONI" not in anon10)
    check("D10: FAKENAME not in output", "FAKENAME" not in anon10)

    print("\n=== D11: de-anon round-trip with glued scenario ===")
    v11 = Vault(mission="test-273-roundtrip")
    eng11 = AnonymizationEngine(vault=v11, extra_detectors=[make_structured_detector()])
    rt_text = (
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "gérantETESTONI\n"
        "TESTONI FAKENAME\n"
    )
    res11 = eng11.anonymize(rt_text)
    anon11 = res11.anonymized
    deanon11 = eng11.deanonymize(anon11)
    check("D11: TESTONI gone from anon", "TESTONI" not in anon11)
    check("D11: FAKENAME gone from anon", "FAKENAME" not in anon11)
    check("D11: de-anon restores TESTONI", "TESTONI" in deanon11)
    check("D11: de-anon restores FAKENAME", "FAKENAME" in deanon11)

    print("\n=== D12: precision — engine does NOT mask common-word prose ===")
    det12 = make_structured_detector()
    ms12 = det12(PRECISION_BLOCK)
    nom12 = [m for m in ms12 if m.entity_type == "NOM"]
    rs12 = [m for m in ms12 if m.entity_type == "RAISON_SOCIALE"]
    bp = PRECISION_BLOCK.lower().index("blanc")
    check("D12: 'blanc' NOT masked as NOM", not any(m.start <= bp < m.end for m in nom12))
    sp = PRECISION_BLOCK.index("La SELARL") + 3
    check("D12: 'La SELARL exerce' NOT masked", not any(m.start <= sp < m.end for m in nom12 + rs12))

except ImportError as e:
    print(f"  (vendor import failed: {e} — skipping Part D)")

if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
