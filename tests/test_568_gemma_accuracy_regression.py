"""tests/test_568_gemma_accuracy_regression.py — #568 Gemma accuracy REGRESSION pin.

WHY THIS EXISTS
---------------
gemma_classifier.py's own unit tests (test_568_gemma_classifier.py) only cover
the pure string-parsing logic (_parse_verdict) — no real model is loaded there
so CI never needs mlx_lm or the gemma-env. That's right for CI, but it means a
prompt tweak or a model swap could silently regress REAL accuracy without any
test catching it.

This file is the catch: it runs the known 30-token corpus (15 form-label false
positives + 15 real French surnames) through the REAL GemmaClassifier, IF the
gemma-env venv is actually installed on this machine
(~/.bubble_shield/gemma-env/bin/python). If it isn't, the test is SKIPPED —
never fails CI on a machine that hasn't run bubble_shield_setup_ml.py's
install_gemma_env() step. This mirrors the project's convention of gating
model-dependent tests on artifact presence (see model_present() in
bubble_shield_setup_ml.py) rather than mocking the model.

The corpus + thresholds come directly from the #568 brief / the validation
run documented in gemma_classifier.py's module docstring (96.7% accuracy,
zero FP-leakage, one safe miss on 'Petit').

Two invariants are pinned:
  1. Zero FP-leakage — every form-label word MUST classify MOT. A leaked NOM
     here would mean a label word survives un-masked as if it were a real
     name's un-mask candidate — not a masking failure, but the inverse
     mis-classification this feature exists to avoid.
  2. >= 28/30 overall correct (FP words correctly MOT + real surnames
     correctly NOM). Misses are tolerated (fail-toward-masking keeps a missed
     real name as NOM, i.e. masked — safe), but overall judge quality must not
     silently drop below the validated baseline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# 15 form-label / common-word false-positive triggers (must classify MOT).
FP = ["Déclarant", "Vous", "conseiller", "Numéro", "fiscal", "Monsieur", "Madame",
      "gérant", "client", "partenaire", "Emploi", "domicile", "Suite", "Compte", "Adresse"]
# 15 real French surnames (should classify NOM; a miss stays MOT-safe... no,
# a miss here means a real name gets un-masked as MOT, but the brief's
# threshold (>=28/30 overall) tolerates a small number of misses because the
# GLiNER/regex layers upstream already caught the entity as a name candidate —
# this judge only ever runs on tokens ALREADY flagged as gazetteer hits, so a
# false MOT here is bounded by the >=28/30 floor, not by a masking guarantee).
REAL = ["Dupont", "Martin", "Bernard", "Petit", "Lenoir", "Dubois", "Leroy", "Moreau",
        "Girard", "Roux", "Fontaine", "Robert", "Colette", "Roy", "Leblanc"]


def _model_present() -> bool:
    return (Path.home() / ".bubble_shield" / "gemma-env" / "bin" / "python").exists()


@pytest.mark.skipif(not _model_present(), reason="gemma-env not installed on this machine")
def test_gemma_accuracy_and_zero_fp_leak():
    from gemma_classifier import GemmaClassifier

    c = GemmaClassifier()
    c.warm_up()
    fp_v = {r["token"]: r["verdict"] for r in c.classify(FP)}
    real_v = {r["token"]: r["verdict"] for r in c.classify(REAL)}

    # Invariant 1 — zero FP-leakage: every label word must be MOT, never a
    # leaked NOM.
    leaked = [t for t, v in fp_v.items() if v == "NOM"]
    assert leaked == [], f"label words leaked as NOM (should be MOT): {leaked}"

    # Invariant 2 — overall accuracy floor: >= 28/30 correct across both sets.
    correct = (sum(1 for v in fp_v.values() if v == "MOT")
               + sum(1 for v in real_v.values() if v == "NOM"))
    assert correct >= 28, (
        f"accuracy regression: only {correct}/30 correct "
        f"(FP verdicts={fp_v}, REAL verdicts={real_v})"
    )
