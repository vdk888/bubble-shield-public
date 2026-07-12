#!/usr/bin/env python3
"""Regression test for issue #266: practitioner personal name leaks in corporate/fiscal docs.

All PII is SYNTHETIC (FAKENAME TESTONI).
Run: python3 scripts/test_266_person_name_corporate.py
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
CORP_BLOCK = (
    "LIASSE FISCALE 2024" + NL
    + NL
    + "SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Dénomination de l’entreprise : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI" + NL
    + "Forme juridique : SELARL" + NL
    + "N° SIREN : 123 456 789" + NL
    + NL
    + "BLOC SIGNATAIRE" + NL
    + "Signataire : GÉRANT  TESTONI FAKENAME" + NL
    + "Signataire : TESTONI FAKENAME" + NL
    + "/ / / TESTONI FAKENAME" + NL
    + "N° département  TESTONI FAKENAME" + NL
    + NL
    + "La SELARL exerce une activité de médecine." + NL
    + "Forme juridique : SELARL" + NL
    + "Type : SAS" + NL
)

PRECISION_BLOCK = (
    "Le printemps blanc est arrivé." + NL
    + "Martin Luther était un réformateur." + NL
    + "La société MARTIN exerce dans le domaine financier." + NL
    + "Le blanc du papier est important." + NL
    + "Blanc et noir sont des couleurs." + NL
    + "La SELARL exerce une activité." + NL
    + "Forme juridique : SELARL" + NL
)

print("=== Part A: daemon DOWN ===\n")
r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CORP_BLOCK})], nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output (first 800 chars):\n  {t[:800]!r}\n")
check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
check("A: FAKENAME not in output", "FAKENAME" not in t)
check("A: TESTONI not in output", "TESTONI" not in t)
check("A: 'Forme juridique' label still in output", "Forme juridique" in t)
check("A: prose 'La' and 'activit' still in output", "La" in t and "activit" in t)

print("\n=== Part A2: precision block ===\n")
r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": PRECISION_BLOCK})], nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output:\n  {t2!r}\n")
check("A2: 'blanc' (lowercase) not masked", "blanc" in t2.lower())
check("A2: 'La SELARL exerce' not masked", "La SELARL" in t2 or "SELARL exerce" in t2)
check("A2: response is non-empty", bool(t2))

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
    print(f"=== Part B: daemon UP (port {NERD_PORT}) ===\n")
    rb = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CORP_BLOCK})], nerd_port=NERD_PORT)
    tb = text_of(rb[2])
    print(f"  Output (first 800 chars):\n  {tb[:800]!r}\n")
    check("B: no degraded-mode warning", "dégradée" not in tb)
    check("B: output contains tokens", "⟦" in tb)
    check("B: FAKENAME not in output (daemon UP)", "FAKENAME" not in tb)
    check("B: TESTONI not in output (daemon UP)", "TESTONI" not in tb)
    check("B: 'Forme juridique' still in output (daemon UP)", "Forme juridique" in tb)
else:
    print(f"=== Part B: daemon not running on port {NERD_PORT} — SKIPPED ===")

print("\n=== Part C: de-anonymisation round-trip ===\n")
home_c = str(Path(tempfile.mkdtemp()) / "bshome_c")
rc1 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CORP_BLOCK})], nerd_port=_MOCK_PORT, home=home_c)
anon_text = text_of(rc1[2])
if anon_text and "⟦" in anon_text:
    check("C: anon output contains NOM tokens", "⟦NOM" in anon_text)
    check("C: FAKENAME gone from anon output", "FAKENAME" not in anon_text)
    check("C: TESTONI gone from anon output", "TESTONI" not in anon_text)
    nom_tokens = re.findall(r"⟦NOM_\d+⟧", anon_text)
    if nom_tokens:
        unique_nom = set(nom_tokens)
        check(f"C: person-name occurrences share ≤2 NOM tokens (found: {unique_nom})", len(unique_nom) <= 2)
else:
    check("C: anon output is non-empty and has tokens", False)

print("\n=== Part D: direct unit tests ===\n")
sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        extract_person_name_from_raison_sociale, _person_name_seeds,
        _COMMON_FRENCH_SURNAMES, _RAISON_SOCIALE_PREFIXES,
        signataire_matches, doc_level_person_repetition_matches,
        make_structured_detector,
    )
    from bubble_shield.recognizers import Match

    print("=== D1-D5: extract_person_name_from_raison_sociale ===")
    toks = extract_person_name_from_raison_sociale("SELARL DU DOCTEUR FAKENAME TESTONI")
    check("D1: SELARL DU DOCTEUR -> [FAKENAME, TESTONI]", toks == ["FAKENAME", "TESTONI"])
    check("D2: SCP -> [FAKENAME, TESTONI]",
          extract_person_name_from_raison_sociale("SCP FAKENAME TESTONI") == ["FAKENAME", "TESTONI"])
    check("D3: SCM DR -> [FAKENAME, TESTONI]",
          extract_person_name_from_raison_sociale("SCM DR FAKENAME TESTONI") == ["FAKENAME", "TESTONI"])
    check("D4: bare SELARL -> no tokens", extract_person_name_from_raison_sociale("SELARL") == [])
    toks5 = extract_person_name_from_raison_sociale("SELAS DU DOCTEUR FAKENAME TESTONI AUTRE")
    check("D5: SELAS -> contains FAKENAME TESTONI", "FAKENAME" in toks5 and "TESTONI" in toks5)

    print("\n=== D6-D10: _person_name_seeds precision ===")
    seeds6 = _person_name_seeds(["FAKENAME", "TESTONI"])
    check("D6: pair FAKENAME TESTONI in seeds", "FAKENAME TESTONI" in seeds6)
    check("D6: reversed pair TESTONI FAKENAME in seeds", "TESTONI FAKENAME" in seeds6)
    check("D6: lone FAKENAME in seeds", "FAKENAME" in seeds6)
    check("D6: lone TESTONI in seeds", "TESTONI" in seeds6)
    seeds7 = _person_name_seeds(["MARTIN", "LEBLANC"])
    check("D7: pair MARTIN LEBLANC in seeds", "MARTIN LEBLANC" in seeds7)
    check("D7: lone MARTIN NOT in seeds (common)", "MARTIN" not in seeds7)
    seeds8 = _person_name_seeds(["MARTIN", "PETIT"])
    check("D8: pair MARTIN PETIT in seeds", "MARTIN PETIT" in seeds8)
    check("D8: lone MARTIN NOT in seeds", "MARTIN" not in seeds8)
    check("D8: lone PETIT NOT in seeds", "PETIT" not in seeds8)
    check("D9: single distinctive FAKENAME in seeds", "FAKENAME" in _person_name_seeds(["FAKENAME"]))
    check("D10: single common MARTIN -> no seeds", "MARTIN" not in _person_name_seeds(["MARTIN"]))

    print("\n=== D11-D14: signataire_matches ===")
    ms11 = signataire_matches("Signataire : TESTONI FAKENAME\n")
    check("D11: plain signataire caught as NOM", any(m.entity_type == "NOM" for m in ms11))
    check("D11: value contains name", any("TESTONI" in m.value or "FAKENAME" in m.value for m in ms11))
    ms12 = signataire_matches("Signataire : GÉRANT  TESTONI FAKENAME\n")
    check("D12: signataire with GÉRANT caught as NOM", any(m.entity_type == "NOM" for m in ms12))
    if ms12:
        check("D12: GÉRANT stripped from value",
              all("GÉRANT" not in m.value for m in ms12 if m.entity_type == "NOM"))
    ms13 = signataire_matches("Gérant : FAKENAME TESTONI\n")
    check("D13: 'Gérant : FAKENAME TESTONI' caught as NOM", any(m.entity_type == "NOM" for m in ms13))
    ms14 = signataire_matches("Nom (et qualité) du signataire/déclarant : TESTONI FAKENAME\n")
    check("D14: 'Nom (et qualité) du signataire' caught as NOM", any(m.entity_type == "NOM" for m in ms14))

    print("\n=== D15-D16: doc_level_person_repetition_matches ===")
    import re as _re
    corp_text2 = (
        "SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Dénomination de l’entreprise : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Signataire : TESTONI FAKENAME\n"
        "/ / / TESTONI FAKENAME\n"
        "N° département  TESTONI FAKENAME\n"
        "La SELARL exerce une activité de médecine.\n"
    )
    from bubble_shield.structured_ext import (
        form_raison_sociale_matches, forme_juridique_anchored_matches, doc_level_repetition_matches,
    )
    rs_found = form_raison_sociale_matches(corp_text2) + forme_juridique_anchored_matches(corp_text2)
    rs_found += doc_level_repetition_matches(corp_text2, rs_found)
    extra = doc_level_person_repetition_matches(corp_text2, rs_found)
    fn_pos = [m.start() for m in _re.finditer("FAKENAME", corp_text2)]
    ts_pos = [m.start() for m in _re.finditer("TESTONI", corp_text2)]
    all_found = rs_found + extra
    cov_fn = sum(1 for p in fn_pos if any(m.start <= p < m.end for m in all_found))
    cov_ts = sum(1 for p in ts_pos if any(m.start <= p < m.end for m in all_found))
    check(f"D15: all {len(fn_pos)} FAKENAME covered ({cov_fn})", cov_fn == len(fn_pos))
    check(f"D15: all {len(ts_pos)} TESTONI covered ({cov_ts})", cov_ts == len(ts_pos))

    common_doc = (
        "Le printemps blanc est arrivé.\n"
        "Martin Luther était un réformateur.\n"
        "La société MARTIN exerce dans le domaine financier.\n"
    )
    seed_m = Match(start=0, end=18, entity_type="RAISON_SOCIALE", value="SCP MARTIN LEBLANC", score=0.90, priority=59)
    extra_common = doc_level_person_repetition_matches(common_doc, [seed_m])
    nom_extra = [m for m in extra_common if m.entity_type == "NOM"]
    martin_pos = common_doc.upper().index("MARTIN")
    martin_masked = any(m.start <= martin_pos < m.end for m in nom_extra)
    check("D16: lone MARTIN in prose NOT masked (common-word guard)", not martin_masked)

    print("\n=== D17-D21: full combined detector ===")
    det = make_structured_detector()
    all_ms = det(CORP_BLOCK)
    nom_ms = [m for m in all_ms if m.entity_type == "NOM"]
    rs_ms = [m for m in all_ms if m.entity_type == "RAISON_SOCIALE"]
    fn_positions = [m.start() for m in _re.finditer("FAKENAME", CORP_BLOCK)]
    ts_positions = [m.start() for m in _re.finditer("TESTONI", CORP_BLOCK)]
    cov_fn2 = sum(1 for p in fn_positions if any(m.start <= p < m.end for m in all_ms))
    cov_ts2 = sum(1 for p in ts_positions if any(m.start <= p < m.end for m in all_ms))
    check(f"D17: all {len(fn_positions)} FAKENAME covered ({cov_fn2})", cov_fn2 == len(fn_positions))
    check(f"D18: all {len(ts_positions)} TESTONI covered ({cov_ts2})", cov_ts2 == len(ts_positions))
    fji = CORP_BLOCK.index("Forme juridique")
    check("D19: 'Forme juridique : SELARL' NOT a NOM or RAISON_SOCIALE match",
          not any(m.start <= fji + len("Forme juridique : ") < m.end for m in nom_ms + rs_ms))
    pi = CORP_BLOCK.index("La SELARL")
    check("D20: prose 'La SELARL exerce' NOT masked",
          not any(m.start <= pi + 3 < m.end for m in nom_ms + rs_ms))
    try:
        det(""); det("foo bar baz"); det("SELARL"); det("SAS\nSARL\nSCI\n")
        check("D21: combined detector fail-open (no exception)", True)
    except Exception as exc:
        check("D21: combined detector fail-open", False)
        print(f"    (error: {exc})")

    print("\n=== D22-D23: precision — common-word guard ===")
    det2 = make_structured_detector()
    ms22 = det2(PRECISION_BLOCK)
    nom22 = [m for m in ms22 if m.entity_type == "NOM"]
    bp = PRECISION_BLOCK.lower().index("blanc")
    check("D22: 'blanc' (lowercase) NOT masked as NOM", not any(m.start <= bp < m.end for m in nom22))
    sp = PRECISION_BLOCK.index("La SELARL") + 3
    check("D23: prose 'La SELARL exerce' NOT masked as NOM", not any(m.start <= sp < m.end for m in nom22))

    print("\n=== D25: vault consistency ===")
    from bubble_shield.vault import Vault
    v25 = Vault(mission="test-266-consistency")
    ms25 = det(CORP_BLOCK)
    # Apply ALL matches (NOM + RAISON_SOCIALE) in reverse order — as the real engine does.
    # Company names are RAISON_SOCIALE; personal names are NOM.
    all25 = sorted(ms25, key=lambda x: x.start, reverse=True)
    out25 = CORP_BLOCK
    for mm in all25:
        tok = v25.token_for(mm.value, mm.entity_type)
        out25 = out25[:mm.start] + tok + out25[mm.end:]
    nom_tokens_25 = set(re.findall(r"⟦NOM_\d+⟧", out25))
    check(f"D25: ≤2 distinct NOM tokens (found: {nom_tokens_25})", len(nom_tokens_25) <= 2)
    check("D25: FAKENAME not in vault-substituted output", "FAKENAME" not in out25)
    check("D25: TESTONI not in vault-substituted output", "TESTONI" not in out25)

    print("\n=== D26: de-anon fidelity \u2014 no round-trip name inversion (fix #266 Bug 1) ===")
    # "TESTONI FAKENAME" (inverted in doc) must restore as "TESTONI FAKENAME", NOT
    # as canonical "FAKENAME TESTONI".  All occurrences must share one person-number.
    from bubble_shield.engine import AnonymizationEngine
    fidelity_doc = (
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Signataire : TESTONI FAKENAME\n"
        "/ / / TESTONI FAKENAME\n"
        "N\u00b0 d\u00e9partement  TESTONI FAKENAME\n"
        "Extra : FAKENAME TESTONI\n"
    )
    v26 = Vault(mission="test-266-fidelity")
    eng26 = AnonymizationEngine(vault=v26, extra_detectors=[det])
    result26 = eng26.anonymize(fidelity_doc)
    anon26 = result26.anonymized
    deanon26 = eng26.deanonymize(anon26)
    check("D26: FAKENAME gone from anon output", "FAKENAME" not in anon26)
    check("D26: TESTONI gone from anon output", "TESTONI" not in anon26)
    check("D26: de-anon restores 'TESTONI FAKENAME' (not inverted)",
          "TESTONI FAKENAME" in deanon26)
    check("D26: de-anon restores 'FAKENAME TESTONI' (canonical form also present)",
          "FAKENAME TESTONI" in deanon26)
    all_nom_nums26 = set(re.findall(r"\u27e6NOM_(\d+)[a-z]?\u27e7", anon26))
    check(f"D26: all NOM tokens share ONE person number (found: {all_nom_nums26})",
          len(all_nom_nums26) == 1)

    print("\n=== D27: POSTE/_QUAL does NOT cross newlines (fix #266 Bug 2) ===")
    from bubble_shield.recognizers import RECOGNIZERS, detect as rec_detect
    # _QUAL used \\s+ which matches \\n -> swallowed next-line label
    role_cross_test = "g\u00e9rant des ventes\nSignataire : TESTONI FAKENAME\n"
    recs_cross27 = rec_detect(role_cross_test, RECOGNIZERS)
    poste_cross27 = [m for m in recs_cross27 if m.entity_type == "POSTE"]
    newline_pos27 = role_cross_test.index("\n")
    cross_line27 = any(m.start < newline_pos27 and m.end > newline_pos27
                       for m in poste_cross27)
    check("D27: POSTE pattern does NOT cross newline boundary", not cross_line27)
    # Signataire label must survive intact so signataire_matches fires
    poste_block27 = "Qualit\u00e9 : G\u00e9rant\nSignataire : TESTONI FAKENAME\n"
    sig_ms27 = signataire_matches(poste_block27)
    check("D27: signataire_matches still catches TESTONI FAKENAME after POSTE fix",
          any("TESTONI" in m.value or "FAKENAME" in m.value for m in sig_ms27))
    check("D27: signataire match is NOM entity type",
          any(m.entity_type == "NOM" for m in sig_ms27))
    # Same-line role qualifier must still match (no regression)
    same_line = "g\u00e9rant des ventes"
    recs_same = rec_detect(same_line, RECOGNIZERS)
    poste_same = [m for m in recs_same if m.entity_type == "POSTE"]
    check("D27: same-line role+qualifier still matches POSTE (no regression)",
          bool(poste_same))

except ImportError as e:
    print(f"  (vendor import failed: {e} — skipping Part D)")

if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
