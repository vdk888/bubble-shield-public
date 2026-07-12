"""
tests/test_568_gemma_classifier.py — #568 GemmaClassifier NOM/MOT judge.

Pure string-parsing tests for _parse_verdict — NO real model is loaded or
downloaded here (mlx_lm / the gemma-env are not required for CI). The real
model is validated manually per the brief's Step 5 (deferred to Task 10
regression if gemma-env isn't provisioned on this machine).

Fail-toward-masking is the safety core under test: _parse_verdict must return
"NOM" (keep masked) for ANYTHING not clearly MOT.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import gemma_classifier as gc


def test_parse_verdict_maps_model_output():
    # the prompt asks for a single word NOM or MOT; parsing must be robust
    assert gc._parse_verdict("MOT") == "MOT"
    assert gc._parse_verdict(" nom.\n") == "NOM"
    assert gc._parse_verdict("le mot est: MOT") == "MOT"


def test_parse_verdict_unclear_defaults_to_nom_keep_masked():
    # fail-toward-masking: anything not clearly MOT stays a name
    assert gc._parse_verdict("je ne sais pas") == "NOM"
    assert gc._parse_verdict("") == "NOM"


def test_parse_verdict_mixed_signal_defaults_to_nom_keep_masked():
    # CRITICAL: fail-toward-masking must hold even when MOT is present but
    # the output is hedged/ambiguous (also mentions NOM). Un-masking here is
    # a PII leak. All of these must stay masked (NOM).
    assert gc._parse_verdict("MOT, pas un nom de famille") == "NOM"  # hedge
    assert gc._parse_verdict("MOT ou NOM ? incertain") == "NOM"  # ambiguous
    assert gc._parse_verdict("MOTNOM") == "NOM"  # collision
    assert gc._parse_verdict("c'est un NOM") == "NOM"
