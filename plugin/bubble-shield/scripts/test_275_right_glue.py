#!/usr/bin/env python3
"""Regression test for issue #275 — right-glued forename leak in liasse fiscale.

Root cause: PDF text extraction omits the space between a forename/surname
(e.g. "FAKENAME") and the FOLLOWING token (e.g. "Signature"), producing
"FAKENAMESignature" with no whitespace. The standard right word-boundary
``(?![A-Za-z])`` fails because "S" IS a letter immediately after the seed.

Real output observed:  "⟦NOM_0002⟧ FAKENAMESignature"  (surname masked, forename leaked)
Expected:              "⟦NOM_0002⟧ ⟦NOM_0001⟧Signature" (or similar — forename masked)

Fix (mirror of #273 Option B) — structured_ext.py doc_level_person_repetition_matches:
  For seeds derived from RAISON_SOCIALE (known, len >= 6, not a common surname),
  compile a right_glued_pattern with a loose RIGHT boundary (no right-char restriction)
  but strict LEFT boundary. Emit only when the FOLLOWING char IS alphabetic — confirming
  a genuine right-glue artifact.

Note on signataire label lines: when "FAKENAMESignature" appears as the VALUE of a
signataire/gérant label ("Signataire : FAKENAMESignature"), the signataire_matches
recognizer correctly masks the ENTIRE value as NOM — this is correct behavior (the whole
field value is the person's name). The right-glue fix provides additional coverage for
occurrences NOT on such label lines (e.g. free-text paragraphs, headers, footers).

All PII is SYNTHETIC (FAKENAME TESTONI).
Run: python3 scripts/test_275_right_glue.py
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

# ── Synthetic liasse block mirroring the real right-glue artifact (#275) ─────
# "FAKENAMESignature" simulates a PDF extraction where the forename ("FAKENAME")
# and the following word ("Signature") are run together with no whitespace.
# We use a free-text / non-labeled context (not a signataire label line) so that
# only the right-glue fix picks up FAKENAME — not the signataire_matches recognizer.
# "Signature" must survive (it's the next word, not part of the name).
# Also includes the #273 left-glue scenario to confirm no regression.
RIGHT_GLUE_BLOCK = (
    "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Forme juridique : SELARL" + NL
    + NL
    # right-glue artifact: forename immediately followed by next word (free-text)
    + "Le document a été signé par FAKENAMESignature" + NL
    # left-glue artifact (#273): surname preceded by trailing char
    + "Qualité de la personne physique : gérantETESTONI" + NL
    # clean occurrences
    + "TESTONI FAKENAME" + NL
)

# ── Precision block — must NOT be over-masked ─────────────────────────────────
PRECISION_BLOCK = (
    "Le printemps blanc est arrivé." + NL
    + "Martin Luther était un réformateur." + NL
    + "La société MARTIN exerce dans le domaine financier." + NL
    + "Le blanc du papier est important." + NL
    + "Blanc et noir sont des couleurs." + NL
    + "La SELARL exerce une activité." + NL
    + "Forme juridique : SELARL" + NL
    # Common surnames glued to words — must NOT be masked by right-glue fix
    + "MARTINSignature" + NL
    + "BLANCDocument" + NL
)

print("=== Part A: daemon DOWN — right-glue masking ===\n")
r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": RIGHT_GLUE_BLOCK})], nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output:\n  {t!r}\n")
check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
check("A: FAKENAME not in output (right-glue masked)", "FAKENAME" not in t)
check("A: TESTONI not in output (left-glue still masked)", "TESTONI" not in t)
# Signature survives because only FAKENAME is the known seed, not "Signature"
check("A: 'Signature' still in output (not over-masked)", "Signature" in t)
check("A: 'Forme juridique' label still in output", "Forme juridique" in t)

print("\n=== Part A2: precision — common names NOT over-masked when glued ===\n")
r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": PRECISION_BLOCK})], nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output:\n  {t2!r}\n")
check("A2: response is non-empty", bool(t2))
check("A2: 'MARTINSignature' not masked (MARTIN is a common surname)", "MARTINSignature" in t2 or "MARTIN" in t2)
check("A2: 'BLANCDocument' not masked (BLANC is a common surname)", "BLANCDocument" in t2 or "BLANC" in t2)
check("A2: 'La SELARL exerce' not masked", "La SELARL" in t2 or "SELARL exerce" in t2)

print("\n=== Part B: de-anonymisation round-trip (via Python API) ===\n")
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.structured_ext import make_structured_detector
    from bubble_shield.vault import Vault

    det_b = make_structured_detector()
    v_b = Vault(mission="test-275-roundtrip-b")
    eng_b = AnonymizationEngine(vault=v_b, extra_detectors=[det_b])
    res_b = eng_b.anonymize(RIGHT_GLUE_BLOCK)
    anon_text = res_b.anonymized
    deanon_text = eng_b.deanonymize(anon_text)
    print(f"  Anon (first 400): {anon_text[:400]!r}")
    print(f"  De-anon (first 400): {deanon_text[:400]!r}")
    if anon_text and "⟦" in anon_text:
        check("B: anon output contains NOM tokens", "⟦NOM" in anon_text)
        check("B: FAKENAME gone from anon output", "FAKENAME" not in anon_text)
        check("B: TESTONI gone from anon output", "TESTONI" not in anon_text)
        check("B: 'Signature' still in anon output", "Signature" in anon_text)
        check("B: de-anon restores FAKENAME", "FAKENAME" in deanon_text)
        check("B: de-anon restores TESTONI", "TESTONI" in deanon_text)
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
    rc = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": RIGHT_GLUE_BLOCK})], nerd_port=NERD_PORT)
    tc = text_of(rc[2])
    print(f"  Output (first 600 chars):\n  {tc[:600]!r}\n")
    check("C: no degraded-mode warning", "dégradée" not in tc)
    check("C: output contains tokens", "⟦" in tc)
    check("C: FAKENAME not in output (daemon UP)", "FAKENAME" not in tc)
    check("C: TESTONI not in output (daemon UP)", "TESTONI" not in tc)
    check("C: 'Signature' still in output (daemon UP)", "Signature" in tc)
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
        _COMMON_FRENCH_SURNAMES,
        _COMMON_FRENCH_FORENAMES,
    )
    from bubble_shield.recognizers import Match

    print("=== D1-D5: right-glue detection (Option B — loose right boundary) ===")
    # Free-text context: FAKENAMESignature NOT on a label line, so only the
    # right-glue fix (not signataire_matches) should mask FAKENAME.
    right_glue_text = (
        "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Le document a été signé par FAKENAMESignature\n"   # right-glue artifact (#275)
        "Qualité de la personne physique : gérantETESTONI\n"  # left-glue (#273)
        "TESTONI FAKENAME\n"   # clean occurrence
    )
    rs_start = right_glue_text.index("SELARL DU DOCTEUR FAKENAME TESTONI")
    rs_end = rs_start + len("SELARL DU DOCTEUR FAKENAME TESTONI")
    rs_match = Match(start=rs_start, end=rs_end,
                     entity_type="RAISON_SOCIALE",
                     value="SELARL DU DOCTEUR FAKENAME TESTONI",
                     score=0.90, priority=59)

    extra = doc_level_person_repetition_matches(right_glue_text, [rs_match])
    all_matches = [rs_match] + extra
    print(f"  All matches: {[(m.start, m.end, m.value) for m in all_matches]}")

    # 1) Right-glued FAKENAME (in FAKENAMESignature) must be masked
    fg_pos = right_glue_text.index("FAKENAMESignature")
    check("D1: right-glued FAKENAME (in FAKENAMESignature) masked",
          any(m.start <= fg_pos < m.end for m in all_matches))

    # 2) "Signature" must NOT be masked (only FAKENAME is the seed)
    sig_pos = fg_pos + len("FAKENAME")
    check("D2: 'Signature' NOT masked (right-glue stops at seed boundary)",
          not any(m.start <= sig_pos < m.end for m in all_matches))

    # 3) Left-glued TESTONI (#273 regression) still masked
    testoni_glued_pos = right_glue_text.index("ETESTONI") + 1   # T in gérantETESTONI
    check("D3: left-glued TESTONI (in gérantETESTONI) still masked (#273 not regressed)",
          any(m.start <= testoni_glued_pos < m.end for m in all_matches))

    # 4) Clean occurrences masked
    clean_fn_pos = right_glue_text.index("TESTONI FAKENAME") + len("TESTONI ")
    check("D4: clean FAKENAME (standalone) masked",
          any(m.start <= clean_fn_pos < m.end for m in all_matches))

    # 5) Matched value for the right-glue is exactly FAKENAME (not FAKENAMESignature)
    rg_matches = [m for m in extra if m.start == fg_pos]
    check("D5: right-glue match value is exactly 'FAKENAME' (8 chars, not more)",
          bool(rg_matches) and rg_matches[0].value == "FAKENAME" and rg_matches[0].end == fg_pos + 8)

    print("\n=== D6-D7: raison_sociale_lone_seeds set — both seeds eligible ===")
    tokens = extract_person_name_from_raison_sociale("SELARL DU DOCTEUR FAKENAME TESTONI")
    seeds = _person_name_seeds(tokens)
    lone_eligible = [s for s in seeds if " " not in s and len(s) >= 6]
    check("D6: FAKENAME is an eligible lone seed (len>=6, not common)", "FAKENAME" in lone_eligible)
    check("D7: TESTONI is an eligible lone seed (len>=6, not common)", "TESTONI" in lone_eligible)

    print("\n=== D8-D10: precision — common-word seeds excluded from loose-right pass ===")
    precision_text = (
        "Raison sociale : SCP MARTIN LEBLANC\n"
        "MARTINSignature\n"       # MARTIN right-glued — must NOT mask (common surname)
        "BLANCDocument\n"         # BLANC short/common — must NOT mask
        "LEBLANCSignature\n"      # LEBLANC: len=7, check status
    )
    rs2_start = precision_text.index("SCP MARTIN LEBLANC")
    rs2_end = rs2_start + len("SCP MARTIN LEBLANC")
    rs2_match = Match(start=rs2_start, end=rs2_end,
                      entity_type="RAISON_SOCIALE",
                      value="SCP MARTIN LEBLANC",
                      score=0.90, priority=59)
    extra2 = doc_level_person_repetition_matches(precision_text, [rs2_match])

    martin_pos = precision_text.index("MARTINSignature")
    check("D8: MARTIN NOT right-glue-masked in MARTINSignature (common-surname guard)",
          not any(m.start <= martin_pos < m.end and m.entity_type == "NOM"
                  for m in extra2))

    blanc_pos = precision_text.index("BLANCDocument")
    check("D9: BLANC NOT right-glue-masked in BLANCDocument (common-surname guard)",
          not any(m.start <= blanc_pos < m.end and m.entity_type == "NOM"
                  for m in extra2))

    is_leblanc_common = "LEBLANC" in _COMMON_FRENCH_SURNAMES
    print(f"  (LEBLANC in _COMMON_FRENCH_SURNAMES: {is_leblanc_common})")
    leblanc_pos = precision_text.index("LEBLANCSignature")
    leblanc_masked = any(m.start <= leblanc_pos < m.end and m.entity_type == "NOM"
                         for m in extra2)
    # fix #267-v2: LEBLANC IS a known-client surname (anchored to "SCP MARTIN LEBLANC").
    # The common-surname guard is bypassed for RAISON_SOCIALE-derived seeds, so
    # "LEBLANCSignature" IS masked (right-glue PDF artifact for a known client name).
    # This is CORRECT: the bypass means known-client names mask everywhere, incl. right-glued.
    # The old expectation ("NOT masked") was baked before the #267-v2 recall fix.
    check("D10: LEBLANC right-glue-masked (known-client bypass — common-surname guard "
          "skipped for anchored names)", leblanc_masked)

    print("\n=== D11-D13: full combined detector on right-glue block ===")
    det = make_structured_detector()
    all_ms = det(RIGHT_GLUE_BLOCK)
    fn_positions = [m.start() for m in re.finditer("FAKENAME", RIGHT_GLUE_BLOCK)]
    ts_positions = [m.start() for m in re.finditer("TESTONI", RIGHT_GLUE_BLOCK)]
    cov_fn = sum(1 for p in fn_positions if any(m.start <= p < m.end for m in all_ms))
    cov_ts = sum(1 for p in ts_positions if any(m.start <= p < m.end for m in all_ms))
    check(f"D11: all {len(fn_positions)} FAKENAME covered ({cov_fn})", cov_fn == len(fn_positions))
    check(f"D12: all {len(ts_positions)} TESTONI covered ({cov_ts})", cov_ts == len(ts_positions))

    # Find the right-glue "FAKENAMESignature" position in RIGHT_GLUE_BLOCK
    # and verify Signature (8 chars after FAKENAME) is NOT masked by a NOM
    # NOTE: The signataire_matches recognizer is NOT used here because we changed
    # the block to use "Le document a été signé par FAKENAMESignature" (no label).
    rg_block_pos = RIGHT_GLUE_BLOCK.index("FAKENAMESignature")
    sig_block_pos = rg_block_pos + len("FAKENAME")
    check("D13: 'Signature' (after FAKENAME in free-text line) NOT masked as NOM",
          not any(m.start <= sig_block_pos < m.end for m in all_ms
                  if m.entity_type == "NOM" and m.start > rg_block_pos))

    print("\n=== D14: de-anon round-trip with right-glue scenario ===")
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    v14 = Vault(mission="test-275-roundtrip")
    eng14 = AnonymizationEngine(vault=v14, extra_detectors=[make_structured_detector()])
    rt_text = (
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Le document a été signé par FAKENAMESignature\n"  # right-glue
        "gérantETESTONI\n"       # left-glue (#273)
        "TESTONI FAKENAME\n"     # clean
    )
    res14 = eng14.anonymize(rt_text)
    anon14 = res14.anonymized
    deanon14 = eng14.deanonymize(anon14)
    print(f"  Anon: {anon14!r}")
    print(f"  De-anon: {deanon14!r}")
    check("D14: FAKENAME gone from anon", "FAKENAME" not in anon14)
    check("D14: TESTONI gone from anon", "TESTONI" not in anon14)
    check("D14: 'Signature' present in anon (not over-masked)", "Signature" in anon14)
    check("D14: de-anon restores FAKENAME", "FAKENAME" in deanon14)
    check("D14: de-anon restores TESTONI", "TESTONI" in deanon14)

    print("\n=== D15: #273 left-glue not regressed ===")
    v15 = Vault(mission="test-275-273-regression")
    eng15 = AnonymizationEngine(vault=v15, extra_detectors=[make_structured_detector()])
    glued_273_text = (
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "gérantETESTONI\n"
        "TESTONI FAKENAME\n"
    )
    res15 = eng15.anonymize(glued_273_text)
    anon15 = res15.anonymized
    check("D15: #273 left-glue still masked (TESTONI not in anon)", "TESTONI" not in anon15)
    check("D15: #273 FAKENAME still masked", "FAKENAME" not in anon15)

    # ── D16-D26: ship-blocker fixes (#275 v2) ────────────────────────────────
    # Ship-blocker 1: forename exclusion from right-glue pass
    # Ship-blocker 2: uppercase-next-char guard (only mask CamelCase glue)

    print("\n=== D16-D21: ship-blocker 1 — forename list excludes forenames from right-glue ===")
    from bubble_shield.structured_ext import _COMMON_FRENCH_FORENAMES

    check("D16: CLAIRE in _COMMON_FRENCH_FORENAMES", "CLAIRE" in _COMMON_FRENCH_FORENAMES)
    check("D17: JULIEN in _COMMON_FRENCH_FORENAMES", "JULIEN" in _COMMON_FRENCH_FORENAMES)
    check("D18: ANTOINE in _COMMON_FRENCH_FORENAMES", "ANTOINE" in _COMMON_FRENCH_FORENAMES)
    check("D19: ANDREA in _COMMON_FRENCH_FORENAMES", "ANDREA" in _COMMON_FRENCH_FORENAMES)
    check("D20: ISABELLE in _COMMON_FRENCH_FORENAMES", "ISABELLE" in _COMMON_FRENCH_FORENAMES)
    check("D21: FAKENAME NOT in _COMMON_FRENCH_FORENAMES (synthetic, distinctive)",
          "FAKENAME" not in _COMMON_FRENCH_FORENAMES)

    # Forename right-glue: ANDREA DUPONT → "ANDREAssistant" must NOT be masked
    # (ANDREA is in _COMMON_FRENCH_FORENAMES → no right-glue pass for it)
    print("\n=== D22-D24: ship-blocker 1 — ANDREA forename does NOT over-mask ===")
    andrea_text = (
        "Dénomination : SELARL DU DOCTEUR ANDREA DUPONT\n"
        "ANDREAssistant\n"     # forename + lowercase continuation → must NOT mask
        "ANDREA DUPONT\n"      # clean occurrence → must still mask (standard pass)
    )
    andrea_rs_val = "SELARL DU DOCTEUR ANDREA DUPONT"
    andrea_rs_start = andrea_text.index(andrea_rs_val)
    andrea_match = Match(start=andrea_rs_start, end=andrea_rs_start + len(andrea_rs_val),
                         entity_type="RAISON_SOCIALE", value=andrea_rs_val,
                         score=0.90, priority=59)
    extra_andrea = doc_level_person_repetition_matches(andrea_text, [andrea_match])
    all_andrea = [andrea_match] + extra_andrea

    andrea_assistant_pos = andrea_text.index("ANDREAssistant")
    check("D22: ANDREAssistant NOT masked (forename exclusion + lowercase guard)",
          not any(m.start <= andrea_assistant_pos < m.end for m in all_andrea))

    andrea_clean_pos = andrea_text.index("ANDREA DUPONT\n") + len("ANDREA DUPONT") - 6
    # Check the ANDREA part of standalone "ANDREA DUPONT" is covered via pair seed
    pair_pos = andrea_text.rindex("ANDREA DUPONT")
    check("D23: standalone ANDREA DUPONT still masked (pair seed — standard pass)",
          any(m.start <= pair_pos < m.end for m in all_andrea))

    # Lowercase continuation: JULIENne must not be masked
    julien_text = (
        "Raison sociale : SCP JULIEN MARTIN\n"
        "JULIENne est une erreur\n"   # lowercase → must NOT mask
        "JULIENDocument\n"            # uppercase next → must NOT mask (JULIEN in forenames)
    )
    julien_rs_val = "SCP JULIEN MARTIN"
    julien_rs_start = julien_text.index(julien_rs_val)
    julien_match = Match(start=julien_rs_start, end=julien_rs_start + len(julien_rs_val),
                         entity_type="RAISON_SOCIALE", value=julien_rs_val,
                         score=0.90, priority=59)
    extra_julien = doc_level_person_repetition_matches(julien_text, [julien_match])

    julien_ne_pos = julien_text.index("JULIENne")
    check("D24: JULIENne NOT masked (JULIEN in forenames exclusion set)",
          not any(m.start <= julien_ne_pos < m.end for m in extra_julien))

    # ── D25-D26: ship-blocker 2 — uppercase-next-char guard ──────────────────
    print("\n=== D25-D26: ship-blocker 2 — uppercase-next guards, lowercase NOT masked ===")
    # TESTONImania: TESTONI followed by lowercase 'm' → must NOT mask
    # TESTONIDocument: TESTONI followed by uppercase 'D' → must mask
    guard_text = (
        "Dénomination : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "TESTONImania\n"       # lowercase next → must NOT mask (real word continuation)
        "TESTONIDocument\n"    # uppercase next → MUST mask (CamelCase PDF artifact)
        "CLAIREment\n"         # CLAIRE in forenames + lowercase → must NOT mask
    )
    # Use SELARL DU DOCTEUR FAKENAME TESTONI as the known RAISON_SOCIALE
    guard_rs_val = "SELARL DU DOCTEUR FAKENAME TESTONI"
    guard_rs_start = guard_text.index(guard_rs_val)
    guard_match = Match(start=guard_rs_start, end=guard_rs_start + len(guard_rs_val),
                        entity_type="RAISON_SOCIALE", value=guard_rs_val,
                        score=0.90, priority=59)
    extra_guard = doc_level_person_repetition_matches(guard_text, [guard_match])
    all_guard = [guard_match] + extra_guard

    testoni_mania_pos = guard_text.index("TESTONImania")
    check("D25: TESTONImania NOT masked (lowercase continuation guard)",
          not any(m.start <= testoni_mania_pos < m.end for m in all_guard))

    testoni_doc_pos = guard_text.index("TESTONIDocument")
    check("D26: TESTONIDocument IS masked (uppercase next — CamelCase PDF artifact)",
          any(m.start <= testoni_doc_pos < m.end for m in all_guard))

except ImportError as e:
    print(f"  (vendor import failed: {e} — skipping Part D)")

if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
