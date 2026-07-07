"""#568 — local Gemma NOM/MOT judge for gazetteer de-pollution.

Validated this session: 96.7% accuracy, zero false-positive leakage, the one
miss ('Petit') fails SAFE (stays masked). Runs in the gemma venv (mlx_lm).

Fail-toward-masking is the safety core of this module: `_parse_verdict`
returns "NOM" (keep masked) for ANYTHING not clearly MOT, and `classify`
returns verdict "NOM" on any inference exception for a token. An unclear
judge must keep masking — never fail open.
"""
from __future__ import annotations

MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"

_PROMPT = (
    "Réponds par un seul mot: NOM ou MOT.\n"
    "Le token suivant est-il un nom de famille français (NOM) "
    "ou un mot commun / une étiquette de formulaire (MOT) ?\n"
    "Token: '{tok}'\nRéponse:"
)


def _parse_verdict(raw: str) -> str:
    """Map model output to NOM|MOT. Fail-toward-masking: unclear → NOM (keep)."""
    t = (raw or "").strip().upper()
    # Only an UNAMBIGUOUS MOT signal (MOT present AND NOM absent) unmasks.
    # Anything else — both present, NOM only, empty, garbage — keeps masked.
    if "MOT" in t and "NOM" not in t:
        return "MOT"
    return "NOM"


class GemmaClassifier:
    def __init__(self, model_id: str = MODEL_ID):
        self.model_id = model_id
        self.warm = False
        self._model = None
        self._tok = None

    def warm_up(self) -> None:
        from mlx_lm import load
        self._model, self._tok = load(self.model_id)
        self.warm = True

    def classify(self, tokens):
        from mlx_lm import generate
        out = []
        for tok in tokens:
            try:
                resp = generate(self._model, self._tok,
                                prompt=_PROMPT.format(tok=tok), max_tokens=4, verbose=False)
                verdict = _parse_verdict(resp)
            except Exception:
                verdict = "NOM"  # fail-safe: keep masked
            out.append({"token": tok, "verdict": verdict})
        return out
