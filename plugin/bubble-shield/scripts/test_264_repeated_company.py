#!/usr/bin/env python3
"""Regression test for issue #264 — repeated company name leaks in liasse fiscale.

Proves that the corporate name (which embeds a practitioner's personal name) is
masked in EVERY occurrence within the same document — not only the labeled
"Dénomination / Raison sociale :" line that the #259 fix already caught.

In the liasse fiscale the company name appears:
  (a) behind "Dénomination de l'entreprise :" (label — now also caught; was a
      variant the #259 regex missed because it didn't include "de l'entreprise")
  (b) as doubled-prefix "SELARL SELARL DU DOCTEUR …" — PDF extraction artifact
  (c) as free-standing page/table headers with no label whatsoever

Two new mechanisms fix this:
  1. Forme-juridique-anchored recognizer: "(SELARL|SARL|SAS|…) <CAPS NAME>"
     catches (b) and (c) without requiring a label.
  2. Doc-level repetition post-pass: after ANY occurrence is found (labeled or
     forme-juridique-anchored), scans the whole document for verbatim repetitions
     and emits a RAISON_SOCIALE match for each unlabeled occurrence.

All PII used here is SYNTHETIC (FAKENAME TESTONI, 123 456 789 00011).

Run:
  python3 scripts/test_264_repeated_company.py
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
    env["HOME"] = str(Path(tempfile.mkdtemp()) / "fakehome")
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


# ── Synthetic test data — SYNTHETIC ONLY, no real client data ─────────────────
#
# Simulates a liasse fiscale where the company name "SELARL DU DOCTEUR
# FAKENAME TESTONI" appears in 5 places:
#   (a) behind "Dénomination de l'entreprise :" — the #264 label variant
#       (was missed by #259 regex which didn't include "de l'entreprise")
#   (b) behind "Raison sociale :" — the #259 labeled form (should still work)
#   (c) standalone header on line 3 (no label)
#   (d) doubled-prefix "SELARL SELARL …" in a simulated section header
#   (e) date-prefixed "01012025 31122025 SELARL SELARL …" (extracted PDF line)
#
# Controls that MUST NOT be masked:
#   - "Forme juridique : SELARL"  (type-word label — not a company name)
#   - "La SELARL exerce une activité"  (prose — not a company name header)

LIASSE_BLOCK = (
    "LIASSE FISCALE 2024\n"
    "\n"
    "SELARL DU DOCTEUR FAKENAME TESTONI\n"             # (c) standalone header
    "01/01/2024 - 31/12/2024\n"
    "\n"
    "Dénomination de l'entreprise : SELARL SELARL DU DOCTEUR FAKENAME TESTONI\n"  # (a)
    "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"  # (b)
    "Forme juridique : SELARL\n"                       # control — must NOT mask
    "N° SIREN : 123 456 789\n"
    "\n"
    "BILAN ACTIF\n"
    "\n"
    "EXPERTISE SELARL SELARL DU DOCTEUR FAKENAME TESTONI\n"  # (d) doubled prefix
    "01012025 31122025 SELARL SELARL DU DOCTEUR FAKENAME TESTONI\n"  # (e)
    "\n"
    "La SELARL exerce une activité de médecine.\n"     # control — must NOT mask
    "\n"
    "Forme juridique : SELARL\n"                       # control
    "Type : SAS\n"                                     # control
)

# Simpler block: ONLY unlabeled headers (no label at all) — tests the
# forme-juridique-anchored path as the sole detection source
UNLABELED_ONLY_BLOCK = (
    "SELARL DU DOCTEUR FAKENAME TESTONI\n"
    "SELARL SELARL DU DOCTEUR FAKENAME TESTONI\n"
)

# ── PART A: daemon DOWN ────────────────────────────────────────────────────────
print("=== Part A: daemon DOWN — all occurrences must be masked ===\n")

r = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": LIASSE_BLOCK})],
              nerd_port=_MOCK_PORT)
t = text_of(r[2])
print(f"  Output (daemon DOWN, first 600 chars):\n  {t[:600]!r}\n")

check("A: response is non-empty", bool(t))
check("A: output contains tokens (⟦)", "⟦" in t)
# All 5 occurrences of FAKENAME / TESTONI must be gone
check("A: FAKENAME not in output (all occurrences masked)", "FAKENAME" not in t)
check("A: TESTONI not in output (all occurrences masked)", "TESTONI" not in t)
# Precision: forme juridique label line must survive
check("A: 'Forme juridique' label still in output (not over-masked)",
      "Forme juridique" in t)
# Precision: prose SELARL survives (but may be followed by masked tokens)
check("A: prose line context 'La' still in output (not over-masked)",
      "La" in t and "activit" in t)

# Part A2: unlabeled-only block (no "Raison sociale :" label at all)
print()
print("=== Part A2: unlabeled-only block (no label) — daemon DOWN ===\n")

r2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": UNLABELED_ONLY_BLOCK})],
               nerd_port=_MOCK_PORT)
t2 = text_of(r2[2])
print(f"  Output: {t2!r}\n")

check("A2: FAKENAME not in output (unlabeled header masked)", "FAKENAME" not in t2)
check("A2: TESTONI not in output (unlabeled header masked)", "TESTONI" not in t2)
check("A2: output contains tokens", "⟦" in t2)

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
    print(f"=== Part B: daemon UP (port {NERD_PORT}) — all occurrences masked ===\n")
    rb = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text", {"text": LIASSE_BLOCK})],
                   nerd_port=NERD_PORT)
    tb = text_of(rb[2])
    print(f"  Output (daemon UP, first 600 chars):\n  {tb[:600]!r}\n")

    check("B: no degraded-mode warning (daemon UP)", "dégradée" not in tb)
    check("B: output contains tokens (daemon UP)", "⟦" in tb)
    check("B: FAKENAME not in output (daemon UP)", "FAKENAME" not in tb)
    check("B: TESTONI not in output (daemon UP)", "TESTONI" not in tb)
    check("B: 'Forme juridique' label still in output (daemon UP, not over-masked)",
          "Forme juridique" in tb)

    # Unlabeled-only block, daemon UP
    rb2 = rpc_calls([INIT, call(2, "bubble_shield_anonymize_text",
                                {"text": UNLABELED_ONLY_BLOCK})],
                    nerd_port=NERD_PORT)
    tb2 = text_of(rb2[2])
    check("B2: FAKENAME not in output — unlabeled header masked (daemon UP)",
          "FAKENAME" not in tb2)
else:
    print(f"=== Part B: daemon not running on port {NERD_PORT} — SKIPPED ===")

# ── PART D: direct unit tests (vendor import) ────────────────────────────────
print("\n=== Part D: unit tests (direct import of vendor modules) ===\n")

sys.path.insert(0, str(HERE.parent / "vendor"))
try:
    from bubble_shield.structured_ext import (
        form_raison_sociale_matches,
        forme_juridique_anchored_matches,
        doc_level_repetition_matches,
        _canonical_company_name,
        make_structured_detector,
    )

    # D1: labeled "Dénomination de l'entreprise :" — was missed by #259 regex
    text_d1 = "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI"
    m_d1 = form_raison_sociale_matches(text_d1)
    check("D1: 'Dénomination de l'entreprise :' label catches company name",
          any("FAKENAME" in m.value for m in m_d1))
    check("D1: entity_type is RAISON_SOCIALE",
          all(m.entity_type == "RAISON_SOCIALE" for m in m_d1))

    # D2: standalone unlabeled header (forme-juridique-anchored)
    text_d2 = "SELARL DU DOCTEUR FAKENAME TESTONI"
    m_d2 = forme_juridique_anchored_matches(text_d2)
    check("D2: unlabeled 'SELARL DU DOCTEUR FAKENAME TESTONI' caught",
          any("FAKENAME" in m.value for m in m_d2))

    # D3: doubled-prefix artifact
    text_d3 = "SELARL SELARL DU DOCTEUR FAKENAME TESTONI"
    m_d3 = forme_juridique_anchored_matches(text_d3)
    check("D3: doubled-prefix 'SELARL SELARL DU DOCTEUR FAKENAME TESTONI' caught",
          any("FAKENAME" in m.value for m in m_d3))

    # D4: precision — "Forme juridique : SELARL" NOT matched
    text_d4 = "Forme juridique : SELARL"
    m_d4 = forme_juridique_anchored_matches(text_d4)
    check("D4: 'Forme juridique : SELARL' NOT matched by forme-juridique recognizer",
          not m_d4)

    # D5: precision — prose "La SELARL exerce…" NOT matched
    text_d5 = "La SELARL exerce une activité de médecine."
    m_d5 = forme_juridique_anchored_matches(text_d5)
    check("D5: prose 'La SELARL exerce…' NOT matched", not m_d5)

    # D6: precision — "Type : SAS" NOT matched
    text_d6 = "Type : SAS"
    m_d6 = forme_juridique_anchored_matches(text_d6)
    check("D6: 'Type : SAS' NOT matched", not m_d6)

    # D7: canonical normalisation — double prefix stripped
    c7 = _canonical_company_name("SELARL SELARL DU DOCTEUR FAKENAME TESTONI")
    check("D7: canonical form strips doubled prefix",
          c7 == "SELARL DU DOCTEUR FAKENAME TESTONI")

    # D8: doc_level_repetition_matches — post-pass finds unlabeled occurrences
    from bubble_shield.recognizers import Match
    # Simulate: one labeled match found, two unlabeled repeats
    seed_match = Match(start=50, end=84, entity_type="RAISON_SOCIALE",
                       value="SELARL DU DOCTEUR FAKENAME TESTONI",
                       score=0.90, priority=59)
    test_doc = (
        "Preamble text before.\n"
        "Some other text here.\n\n"
        "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Section header: SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Another line: SELARL DU DOCTEUR FAKENAME TESTONI\n"
    )
    # Find the real start position of the first occurrence
    import re as _re
    first_occ = _re.search("SELARL DU DOCTEUR FAKENAME TESTONI", test_doc)
    if first_occ:
        seed_match = Match(start=first_occ.start(), end=first_occ.end(),
                           entity_type="RAISON_SOCIALE",
                           value="SELARL DU DOCTEUR FAKENAME TESTONI",
                           score=0.90, priority=59)
    extra = doc_level_repetition_matches(test_doc, [seed_match])
    all_occ = list(_re.finditer("SELARL DU DOCTEUR FAKENAME TESTONI", test_doc))
    check("D8: doc_level_repetition_matches finds unlabeled repeats",
          len(extra) >= len(all_occ) - 1)  # at least (total - 1) extra found

    # D9: combined detector — ALL 5 FAKENAME occurrences masked in full block
    det = make_structured_detector()
    all_m = det(LIASSE_BLOCK)
    rs_m = [m for m in all_m if m.entity_type == "RAISON_SOCIALE"]
    fakename_positions = [
        m.start() for m in _re.finditer("FAKENAME", LIASSE_BLOCK)
    ]
    covered = {
        pos
        for pos in fakename_positions
        for rm in rs_m
        if rm.start <= pos < rm.end
    }
    check(
        f"D9: all {len(fakename_positions)} FAKENAME occurrences covered "
        f"({len(covered)} covered)",
        len(covered) == len(fakename_positions),
    )

    # D10: precision — combined detector does NOT mask "Forme juridique : SELARL"
    m_d10 = det("Forme juridique : SELARL\n")
    rs_d10 = [m for m in m_d10 if m.entity_type == "RAISON_SOCIALE"]
    check("D10: 'Forme juridique : SELARL' NOT RAISON_SOCIALE in combined detector",
          not rs_d10)

    # D11: precision — prose NOT over-masked
    m_d11 = det("La SELARL exerce une activité de médecine.\n")
    rs_d11 = [m for m in m_d11 if m.entity_type == "RAISON_SOCIALE"]
    check("D11: prose 'La SELARL exerce…' NOT RAISON_SOCIALE in combined detector",
          not rs_d11)

    # D12: fail-open — no exception on any text
    try:
        det("")
        det("foo bar baz")
        det("SELARL")
        det("SAS\nSARL\nSCI\n")
        check("D12: combined detector fail-open (no exception)", True)
    except Exception as exc:
        check("D12: combined detector fail-open", False)
        print(f"    (error: {exc})")

    # ── Bug A regression tests (ship-blocker: degenerate seed corrupts doc) ───
    print()
    print("=== Bug A regression: bare forme-juridique seed must NOT spread ===")
    from bubble_shield.structured_ext import _FORME_JURIDIQUE_SET

    # D13: bare SARL seed — doc_level_repetition_matches must emit NO matches
    bare_sarl_seed = Match(start=17, end=21, entity_type="RAISON_SOCIALE",
                           value="SARL", score=0.90, priority=59)
    bug_a_doc = (
        "Raison sociale : SARL\n"
        "Forme juridique : SARL\n"
        "La SARL exerce une activité.\n"
        "SARL DUPONT\n"
    )
    extra_a = doc_level_repetition_matches(bug_a_doc, [bare_sarl_seed])
    check("D13: bare SARL seed emits 0 repetition matches (no over-mask)",
          len(extra_a) == 0)

    # D14: no bare-SARL RAISON_SOCIALE match from combined detector on bare-type text
    det_a = make_structured_detector()
    m_d14 = det_a(bug_a_doc)
    rs_d14 = [m for m in m_d14 if m.entity_type == "RAISON_SOCIALE"]
    # "Forme juridique : SARL" must not be masked as RAISON_SOCIALE
    forme_juri_line = "Forme juridique : SARL"
    forme_juri_covered = any(
        m.start <= bug_a_doc.index(forme_juri_line) + len("Forme juridique : ") < m.end
        for m in rs_d14
    )
    check("D14: 'Forme juridique : SARL' NOT masked as RAISON_SOCIALE (bare-type guard)",
          not forme_juri_covered)

    # D15: prose "La SARL exerce…" not masked as RAISON_SOCIALE
    prose_line = "La SARL exerce"
    prose_covered = any(
        m.start <= bug_a_doc.index(prose_line) + len("La ") < m.end
        for m in rs_d14
    )
    check("D15: prose 'La SARL exerce…' NOT masked as RAISON_SOCIALE (bare-type guard)",
          not prose_covered)

    # D16: seed whose canonical is a multi-word name is NOT blocked by the guard
    real_seed = Match(start=0, end=34, entity_type="RAISON_SOCIALE",
                      value="SELARL DU DOCTEUR FAKENAME TESTONI",
                      score=0.90, priority=59)
    multi_doc = (
        "SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Table: SELARL DU DOCTEUR FAKENAME TESTONI\n"
    )
    extra_real = doc_level_repetition_matches(multi_doc, [real_seed])
    check("D16: real multi-word canonical seed still produces repetition matches",
          len(extra_real) >= 1)

    # ── Bug B regression tests (ship-blocker: two vault tokens for one company) ─
    print()
    print("=== Bug B regression: doubled-prefix and clean form → ONE canonical ===")

    # D17: form_raison_sociale_matches emits same canonical value for both
    #      "Dénomination de l'entreprise : SELARL SELARL DU DOCTEUR FAKENAME TESTONI"
    #      "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI"
    bug_b_text = (
        "Dénomination de l'entreprise : SELARL SELARL DU DOCTEUR FAKENAME TESTONI\n"
        "Raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI\n"
    )
    m_b = form_raison_sociale_matches(bug_b_text)
    vals_b = {m.value for m in m_b if m.entity_type == "RAISON_SOCIALE"}
    check("D17: doubled-prefix and clean form both emit the same canonical value",
          len(vals_b) == 1)
    if vals_b:
        canon_b = next(iter(vals_b))
        check("D17b: canonical value is the clean (non-doubled) form",
              canon_b == "SELARL DU DOCTEUR FAKENAME TESTONI")

    # D18: vault produces a SINGLE token for both occurrences
    from bubble_shield.vault import Vault
    v = Vault(mission="test-bug-b")
    tok1 = v.token_for("SELARL SELARL DU DOCTEUR FAKENAME TESTONI", "RAISON_SOCIALE")
    tok2 = v.token_for("SELARL DU DOCTEUR FAKENAME TESTONI", "RAISON_SOCIALE")
    check("D18: vault emits different tokens for different raw strings (vault is exact-key)",
          tok1 != tok2)
    # D18 shows vault is exact-key — the fix must happen BEFORE vault sees the value.
    # D19 confirms that both structured_ext emissions now produce the SAME value.
    vals_b17 = list({m.value for m in m_b})
    check("D19: structured_ext emissions all have the same value → vault will get ONE key",
          len(vals_b17) == 1)

    # D20: combined detector on the full bug-B scenario — verify same token in output
    #      when using a fresh vault (simulate engine: replace each span with its token)
    from bubble_shield.vault import Vault as _Vault
    v20 = _Vault(mission="test-bug-b-e2e")
    all_m20 = det(bug_b_text)
    rs_m20 = sorted(
        [m for m in all_m20 if m.entity_type == "RAISON_SOCIALE"],
        key=lambda x: x.start, reverse=True,
    )
    out20 = bug_b_text
    for mm in rs_m20:
        tok = v20.token_for(mm.value, mm.entity_type)
        out20 = out20[:mm.start] + tok + out20[mm.end:]
    # Both original lines should now contain the same token
    token_ids_in_out = _re.findall(r"⟦RAISON_SOCIALE_(\d+)⟧", out20)
    check(
        f"D20: both lines get the same ⟦RAISON_SOCIALE_N⟧ token "
        f"(token IDs found: {token_ids_in_out})",
        len(set(token_ids_in_out)) == 1,
    )

except ImportError as e:
    print(f"  (vendor import failed: {e} — skipping Part D)")

# ── Summary ──────────────────────────────────────────────────────────────────
if _MOCK_SRV:
    _MOCK_SRV.shutdown()
print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
