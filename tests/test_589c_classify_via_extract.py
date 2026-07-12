"""
test_589c_classify_via_extract.py — name-focused de-pollution judge (Task 2).

Task 1 (#589-C) built classify_via_extract on the extract_pii path; Task 2
replaces its body with the fast B2 name-focused single-shot judge:

  - generate returns "GENERIQUE" (and not "PII")            → verdict "MOT" (un-mask)
  - generate returns "PII" (a name / raison sociale)        → verdict "NOM" (keep)
  - generate raises for an entry                            → verdict "NOM" (fail-safe)

SAFETY CONTRACT (P0, unchanged from Task 1): an error must NEVER produce
"MOT". Only a clean "GENERIQUE" verdict un-masks. These tests use a FAKE
generate — the real MLX model is never loaded here.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import importlib.util
import pathlib

_GC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "plugin/bubble-shield/scripts/gemma_classifier.py"
)
_spec = importlib.util.spec_from_file_location("gc_589c", _GC)
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)


def _make_clf(fake_generate):
    """Build a GemmaClassifier whose model call is a fake generate(...).

    The fake replaces mlx_lm.generate transparently: the judge calls
    generate(model, tok, prompt=..., max_tokens=8, verbose=...) and gets back
    whatever the fake returns (or the fake raises). No real MLX.
    """
    clf = gc.GemmaClassifier()
    clf.warm = True
    clf._model = object()
    clf._tok = object()

    import sys
    import types

    fake_mod = types.ModuleType("mlx_lm")
    fake_mod.generate = fake_generate
    fake_mod.load = lambda *a, **k: (object(), object())
    sys.modules["mlx_lm"] = fake_mod
    return clf


# --- Test 1: real name → NOM (stays masked) -------------------------------
def test_real_name_entry_yields_nom():
    def fake_generate(model, tok, prompt, **kw):
        return "PII"  # judge recognises a real name

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["Monsieur Théodore MARTINVILLE"])
    assert out == [{"token": "Monsieur Théodore MARTINVILLE", "verdict": "NOM"}]


# --- Test 2: multi-word boilerplate → MOT (THE BUG FIX) -------------------
def test_multiword_boilerplate_yields_mot_the_bug_fix():
    # The old single-token judge defaulted multi-word phrases to NOM (never
    # un-masked). The B2 judge returns GENERIQUE for pure boilerplate → MOT.
    def fake_generate(model, tok, prompt, **kw):
        return "GENERIQUE"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["cadre de notre activité de Conseil"])
    assert out == [
        {"token": "cadre de notre activité de Conseil", "verdict": "MOT"}
    ]


# --- Test 3: form labels → MOT --------------------------------------------
def test_form_labels_yield_mot():
    def fake_generate(model, tok, prompt, **kw):
        return "GENERIQUE"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["déclarant 1", "Nom de naissance"])
    assert out == [
        {"token": "déclarant 1", "verdict": "MOT"},
        {"token": "Nom de naissance", "verdict": "MOT"},
    ]


# --- Test 4: name-bearing entry → NOM (whole entry stays masked) ----------
def test_name_entry_yields_nom():
    # The B2 judge answers PII for an entry that is a real person name → the
    # whole entry stays masked. No partial un-mask.
    def fake_generate(model, tok, prompt, **kw):
        return "PII"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["M. MARTINVILLE"])
    assert out == [{"token": "M. MARTINVILLE", "verdict": "NOM"}]


# --- Test 5: generate() raises → NOM (fail-safe), batch unaffected --------
def test_generate_raises_yields_nom_and_other_entries_unaffected():
    # Fail-toward-masking: an inference error for ONE entry must produce NOM
    # (never MOT) and must NOT corrupt the verdicts of the other entries.
    def fake_generate(model, tok, prompt, **kw):
        if "boom" in prompt:
            raise RuntimeError("MLX stream error")
        return "GENERIQUE"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["clean label", "boom entry", "another label"])
    assert out == [
        {"token": "clean label", "verdict": "MOT"},
        {"token": "boom entry", "verdict": "NOM"},  # error → keep masked
        {"token": "another label", "verdict": "MOT"},
    ]


def test_empty_output_parses_to_nom():
    # A successful generate() returning empty/whitespace has neither GENERIQUE
    # nor PII → NOM (fail-toward-masking). Distinct from the old extract judge,
    # where an empty parse meant "no PII" → MOT. B2 requires an explicit
    # GENERIQUE to un-mask.
    def fake_generate(model, tok, prompt, **kw):
        return "   \n  "

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["  "])
    assert out == [{"token": "  ", "verdict": "NOM"}]


# --- Wedge fix: per-token abandon-check (defense-in-depth) -----------------
#
# classify_via_extract accepts an OPTIONAL should_abort callable. When the
# worker's job is abandoned (caller timed out), the callback flips True and the
# loop stops early, returning only the results computed so far. The remaining
# tokens are simply ABSENT (→ depollute treats them as stay-masked). Without a
# callback, behavior is unchanged (all tokens processed).
def test_should_abort_returns_partial_results():
    # should_abort flips True after 2 tokens → only the first 2 are processed.
    def fake_generate(model, tok, prompt, **kw):
        return "GENERIQUE"

    clf = _make_clf(fake_generate)

    processed = {"n": 0}

    def should_abort():
        # Checked at the TOP of each iteration. Abort once 2 tokens are done.
        return processed["n"] >= 2

    # Wrap generate to count processed tokens.
    orig = clf.classify_via_extract

    def counting_generate(model, tok, prompt, **kw):
        processed["n"] += 1
        return "GENERIQUE"

    clf = _make_clf(counting_generate)
    out = clf.classify_via_extract(
        ["a", "b", "c", "d", "e"], should_abort=should_abort
    )
    # Only the first 2 tokens processed; c/d/e absent (stay masked downstream).
    assert out == [
        {"token": "a", "verdict": "MOT"},
        {"token": "b", "verdict": "MOT"},
    ]
    assert {r["token"] for r in out} == {"a", "b"}
    for absent in ("c", "d", "e"):
        assert absent not in {r["token"] for r in out}


def test_no_should_abort_processes_all_tokens_unchanged():
    # Backward-compatible: no callback → every token processed, as before.
    def fake_generate(model, tok, prompt, **kw):
        return "GENERIQUE"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["a", "b", "c"])
    assert out == [
        {"token": "a", "verdict": "MOT"},
        {"token": "b", "verdict": "MOT"},
        {"token": "c", "verdict": "MOT"},
    ]


def test_should_abort_false_processes_all_tokens():
    # A callback that never returns True is equivalent to no callback.
    def fake_generate(model, tok, prompt, **kw):
        return "PII"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(
        ["a", "b", "c"], should_abort=lambda: False
    )
    assert out == [
        {"token": "a", "verdict": "NOM"},
        {"token": "b", "verdict": "NOM"},
        {"token": "c", "verdict": "NOM"},
    ]
