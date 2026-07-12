"""
test_task2_judge_prompt.py — C2 value-focused judging prompt for
classify_via_extract (bare-surname hardening of C1).

Task 2's B2 prompt asked "is this a NAME?" but the de-pollution allowlist
passes NOM+POSTE+ADRESSE, so B2 un-masked real addresses (an address is not a
name → un-mask, a leak). C1 reframed the question to "is this a real
identifying VALUE (person name / real address / company raison sociale) →
PII/keep, or a generic label / job title / boilerplate → GENERIQUE/un-mask?".

C2 (2026-07-11) hardens C1 against BARE single-token surnames: C1's exemplars
were all FULL names, so on a bare surname that is also a common word ("Petit",
"Smith") the small model leaned GENERIQUE and un-masked a real name (a leak).
C2 adds an explicit bare-token rule ("ATTENTION aux mots seuls … NOM DE
FAMILLE … réponds PII") plus 4 bare-token few-shot exemplars (Petit→PII,
Duchemin→PII, FISCAL→GENERIQUE, Cadre supérieur→GENERIQUE).

classify_via_extract's fast (max_tokens=8) single-shot judge and parse logic
are UNCHANGED — only the prompt string changes:

  verdict = "MOT"  if ("GENERIQUE" in resp.upper() and "PII" not in resp.upper())
            else   "NOM"
  on ANY exception → "NOM"  (fail-toward-masking)

_JUDGE_PROMPT is a module constant containing the verbatim C2 few-shot text
(French accents + guillemets «» + all 12 exemplars preserved exactly).
extract_pii / _EXTRACT_PROMPT are UNCHANGED (still used for the #589-B
second pass) — only classify_via_extract's prompt changes.

Uses a FAKE generate — the real MLX model is never loaded here.
All PII in this file is SYNTHETIC. No real client values anywhere.
"""
from __future__ import annotations

import importlib.util
import pathlib

_GC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "plugin/bubble-shield/scripts/gemma_classifier.py"
)
_spec = importlib.util.spec_from_file_location("gc_task2", _GC)
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)


def _make_clf(fake_generate):
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


# The verbatim C2 prompt, copied from the plan Task 1 "Verbatim C2 prompt:"
# block (French accents, guillemets «», all 12 few-shot exemplars incl. the 4
# new bare-token ones). This is the authoritative reference the module constant
# must equal byte-for-byte.
_C2_VERBATIM = (
    "Tu filtres les fausses alertes d'un outil d'anonymisation.\n"
    "On te donne une courte chaîne extraite d'un document. Réponds PII si c'est une VRAIE donnée identifiante — le nom/prénom d'une personne réelle, une adresse postale réelle, ou la raison sociale d'une entreprise (SARL, SAS, SELARL, SA, SCI...). Réponds GENERIQUE si c'est un mot commun, un intitulé de poste (consultant, cadre supérieur...), une étiquette de formulaire (déclarant 1, nom de naissance...), ou une phrase administrative générique.\n"
    "ATTENTION aux mots seuls: si un mot isolé pourrait être le NOM DE FAMILLE d'une personne (même si c'est aussi un mot courant, ex: Petit, Smith), réponds PII. Ne réponds GENERIQUE pour un mot seul que si c'est clairement un terme administratif/fiscal ou un nom commun qui n'est pas un patronyme.\n"
    "En cas de doute, réponds PII.\n"
    "Réponds par UN SEUL mot: PII ou GENERIQUE.\n"
    "\n"
    "Chaîne: «directeur général»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Jean-Marc DUPONTEL»\n"
    "Réponse: PII\n"
    "Chaîne: «12 rue des Acacias, 69003 Lyon»\n"
    "Réponse: PII\n"
    "Chaîne: «adresse du souscripteur»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Madame Sophie LEGRAND»\n"
    "Réponse: PII\n"
    "Chaîne: «SARL Lumière Patrimoine»\n"
    "Réponse: PII\n"
    "Chaîne: «cadre de la mission»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «8 boulevard Haussmann 75009 Paris»\n"
    "Réponse: PII\n"
    "Chaîne: «Petit»\n"
    "Réponse: PII\n"
    "Chaîne: «Duchemin»\n"
    "Réponse: PII\n"
    "Chaîne: «FISCAL»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Cadre supérieur»\n"
    "Réponse: GENERIQUE\n"
    "\n"
    "Chaîne: «{tok}»\n"
    "Réponse:"
)


