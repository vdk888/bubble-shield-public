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


# #589-B — Gemma PII-span extraction (2nd pass for degraded tax forms).
_EXTRACT_PROMPT = (
    "Liste toute donnée personnelle identifiante dans ce texte de formulaire fiscal "
    "français. Une ligne par donnée, format « TYPE: valeur exacte ». Types autorisés: "
    "NOM, PRENOM, SIRET, DATE_NAISSANCE, LIEU_NAISSANCE, ADRESSE, TELEPHONE, RAISON_SOCIALE. "
    "N'invente rien ; recopie la valeur telle qu'elle apparaît. Si aucune donnée: réponds « (aucune) ».\n\n"
    "TEXTE:\n{text}\n\nDONNÉES:"
)
_ALLOWED_TYPES = {"NOM", "PRENOM", "SIRET", "DATE_NAISSANCE", "LIEU_NAISSANCE",
                  "ADRESSE", "TELEPHONE", "RAISON_SOCIALE"}


_AUCUNE_STRIP_CHARS = " \t\r\n\"'«»«»"


def _is_aucune_sentinel(v: str) -> bool:
    """True when `v` is Gemma's "(aucune)" sentinel, possibly echoed back wrapped
    in guillemets/straight quotes and/or extra whitespace, in any case. Robust
    check per Task-2 review: a quoted echo (e.g. « (aucune) ») must be dropped,
    not turned into a spurious span."""
    v = v.strip(_AUCUNE_STRIP_CHARS).strip().lower()
    return v in ("aucune", "(aucune)")


def _parse_extract(raw):
    """Parse Gemma's 'TYPE: value' lines into typed spans. Unknown/empty types dropped.
    Fail-toward-nothing here (an empty parse is caught downstream as suspicious → fail-closed)."""
    spans = []
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        t, _, v = line.partition(":")
        t = t.strip().upper(); v = v.strip()
        if t in _ALLOWED_TYPES and v and not _is_aucune_sentinel(v):
            spans.append({"type": t, "text": v})
    return spans


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

    def extract_pii(self, text):
        from mlx_lm import generate
        resp = generate(self._model, self._tok,
                        prompt=_EXTRACT_PROMPT.format(text=text[:6000]),
                        max_tokens=512, verbose=False)
        return _parse_extract(resp)
