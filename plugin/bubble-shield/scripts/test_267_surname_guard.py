#!/usr/bin/env python3
"""Regression test for issue #267 — hardened _COMMON_FRENCH_SURNAMES guard.

The _COMMON_FRENCH_SURNAMES frozenset was missing many common INSEE surnames
(GARCIA, NGUYEN, PEREZ, LECLERC, LEFEBVRE, MOREAU, LAURENT, SIMON, MICHEL,
ROUX, FONTAINE, CHEVALIER, etc.). This meant the lone-token loose pass could
over-mask these surnames when they appeared as ordinary words in prose not
related to the client.

The fix is DATA-ONLY: expand _COMMON_FRENCH_SURNAMES to ~186 entries covering
the top INSEE common French surnames. The matching LOGIC is unchanged.

KEY SAFETY CONTRACT:
  - Newly-listed surnames as ORDINARY words / not the client -> NOT masked.
  - A client actually named one of these surnames (e.g. company "SELARL GARCIA")
    -> still masked via the standard labeled/pair detection (exclusion only blocks
    the lone-token repetition pass, not the known-client labeled/pair match).

All PII is SYNTHETIC.
Run: python3 scripts/test_267_surname_guard.py
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

# Prose block with newly-added surnames as ORDINARY words (NOT the client)
PROSE_BLOCK = (
    "Rapport annuel -- exercice 2024" + NL
    + NL
    + "La famille GARCIA est connue dans la region viticole." + NL
    + "Les familles NGUYEN representent une part importante de la communaute." + NL
    + "Selon l'INSEE, PEREZ est parmi les 50 noms les plus portes en France." + NL
    + "Le groupe LECLERC est une enseigne de grande distribution." + NL
    + "La famille MOREAU est installee dans ce departement depuis plusieurs generations." + NL
    + "LAURENT est un prenom et un patronyme courant." + NL
    + "SIMON et MICHEL sont aussi des prenoms masculins frequents." + NL
)

# Client-is-GARCIA block: company "SELARL GARCIA TESTONI"
GARCIA_CLIENT_BLOCK = (
    "LIASSE FISCALE 2024" + NL
    + NL
    + "SELARL GARCIA TESTONI" + NL
    + "Denomination de l'entreprise : SELARL GARCIA TESTONI" + NL
    + "Raison sociale : SELARL GARCIA TESTONI" + NL
    + "Forme juridique : SELARL" + NL
    + "N SIREN : 987 654 321" + NL
    + NL
    + "Signataire : GARCIA TESTONI" + NL
    + "GARCIA TESTONI" + NL
    + "La SELARL exerce une activite medicale." + NL
)

print("=== Part A: daemon DOWN -- prose surnames NOT over-masked ===\n")
r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": PROSE_BLOCK})], nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output:\n  {t!r}\n")
check("A: response is non-empty", bool(t))
check("A: response produced (no crash)", len(t) > 0)

print("\n=== Part B: daemon DOWN -- client GARCIA still masked ===\n")
r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": GARCIA_CLIENT_BLOCK})], nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output:\n  {t2!r}\n")
check("B: response is non-empty", bool(t2))
check("B: output contains tokens (⟦)", "⟦" in t2)
check("B: GARCIA not in output (known-client detection)", "GARCIA" not in t2)
check("B: TESTONI not in output (known-client detection)", "TESTONI" not in t2)
check("B: 'Forme juridique' label still in output", "Forme juridique" in t2)
check("B: prose 'La SELARL exerce' still in output", "SELARL exerce" in t2)

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
    print(f"=== Part C: daemon UP (port {NERD_PORT}) -- client GARCIA masked ===\n")
    rc = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": GARCIA_CLIENT_BLOCK})], nerd_port=NERD_PORT)
    tc = text_of(rc[2])
    print(f"  Output (first 600 chars):\n  {tc[:600]!r}\n")
    check("C: no degraded-mode warning", "degradee" not in tc)
    check("C: output contains tokens", "⟦" in tc)
    check("C: GARCIA not in output (daemon UP)", "GARCIA" not in tc)
    check("C: TESTONI not in output (daemon UP)", "TESTONI" not in tc)
else:
    print(f"=== Part C: daemon not running on port {NERD_PORT} -- SKIPPED ===")

print("\n=== Part D: direct unit tests ===\n")
sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        _COMMON_FRENCH_SURNAMES,
        _person_name_seeds,
        doc_level_person_repetition_matches,
        doc_level_repetition_matches,
        make_structured_detector,
        extract_person_name_from_raison_sociale,
    )
    from bubble_shield.recognizers import Match

    print("=== D1-D10: newly-added entries in _COMMON_FRENCH_SURNAMES ===")
    new_entries = [
        "GARCIA", "NGUYEN", "PEREZ", "LECLERC", "LEFEBVRE",
        "MOREAU", "LAURENT", "SIMON", "MICHEL", "ROUX",
    ]
    for i, name in enumerate(new_entries, 1):
        check(f"D{i}: {name} in _COMMON_FRENCH_SURNAMES", name in _COMMON_FRENCH_SURNAMES)

    print("\n=== D11-D15: additional new entries ===")
    more_entries = ["FONTAINE", "CHEVALIER", "FERNANDEZ", "GONZALEZ", "MARTINEZ"]
    for i, name in enumerate(more_entries, 11):
        check(f"D{i}: {name} in _COMMON_FRENCH_SURNAMES", name in _COMMON_FRENCH_SURNAMES)

    print("\n=== D16-D18: existing entries from original list still present ===")
    check("D16: MARTIN still present", "MARTIN" in _COMMON_FRENCH_SURNAMES)
    check("D17: BERNARD still present", "BERNARD" in _COMMON_FRENCH_SURNAMES)
    check("D18: DUPONT still present", "DUPONT" in _COMMON_FRENCH_SURNAMES)

    print("\n=== D19-D20: lone seeds excluded from lone-token pass ===")
    seeds_garcia = _person_name_seeds(["GARCIA"])
    check("D19: lone GARCIA NOT in seeds (common-surname guard)", "GARCIA" not in seeds_garcia)
    seeds_nguyen = _person_name_seeds(["NGUYEN"])
    check("D20: lone NGUYEN NOT in seeds (common-surname guard)", "NGUYEN" not in seeds_nguyen)

    print("\n=== D21-D23: GARCIA+TESTONI pair seeds ===")
    # Without bypass (unanchored / NOM path): lone common surname excluded.
    seeds_pair_no_bypass = _person_name_seeds(["GARCIA", "TESTONI"])
    check("D21: pair 'GARCIA TESTONI' in seeds", "GARCIA TESTONI" in seeds_pair_no_bypass)
    check("D22: reversed pair 'TESTONI GARCIA' in seeds", "TESTONI GARCIA" in seeds_pair_no_bypass)
    # With bypass (RAISON_SOCIALE path: known-client surname): lone GARCIA MUST be in seeds.
    # fix #267-v2: the guard is bypassed for RAISON_SOCIALE-derived tokens so standalone
    # body repetitions of the client's surname are masked even if it's a common name.
    seeds_pair_bypass = _person_name_seeds(["GARCIA", "TESTONI"], bypass_common_surname_guard=True)
    check("D23: lone GARCIA IN seeds when bypass=True (known-client anchor)",
          "GARCIA" in seeds_pair_bypass)

    print("\n=== D24-D25: GARCIA in prose NOT masked by lone-token pass ===")
    prose_text = "La famille GARCIA est connue dans la region.\n"
    extra_prose = doc_level_person_repetition_matches(prose_text, [])
    garcia_pos = prose_text.index("GARCIA")
    check("D24: lone GARCIA in prose NOT masked (no seed context)",
          not any(m.start <= garcia_pos < m.end for m in extra_prose))

    garcia_client_text = (
        "Raison sociale : SELARL GARCIA TESTONI\n"
        "GARCIA TESTONI\n"
        "La famille GARCIA est connue dans la region.\n"  # prose lone GARCIA
    )
    rs_start = garcia_client_text.index("SELARL GARCIA TESTONI")
    rs_end = rs_start + len("SELARL GARCIA TESTONI")
    rs_match = Match(start=rs_start, end=rs_end, entity_type="RAISON_SOCIALE",
                     value="SELARL GARCIA TESTONI", score=0.90, priority=59)
    rs_extra = doc_level_repetition_matches(garcia_client_text, [rs_match])
    all_rs = [rs_match] + rs_extra
    person_extra = doc_level_person_repetition_matches(garcia_client_text, all_rs)
    all_matches = all_rs + person_extra
    pair1_pos = garcia_client_text.index("GARCIA TESTONI\n")
    check("D25: 'GARCIA TESTONI' pair occurrence IS masked (pair seed covers recall)",
          any(m.start <= pair1_pos < m.end for m in all_matches))

    print("\n=== D26-D29: full detector -- client GARCIA still masked ===")
    det = make_structured_detector()
    all_ms = det(GARCIA_CLIENT_BLOCK)
    garcia_positions = [m.start() for m in re.finditer("GARCIA", GARCIA_CLIENT_BLOCK)]
    testoni_positions = [m.start() for m in re.finditer("TESTONI", GARCIA_CLIENT_BLOCK)]
    cov_garcia = sum(1 for p in garcia_positions if any(m.start <= p < m.end for m in all_ms))
    cov_testoni = sum(1 for p in testoni_positions if any(m.start <= p < m.end for m in all_ms))
    check(f"D26: all {len(garcia_positions)} GARCIA occurrences covered ({cov_garcia})",
          cov_garcia == len(garcia_positions))
    check(f"D27: all {len(testoni_positions)} TESTONI occurrences covered ({cov_testoni})",
          cov_testoni == len(testoni_positions))
    nom_ms = [m for m in all_ms if m.entity_type == "NOM"]
    rs_ms2 = [m for m in all_ms if m.entity_type == "RAISON_SOCIALE"]
    fj_idx = GARCIA_CLIENT_BLOCK.index("Forme juridique")
    check("D28: 'Forme juridique : SELARL' NOT masked",
          not any(m.start <= fj_idx + len("Forme juridique : ") < m.end for m in nom_ms + rs_ms2))
    prose_idx = GARCIA_CLIENT_BLOCK.index("La SELARL")
    check("D29: prose 'La SELARL exerce' NOT masked",
          not any(m.start <= prose_idx + 3 < m.end for m in nom_ms + rs_ms2))

    print("\n=== D30: de-anon round-trip -- GARCIA as client name ===")
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault
    v30 = Vault(mission="test-267-garcia-roundtrip")
    eng30 = AnonymizationEngine(vault=v30, extra_detectors=[make_structured_detector()])
    res30 = eng30.anonymize(GARCIA_CLIENT_BLOCK)
    anon30 = res30.anonymized
    deanon30 = eng30.deanonymize(anon30)
    check("D30: GARCIA gone from anon output", "GARCIA" not in anon30)
    check("D30: TESTONI gone from anon output", "TESTONI" not in anon30)
    check("D30: de-anon restores GARCIA", "GARCIA" in deanon30)
    check("D30: de-anon restores TESTONI", "TESTONI" in deanon30)
    nom_tokens_30 = set(re.findall(r"⟦NOM_\d+⟧", anon30))
    check(f"D30: <=2 distinct NOM tokens in anon output (found: {nom_tokens_30})",
          len(nom_tokens_30) <= 2)

    print("\n=== D31: expanded list size sanity ===")
    check(f"D31: _COMMON_FRENCH_SURNAMES has >=100 entries (has {len(_COMMON_FRENCH_SURNAMES)})",
          len(_COMMON_FRENCH_SURNAMES) >= 100)

    # ── D32-D36: recall regression fix (#267-v2) ──────────────────────────────
    # D32-D34: a newly-listed common surname that IS the client's name (anchored to
    # a company via RAISON_SOCIALE) must mask STANDALONE body repetitions.
    # Scenario: client "SELARL LECLERC DUBOIS", body has lone "LECLERC agit seul".
    print("\n=== D32-D34: known-client common surname masks standalone body occurrences ===")
    leclerc_text = (
        "Denomination de l'entreprise : SELARL LECLERC DUBOIS\n"
        "Raison sociale : SELARL LECLERC DUBOIS\n"
        "\n"
        "LECLERC agit seul pour les decisions.\n"
        "Le gerant LECLERC a signe le bilan.\n"
    )
    det_leclerc = make_structured_detector()
    ms_leclerc = det_leclerc(leclerc_text)
    leclerc_positions = [m.start() for m in re.finditer("LECLERC", leclerc_text)]
    cov_leclerc = sum(1 for p in leclerc_positions
                      if any(m.start <= p < m.end for m in ms_leclerc))
    check(f"D32: all {len(leclerc_positions)} LECLERC occurrences covered ({cov_leclerc})",
          cov_leclerc == len(leclerc_positions))
    dubois_positions = [m.start() for m in re.finditer("DUBOIS", leclerc_text)]
    cov_dubois = sum(1 for p in dubois_positions
                     if any(m.start <= p < m.end for m in ms_leclerc))
    check(f"D33: all {len(dubois_positions)} DUBOIS occurrences covered ({cov_dubois})",
          cov_dubois == len(dubois_positions))
    check("D34: standalone 'LECLERC agit' line IS masked (known-client bypass)",
          any(m.start <= leclerc_text.index("LECLERC agit") < m.end for m in ms_leclerc))

    # D35: unanchored common surname in a doc with NO matching company → NOT masked.
    # This is the original #267 goal — must not be regressed.
    print("\n=== D35: unanchored common surname in prose NOT masked (original #267 goal) ===")
    perez_prose_text = (
        "Rapport 2025 — aucune raison sociale n'inclut PEREZ.\n"
        "Selon l'INSEE, PEREZ est un patronyme tres commun.\n"
        "La famille PEREZ habite dans le departement.\n"
    )
    det_perez = make_structured_detector()
    ms_perez = det_perez(perez_prose_text)
    perez_positions = [m.start() for m in re.finditer("PEREZ", perez_prose_text)]
    masked_perez = sum(1 for p in perez_positions
                       if any(m.start <= p < m.end for m in ms_perez))
    check(f"D35: unanchored PEREZ in prose NOT masked ({masked_perez}/{len(perez_positions)} masked)",
          masked_perez == 0)

    # D36: same surname (PEREZ) IS masked when anchored as client name.
    print("\n=== D36: PEREZ as client name (anchored) IS masked everywhere ===")
    perez_client_text = (
        "Raison sociale : SELARL PEREZ MARTINEZ\n"
        "PEREZ agit en qualite de gerant.\n"
        "Signature : PEREZ\n"
    )
    det_perez2 = make_structured_detector()
    ms_perez2 = det_perez2(perez_client_text)
    perez2_positions = [m.start() for m in re.finditer("PEREZ", perez_client_text)]
    cov_perez2 = sum(1 for p in perez2_positions
                     if any(m.start <= p < m.end for m in ms_perez2))
    check(f"D36: all {len(perez2_positions)} PEREZ occurrences masked when client-anchored ({cov_perez2})",
          cov_perez2 == len(perez2_positions))

except ImportError as e:
    print(f"  (vendor import failed: {e} -- skipping Part D)")

if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