# --- Test 4: the prompt constant IS the verbatim C2 text -------------------
def test_judge_prompt_constant_is_verbatim_c2():
    p = gc._JUDGE_PROMPT
    # Byte-for-byte equality with the plan's C2 block (accents, «», 12 exemplars).
    assert p == _C2_VERBATIM
    # Spot-check the C1-inherited reframing (VALUE, not just NAME): address
    # exemplars present (the leak B2 caused), job-title/label GENERIQUE examples.
    assert "Réponds par UN SEUL mot: PII ou GENERIQUE." in p
    assert "Chaîne: «12 rue des Acacias, 69003 Lyon»\nRéponse: PII" in p
    assert "Chaîne: «8 boulevard Haussmann 75009 Paris»\nRéponse: PII" in p
    assert "Chaîne: «adresse du souscripteur»\nRéponse: GENERIQUE" in p
    assert "Chaîne: «cadre de la mission»\nRéponse: GENERIQUE" in p
    assert "Chaîne: «directeur général»\nRéponse: GENERIQUE" in p
    assert "Chaîne: «Jean-Marc DUPONTEL»\nRéponse: PII" in p
    assert "Chaîne: «Madame Sophie LEGRAND»\nRéponse: PII" in p
    assert "Chaîne: «SARL Lumière Patrimoine»\nRéponse: PII" in p
    # The {tok} slot for the token under test.
    assert "Chaîne: «{tok}»\nRéponse:" in p
    # All 12 exemplars present (7 PII, 5 GENERIQUE).
    assert p.count("Réponse: PII") == 7
    assert p.count("Réponse: GENERIQUE") == 5


# --- Test (new, C2): the bare-surname RULE + bare-token exemplars are present
def test_judge_prompt_has_bare_surname_rule():
    """C2 hardening: the prompt must carry the explicit bare-token rule and the
    Petit/Duchemin bare-surname → PII few-shot exemplars that stop C1's
    single-token real-surname leaks. Prompt-content only — no model call."""
    p = gc._JUDGE_PROMPT
    # The explicit bare-token instruction (the core of the C2 fix).
    assert "NOM DE FAMILLE" in p
    assert "ATTENTION aux mots seuls" in p
    # The 4 new bare-token exemplars (2 PII surnames, 2 GENERIQUE admin terms).
    assert "Chaîne: «Petit»\nRéponse: PII" in p
    assert "Chaîne: «Duchemin»\nRéponse: PII" in p
    assert "Chaîne: «FISCAL»\nRéponse: GENERIQUE" in p
    assert "Chaîne: «Cadre supérieur»\nRéponse: GENERIQUE" in p


# --- Test 3: verdict parse -------------------------------------------------
def test_generique_yields_mot():
    clf = _make_clf(lambda *a, **k: "GENERIQUE")
    out = clf.classify_via_extract(["directeur général"])
    assert out == [{"token": "directeur général", "verdict": "MOT"}]


def test_pii_yields_nom():
    clf = _make_clf(lambda *a, **k: "PII")
    out = clf.classify_via_extract(["Jean Dupont"])
    assert out == [{"token": "Jean Dupont", "verdict": "NOM"}]


def test_empty_output_yields_nom():
    # B2: empty output has neither GENERIQUE → NOM (fail-toward-masking).
    clf = _make_clf(lambda *a, **k: "")
    out = clf.classify_via_extract(["x"])
    assert out == [{"token": "x", "verdict": "NOM"}]


def test_generique_with_surrounding_text_yields_mot():
    # "GENERIQUE" present, "PII" absent → MOT
    clf = _make_clf(lambda *a, **k: "Je pense que GENERIQUE")
    out = clf.classify_via_extract(["date de signature"])
    assert out == [{"token": "date de signature", "verdict": "MOT"}]


def test_both_generique_and_pii_present_yields_nom():
    # Ambiguous (both present) → NOM (keep masked)
    clf = _make_clf(lambda *a, **k: "PII ou GENERIQUE")
    out = clf.classify_via_extract(["ambigu"])
    assert out == [{"token": "ambigu", "verdict": "NOM"}]


def test_generique_case_insensitive():
    clf = _make_clf(lambda *a, **k: "generique")
    out = clf.classify_via_extract(["mot"])
    assert out == [{"token": "mot", "verdict": "MOT"}]


# --- Test 4: fail-safe (exception → NOM), batch unaffected ------------------
def test_generate_raises_yields_nom_batch_unaffected():
    def fake_generate(model, tok, prompt, **kw):
        if "boom" in prompt:
            raise RuntimeError("MLX stream error")
        return "GENERIQUE"

    clf = _make_clf(fake_generate)
    out = clf.classify_via_extract(["clean label", "boom entry", "another label"])
    assert out == [
        {"token": "clean label", "verdict": "MOT"},
        {"token": "boom entry", "verdict": "NOM"},   # error → keep masked
        {"token": "another label", "verdict": "MOT"},
    ]


# --- Test 5: max_tokens=8 and prompt formatting ----------------------------
def test_classify_via_extract_uses_max_tokens_8_and_formatted_prompt():
    calls = []

    def spy_generate(model, tok, prompt, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        return "GENERIQUE"

    clf = _make_clf(spy_generate)
    clf.classify_via_extract(["directeur général"])

    assert len(calls) == 1
    assert calls[0]["kw"].get("max_tokens") == 8
    # The token is interpolated into the B2 prompt's {tok} slot.
    assert "Chaîne: «directeur général»\nRéponse:" in calls[0]["prompt"]
    assert calls[0]["prompt"] == gc._JUDGE_PROMPT.format(tok="directeur général")
