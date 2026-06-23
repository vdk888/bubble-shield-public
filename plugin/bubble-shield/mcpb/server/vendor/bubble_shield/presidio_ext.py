"""
presidio_ext.py — OPTIONAL NER layer to raise name/location recall.

Off by default. The regex engine runs everywhere with zero ML deps; this
module adds Microsoft Presidio + spaCy NER for PERSON/LOCATION when the
operator installs them (the meeting's "modèle local plus léger" option).

It degrades gracefully: if presidio/spacy aren't installed, `ner_matches`
returns [] and the engine behaves exactly as the pure-regex build.

To enable:
    pip install presidio-analyzer spacy
    python -m spacy download fr_core_news_lg
then pass the matches into detection (see README "Activer la couche NER").
"""
from __future__ import annotations

from typing import List

from bubble_shield.recognizers import Match

# Map Presidio entity labels → our types.
_LABEL_MAP = {
    "PERSON": "NOM",
    "LOCATION": "ADRESSE",
    "PER": "NOM",
    "LOC": "ADRESSE",
}

_ANALYZER = None
_AVAILABLE: bool | None = None


def is_available() -> bool:
    """True iff presidio + a spaCy model can be loaded (cached)."""
    global _ANALYZER, _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        from presidio_analyzer import AnalyzerEngine  # noqa: WPS433
        from presidio_analyzer.nlp_engine import NlpEngineProvider  # noqa: WPS433
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "fr", "model_name": "fr_core_news_lg"}],
        })
        _ANALYZER = AnalyzerEngine(nlp_engine=provider.create_engine(),
                                   supported_languages=["fr"])
        _AVAILABLE = True
    except Exception:        # not installed / model missing → stay pure-regex
        _ANALYZER = None
        _AVAILABLE = False
    return _AVAILABLE


def ner_matches(text: str, score: float = 0.75) -> List[Match]:
    """Return PERSON/LOCATION spans from Presidio NER, or [] if unavailable.

    Score defaults below the structured-PII 1.0 so the fail-closed threshold
    treats ML guesses as 'review' rather than 'certain'.
    """
    if not is_available() or _ANALYZER is None:
        return []
    out: List[Match] = []
    for r in _ANALYZER.analyze(text=text, language="fr"):
        etype = _LABEL_MAP.get(r.entity_type)
        if not etype:
            continue
        out.append(Match(r.start, r.end, etype, text[r.start:r.end],
                         min(score, float(r.score)), priority=48))
    return out
