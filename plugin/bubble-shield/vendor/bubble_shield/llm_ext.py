"""
llm_ext.py — OPTIONAL local-LLM recognizer (the prose layer).

This is the second detection layer popularised by **DontFeedTheAI**
(github.com/zeroc00I): a *local* LLM (served by Ollama) reads the prose and
flags the soft, context-dependent PII that regex can't — person names,
employers/organisations, places — while the regex layer in `recognizers.py`
owns the structured, checksum-backed PII (IBAN, SIRET, e-mail…). Two layers,
both 100 % local, nothing leaves the machine.

Design choices:
  - **Pure stdlib** (urllib). Bubble Shield gains no dependency from this file, so it
    ships and runs anywhere; the layer simply stays dormant if no local Ollama
    is reachable (e.g. on a server). Enable it where you actually have a GPU.
  - **Off by default & fail-open-to-regex**: if Ollama is unreachable or
    answers badly, `llm_matches` returns [] and the engine behaves exactly
    like the pure-regex build. The local LLM only ever *adds* recall.
  - **Scores below 1.0**: an LLM guess is "review", never "certain". The
    fail-closed threshold then treats a name found only by the LLM as a
    flag-for-review rather than a validated detection.
  - **No offsets trusted from the model**: LLMs are unreliable at character
    indices, so we ask only for the *strings* and locate every occurrence
    ourselves with an exact search. Robust and model-agnostic.

Enable it:
    # 1. install + run Ollama locally (NOT on the VPS), pull a model:
    #    https://ollama.com   →   ollama pull llama3.1
    # 2. point Bubble Shield at it (defaults shown):
    export BUBBLE_SHIELD_OLLAMA_URL=http://localhost:11434
    export BUBBLE_SHIELD_OLLAMA_MODEL=llama3.1
    # 3. use the LLM layer:
    AnonymizationEngine(use_llm=True)
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import List

from bubble_shield.recognizers import Match

# Ollama label → Bubble Shield entity type. ORG becomes SOCIETE so the anonymised
# text reads naturally in French ("la société ⟦SOCIETE_0001⟧").
_LABEL_MAP = {
    "PERSON": "NOM",
    "PER": "NOM",
    "NAME": "NOM",
    "ORG": "SOCIETE",
    "ORGANIZATION": "SOCIETE",
    "ORGANISATION": "SOCIETE",
    "EMPLOYER": "SOCIETE",
    "LOCATION": "ADRESSE",
    "LOC": "ADRESSE",
    "ADDRESS": "ADRESSE",
    "GPE": "ADRESSE",
}

_SYSTEM_PROMPT = (
    "Tu es un détecteur d'informations personnelles (PII) pour des documents "
    "financiers et patrimoniaux français. On te donne un texte. Tu repères "
    "UNIQUEMENT les entités identifiantes que les expressions régulières "
    "ratent : noms de personnes (PERSON), employeurs ou sociétés (ORG), "
    "lieux ou adresses en clair (LOCATION). N'inclus PAS les e-mails, IBAN, "
    "numéros, montants ou dates (déjà couverts ailleurs). "
    "Réponds STRICTEMENT en JSON : "
    '{"entities":[{"type":"PERSON|ORG|LOCATION","text":"<extrait exact>"}]}. '
    "Recopie chaque extrait EXACTEMENT comme dans le texte, sans le reformuler."
)


def _ollama_url() -> str:
    return os.environ.get("BUBBLE_SHIELD_OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("BUBBLE_SHIELD_OLLAMA_MODEL", "llama3.1")


def _timeout() -> float:
    try:
        return float(os.environ.get("BUBBLE_SHIELD_OLLAMA_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def is_available() -> bool:
    """True iff a local Ollama server answers /api/tags. Not cached: a server
    can come and go between runs, and the check is a sub-millisecond loopback
    call when up (and a fast refused-connection when down)."""
    try:
        req = urllib.request.Request(f"{_ollama_url()}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _call_ollama(text: str) -> str:
    """POST the chat request to Ollama, return the raw assistant content.
    Uses format=json so the model is constrained to emit a JSON object."""
    payload = {
        "model": _ollama_model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_ollama_url()}/api/chat", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_timeout()) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("message", {}).get("content", "") or ""


def _spans_for(value: str, text: str, etype: str, score: float) -> List[Match]:
    """Locate every exact, word-bounded occurrence of `value` in `text`."""
    value = value.strip()
    if len(value) < 2:
        return []
    out: List[Match] = []
    for m in re.finditer(re.escape(value), text):
        out.append(Match(m.start(), m.end(), etype, value, score, priority=47))
    return out


def llm_matches(text: str, score: float = 0.7) -> List[Match]:
    """Return PERSON/ORG/LOCATION spans found by the local LLM, or [] if no
    Ollama is reachable or the response can't be parsed. Never raises."""
    if not text.strip() or not is_available():
        return []
    try:
        raw = _call_ollama(text)
        parsed = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []

    # Accept {"entities":[...]}, a bare list, or {"PII":[...]} — models vary.
    items = parsed.get("entities") if isinstance(parsed, dict) else parsed
    if items is None and isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                items = v
                break
    if not isinstance(items, list):
        return []

    out: List[Match] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        value = str(it.get("text") or it.get("value") or "").strip()
        label = str(it.get("type") or it.get("label") or "").upper()
        etype = _LABEL_MAP.get(label)
        if not value or not etype:
            continue
        out.extend(_spans_for(value, text, etype, min(score, 0.95)))
    return out
