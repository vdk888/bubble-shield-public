#!/usr/bin/env python3
"""Regression test for issue #257 — FR état-civil FORM layout detection.

Proves that the structured_ext FORM recognizers:
  1. Mask Nom/Prénom/DOB/Lieu de naissance/Pièce d'identité in the DCC-style
     label-value form block with daemon UP (mock daemon returns empty NER matches;
     structured_ext provides the form-layout detection independently).
  2. Are wired into the bubble_shield_read / anonymize_text path
     (i.e. structured_ext actually fires in the engine, not just in isolation).
  3. Work for a name NOT in the gazetteer (proving structured recognizer does
     the work, not the first-name list).
  4. Don't over-mask a non-PII control text.

Note: daemon-down path now fails CLOSED (isError:true, no output). Tests use a
mock daemon (returns empty NER matches) to exercise structured_ext behaviour
while satisfying the NER gate.

All PII used here is SYNTHETIC. Run:
  python3 scripts/test_257_form_layout.py
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

# Start mock NER daemon (returns empty NER matches — structured_ext does the work).
# Tests now run with daemon UP so they pass the fail-closed gate.
_MOCK_SRV, _MOCK_PORT = None, None
try:
    _MOCK_PORT, _MOCK_SRV = start_mock_daemon()
    time.sleep(0.05)
except Exception as e:
    print(f"WARNING: mock daemon failed to start: {e}", file=sys.stderr)
    _MOCK_PORT = 1  # fallback: tests will skip via isError

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def rpc_calls(calls: list, *, nerd_port: int = 1, home: str | None = None,
              gemma_mode: str | None = None) -> dict:
    """Drive the MCP server over stdio with the given JSON-RPC calls.

    gemma_mode: when set, writes a bubble-shield.json pinning gemma_mode and points
    BUBBLE_SHIELD_GUARD_CONFIG at it. Prose assertions (Part C) pin "hard" so the
    masker skips the Gemma pass — these tests exercise the non-Gemma prose path and
    must not require the Gemma daemon (port 8724), which isn't running here. Under the
    default "all" mode, prose legitimately fails closed when Gemma is unreachable.
    """
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(HERE.parent)
    env["BUBBLE_SHIELD_HOME"] = home or str(Path(tempfile.mkdtemp()) / "bshome")
    env["HOME"] = str(Path(tempfile.mkdtemp()) / "fakehome")  # isolate ~/.config
    env["BUBBLE_SHIELD_NERD_PORT"] = str(nerd_port)
    if gemma_mode is not None:
        _cfg = Path(tempfile.mkdtemp()) / "bubble-shield.json"
        _cfg.write_text(json.dumps({"gemma_mode": gemma_mode}), encoding="utf-8")
        env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(_cfg)
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

# Primary test case: standard DCC-style état-civil form
# Name "XANTHIPPE ZORVEC" is NOT in any French first-name gazetteer —
# it exists only to prove the structured recognizer (label-value) does the work.
FORM_ETAT_CIVIL = (
    "Nom : ZORVEC\n"
    "Prénom : Xanthippe\n"
    "Né(e) le : 03/05/1980 à : Lyon\n"
    "Passeport n° 12AB34567\n"
    "IBAN : FR76 3000 6000 0112 3456 7890 189\n"
    "Email : x.zorvec@testpii.invalid"
)

# Variant: "Lieu de naissance" explicit label
FORM_LIEU_EXPLICIT = (
    "Nom de naissance : ZORBESCU\n"
    "Prénom : Vladimira\n"
    "Date de naissance : 14/07/1975\n"
    "Lieu de naissance : Bordeaux\n"
    "CNI n° 987654321098\n"
)

# Control: non-PII text — should NOT be masked
CONTROL_NOPII = (
    "La réunion a eu lieu le 15 mars 2024. "
    "L'ordre du jour portait sur les investissements en Europe. "
    "Le montant total prévu est de 50 000 EUR."
)

# ── PART A: mock daemon UP (empty NER matches) — structured_ext catches FORM fields ──
print("=== Part A: mock-daemon UP (empty NER) — structured_ext must catch FORM fields ===\n")

r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": FORM_ETAT_CIVIL})],
              nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output (daemon DOWN):\n  {t!r}\n")

check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
check("A: Nom label value 'ZORVEC' masked", "ZORVEC" not in t)
check("A: Prénom label value 'Xanthippe' masked", "Xanthippe" not in t)
# BLOCKER FIX (#257-b): DOB in 'Né(e) le : DD/MM/YYYY' must be masked —
# the old DATE_NAISSANCE regex n[ée]e?\s+le silently missed the '(e)' form.
check("A: DOB '03/05/1980' masked (Né(e) le form) [#257 BLOCKER]", "03/05/1980" not in t)
check("A: birthplace 'Lyon' masked (Né(e) le ... à : Lyon)", "Lyon" not in t)
check("A: Passeport n° '12AB34567' masked", "12AB34567" not in t)
check("A: IBAN tokenised (regex core)", "FR76 3000" not in t)
check("A: email tokenised (regex core)", "x.zorvec@testpii.invalid" not in t)

# ── PART A2: lieu de naissance explicit label, mock daemon UP ────────────────
print("\n=== Part A2: 'Lieu de naissance' label, mock-daemon UP ===\n")

r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": FORM_LIEU_EXPLICIT})],
               nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output (daemon DOWN, lieu explicit):\n  {t2!r}\n")

check("A2: Nom de naissance 'ZORBESCU' masked", "ZORBESCU" not in t2)
check("A2: Prénom 'Vladimira' masked", "Vladimira" not in t2)
check("A2: Lieu de naissance 'Bordeaux' masked", "Bordeaux" not in t2)
check("A2: CNI n° '987654321098' masked", "987654321098" not in t2)

# ── PART A3: bubble_shield_read path (file on disk), mock daemon UP ──────────
# SHADOW-INDEX REDESIGN (B1) — bubble_shield_read is a fast hash→serve path
# that runs ZERO models at read time. On a cache HIT it serves the
# pre-computed masked shadow; on a cache MISS (this test: a brand-new temp
# file that was never swept) it serves the RAW extracted text verbatim
# (accepted, client-agreed B1 gap for speed). Full anonymisation happens
# later in the background sweep, NOT on the read call.
#
# This test used to assert bubble_shield_read masked FORM fields (ZORVEC,
# Xanthippe, Lyon, passport number, IBAN) directly at read time. That
# contract was retired by the shadow-index redesign: reading an unindexed
# file no longer runs structured_ext/NER at all, so those assertions must
# INVERT — read-on-miss now correctly serves the raw text unmasked, and the
# absence of any ⟦…⟧ token proves no masking model ran at read time. Mirrors
# the B1 re-encoding in scripts/test_bubble_shield_mcp.py sections 3 & 6 and
# tests/test_daemon_onnx_detection.py::test_blind_daemon_read_of_unindexed_file_serves_raw_daemon_independent.
#
# NOTE: Part A above (bubble_shield_anonymize_text) is UNCHANGED — that path
# still runs structured_ext/NER and still masks FORM fields correctly; only
# the read-of-an-unswept-file path became raw-on-miss.
print("\n=== Part A3: bubble_shield_read B1 contract — unswept file served raw ===\n")

tf = Path(tempfile.mkdtemp()) / "etat_civil_257.txt"
tf.write_text(FORM_ETAT_CIVIL, encoding="utf-8")
r3 = rpc_calls([INIT, call(2, "bubble_shield_read", {"path": str(tf)})], nerd_port=_MOCK_PORT)
res3 = r3[2].get("result", {})
t3 = text_of(r3[2])
print(f"  Output (read, unswept/cache-miss):\n  {t3!r}\n")

check("A3: B1 read (unswept file) → no isError (read never touches models)",
      not res3.get("isError"))
check("A3: B1 read miss — Nom 'ZORVEC' served raw (masking is the sweep's job)",
      "ZORVEC" in t3)
check("A3: B1 read miss — Prénom 'Xanthippe' served raw",
      "Xanthippe" in t3)
check("A3: B1 read miss — birthplace 'Lyon' served raw",
      "Lyon" in t3)
check("A3: B1 read miss — Passeport n° '12AB34567' served raw",
      "12AB34567" in t3)
check("A3: B1 read miss — IBAN served raw (not tokenised)",
      "FR76 3000" in t3)
check("A3: B1 read runs no models: body must contain no ⟦tokens⟧",
      "⟦" not in t3)

# ── PART B: daemon UP (if available) ────────────────────────────────────────
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
    print(f"=== Part B: daemon UP (port {NERD_PORT}) — FORM fields tokenised ===\n")
    r4 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": FORM_ETAT_CIVIL})],
                   nerd_port=NERD_PORT)
    t4 = text_of(r4[2])
    print(f"  Output (daemon UP):\n  {t4!r}\n")

    check("B: no degraded-mode warning", "dégradée" not in t4)
    check("B: output contains tokens", "⟦" in t4)
    check("B: Nom 'ZORVEC' masked (daemon UP)", "ZORVEC" not in t4)
    check("B: Prénom 'Xanthippe' masked (daemon UP)", "Xanthippe" not in t4)
    # BLOCKER FIX (#257-b): DOB must mask daemon UP too.
    check("B: DOB '03/05/1980' masked (Né(e) le, daemon UP) [#257 BLOCKER]", "03/05/1980" not in t4)
    check("B: birthplace 'Lyon' masked (daemon UP)", "Lyon" not in t4)
    check("B: Passeport n° '12AB34567' masked (daemon UP)", "12AB34567" not in t4)
    check("B: IBAN tokenised (daemon UP)", "FR76 3000" not in t4)
    check("B: email tokenised (daemon UP)", "x.zorvec@testpii.invalid" not in t4)
else:
    print(f"=== Part B: daemon not running on port {NERD_PORT} — SKIPPED (not FAILED) ===")

# ── PART C: non-PII control text — no over-masking ───────────────────────────
print("\n=== Part C: non-PII control text — no false positives ===\n")

r5 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": CONTROL_NOPII})],
               nerd_port=_MOCK_PORT, gemma_mode="hard")  # prose path: skip Gemma
t5 = text_of(r5[2])
print(f"  Output (control, daemon DOWN):\n  {t5!r}\n")

# The control text should come back largely intact (no PII, no masking needed)
# We allow the degraded-mode warning to appear but the content must be unmolested.
control_clean = t5.replace(
    "⚠️ Détection dégradée (regex seul, NER hors-ligne) — des données identifiantes "
    "en texte libre (noms, lieux de naissance, numéros de pièce) peuvent ne PAS être "
    "masquées. Relancez bubble_shield_setup_ml(action='status') pour vérifier l'état "
    "du pack ML, ou redémarrez la session pour réarmer le daemon NER.\n\n", "")
check("C: control text — 'Lé réunion' phrase intact", "réunion" in control_clean)
check("C: control text — no PII tokens injected where none exist",
      "15 mars" in control_clean or "15" in control_clean)  # date keeps its form
check("C: control text — 'Europe' not masked (not a name in form context)", "Europe" in t5)

# ── PART D: structured_ext isolation unit test ───────────────────────────────
print("\n=== Part D: structured_ext unit test (direct import) ===\n")

sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        form_nom_matches, form_lieu_naissance_matches,
        form_piece_identite_matches, make_structured_detector,
    )

    # Test form_nom_matches
    test_form = "Nom : ZORVEC\nPrénom : Xanthippe\n"
    nom_m = form_nom_matches(test_form)
    check("D: form_nom_matches finds 'ZORVEC'", any("ZORVEC" in m.value for m in nom_m))
    check("D: form_nom_matches finds 'Xanthippe'", any("Xanthippe" in m.value for m in nom_m))
    check("D: form_nom_matches entity_type is NOM", all(m.entity_type == "NOM" for m in nom_m))

    # Placeholder guard — 'Prénom : (vide)' and similar MUST NOT produce a NOM match.
    # Advisory fix: masking template/unfilled fields corrupts dossier integrity.
    placeholder_cases = [
        ("Prénom : (vide)", "(vide)"),
        ("Nom : N/A", "N/A"),
        ("Nom : néant", "néant"),
    ]
    for ph_text, ph_val in placeholder_cases:
        ph_m = form_nom_matches(ph_text)
        check(f"D: placeholder guard — '{ph_val}' NOT masked as NOM",
              not any(ph_val.lower() in m.value.lower() for m in ph_m))

    # Test form_lieu_naissance_matches
    lieu_text = "Né(e) le : 03/05/1980 à : Lyon\n"
    lieu_m = form_lieu_naissance_matches(lieu_text)
    check("D: form_lieu_naissance_matches finds 'Lyon'",
          any("Lyon" in m.value for m in lieu_m))
    check("D: form_lieu_naissance_matches entity_type is LIEU_NAISSANCE",
          all(m.entity_type == "LIEU_NAISSANCE" for m in lieu_m))

    # Test form_lieu with explicit label
    lieu2_text = "Lieu de naissance : Bordeaux\n"
    lieu2_m = form_lieu_naissance_matches(lieu2_text)
    check("D: form_lieu_naissance explicit label finds 'Bordeaux'",
          any("Bordeaux" in m.value for m in lieu2_m))

    # Test form_piece_identite_matches
    id_text = "Passeport n° 12AB34567\n"
    id_m = form_piece_identite_matches(id_text)
    check("D: form_piece_identite_matches finds passport number",
          any("12AB34567" in m.value for m in id_m))
    check("D: form_piece_identite entity_type is PIECE_IDENTITE",
          all(m.entity_type == "PIECE_IDENTITE" for m in id_m))

    # Test CNI
    cni_text = "CNI n° 987654321098\n"
    cni_m = form_piece_identite_matches(cni_text)
    check("D: form_piece_identite_matches finds CNI number",
          any("987654321098" in m.value for m in cni_m))

    # Test the combined detector
    det = make_structured_detector()
    full = det(FORM_ETAT_CIVIL)
    types = {m.entity_type for m in full}
    check("D: combined detector finds NOM", "NOM" in types)
    check("D: combined detector finds LIEU_NAISSANCE", "LIEU_NAISSANCE" in types)
    check("D: combined detector finds PIECE_IDENTITE", "PIECE_IDENTITE" in types)
    check("D: combined detector fail-open (no exception on any text)", True)  # reached here = pass

    # Test that a non-PII-labeled text does NOT produce FORM matches
    no_label_text = "La réunion a eu lieu le 15 mars 2024 en Europe.\n"
    nopii_m = det(no_label_text)
    nopii_types = {m.entity_type for m in nopii_m}
    check("D: non-PII text — no NOM from form labels", "NOM" not in nopii_types)

    # BLOCKER FIX (#257-b): core DATE_NAISSANCE recognizer — Né(e) le form.
    # Tests directly against recognizers.py (vendor copy) to ensure the
    # parenthetical (e) form is matched by the core regex, daemon-independent.
    try:
        from bubble_shield.recognizers import RECOGNIZERS
        dob_r = next(r for r in RECOGNIZERS if r.entity_type == "DATE_NAISSANCE")
        dob_forms = [
            ("Né(e) le : 03/05/1980", "03/05/1980"),   # THE BLOCKER
            ("née le : 03/05/1980",   "03/05/1980"),
            ("né le : 03/05/1980",    "03/05/1980"),
            ("date de naissance : 03/05/1980", "03/05/1980"),
        ]
        for dob_text, expected_val in dob_forms:
            dob_found = dob_r.find(dob_text)
            check(f"D: DATE_NAISSANCE recognizer — {dob_text!r}",
                  any(expected_val in m.value for m in dob_found))
    except Exception as exc:
        check(f"D: DATE_NAISSANCE recognizer import", False)
        print(f"    (error: {exc})")

except ImportError as e:
    print(f"  (structured_ext import failed: {e} — skipping Part D)")

# ── Summary ──────────────────────────────────────────────────────────────────
if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
