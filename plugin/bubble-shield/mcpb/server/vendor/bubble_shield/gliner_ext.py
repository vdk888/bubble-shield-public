"""
gliner_ext.py — OPTIONAL local NER layer using GLiNER (the prose/context layer).

Why GLiNER over a 7B LLM for this job (validated 2026-06-01 on real CGP
KYC docs): GLiNER is a 0.3B span-extraction model (multilingual, incl. FR) that
runs on CPU/MPS with no GPU, in parallel, with no prompt fragility. On the real
a real DCC it matched a 7B-class model's usefulness at a fraction of the cost and
latency. It is the right "soft PII" layer (person names, addresses, prose-only
identifiers) on top of the checksum-backed regex layer in `recognizers.py`.

THE CHUNKING IS LOAD-BEARING, NOT AN OPTIMISATION
-------------------------------------------------
GLiNER has a hard ~384-token window. A single `predict_entities` call on a full
dossier (10k+ chars) SILENTLY TRUNCATES to the first ~1500 chars and drops the
rest (observed: "Sentence of length 2274 has been truncated to 384"). On the real
DCC that meant 5 entities instead of 13 — the client's mobile, personal e-mail and
the partner's data all lived past the cutoff. So we ALWAYS run a sliding window
with overlap and UNION the spans (recall-first: a missed PII is the real risk).
Overlap guarantees an entity straddling a chunk boundary is seen whole in at least
one window. (Joris's multi-pass idea, confirmed by measurement.)

Design choices (mirrors llm_ext.py):
  - **Off by default & fail-open**: if gliner/torch aren't installed or the model
    can't load, `gliner_matches` returns [] and the engine behaves exactly like the
    pure-regex build. The layer only ever ADDS recall.
  - **Lazy, cached model load**: import + load happen on first use, then the model
    is memoised for the process (loading is the slow part, ~2-5s; inference is fast).
  - **Scores carried through**: GLiNER confidences flow into the fail-closed
    threshold — a name found only by GLiNER below threshold is "review", not "safe".
  - **Label → bubble_shield entity-type mapping**: GLiNER returns the natural-language
    label we asked for; we map it to bubble_shield's canonical types (NOM, ADRESSE, …).

Enable it:
    AnonymizationEngine(extra_detectors=[gliner_matches])
or pass a configured detector via `make_gliner_detector(...)`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from bubble_shield.recognizers import Match

# ── Configuration (env-overridable, sane defaults for FR finance) ──────────
DEFAULT_MODEL = os.environ.get("BUBBLE_SHIELD_GLINER_MODEL", "urchade/gliner_multi_pii-v1")
DEFAULT_CHUNK = int(os.environ.get("BUBBLE_SHIELD_GLINER_CHUNK", "1500"))     # chars
DEFAULT_OVERLAP = int(os.environ.get("BUBBLE_SHIELD_GLINER_OVERLAP", "300"))  # chars
DEFAULT_THRESHOLD = float(os.environ.get("BUBBLE_SHIELD_GLINER_THRESHOLD", "0.45"))

# Natural-language labels we ask GLiNER for → bubble_shield canonical entity types.
# GLiNER works best with lower/title-case natural labels, so we keep them human
# and map afterwards. Order matters only for readability.
LABEL_TO_TYPE: Dict[str, str] = {
    "person name": "NOM",
    "full name": "NOM",
    # Family-member names are PII too — and the MOST sensitive misses on real
    # KYC docs were exactly these (spouse, and a MINOR CHILD's name in a
    # relations table). Asking GLiNER explicitly for them lifts recall on the
    # relational PII the spec cares about ("les enfants, le nom de son chien").
    "family member name": "NOM",
    "child name": "NOM",
    "spouse name": "NOM",
    "address": "ADRESSE",
    "postal address": "ADRESSE",
    # Birthplace is identifying (city after a DOB was missed because no
    # label asked for them). Map to a dedicated type so the vault is clear.
    "place of birth": "LIEU_NAISSANCE",
    "city": "LIEU_NAISSANCE",
    "phone number": "TEL",
    "email": "EMAIL",
    "email address": "EMAIL",
    "date of birth": "DATE_NAISSANCE",
    # Marriage / PACS dates sit far from their form label; ask for them directly.
    "marriage date": "DATE_EVENEMENT",
    "date": "DATE_EVENEMENT",
    "passport number": "PIECE_IDENTITE",
    "identity document number": "PIECE_IDENTITE",
    "social security number": "SECU",
    "tax number": "NUM_FISCAL",
    "iban": "IBAN",
    "bank account number": "IBAN",
}
DEFAULT_LABELS: List[str] = list(LABEL_TO_TYPE.keys())


def load_label_map(path=None) -> Dict[str, str]:
    """Return LABEL_TO_TYPE merged with any custom gliner_labels from
    custom_fields.json (label → entity_type). Fail-soft: returns a copy of the
    built-in map if the custom config is missing/unreadable."""
    merged = dict(LABEL_TO_TYPE)
    try:
        from bubble_shield.custom_recognizers import load_custom_fields_config
        cfg = load_custom_fields_config(path)
    except Exception:
        return merged
    for entry in cfg.get("gliner_labels", []):
        label = str(entry.get("label", "")).strip().lower()
        etype = str(entry.get("entity_type", "")).strip()
        if label and etype:
            merged[label] = etype
    return merged


def default_labels(path=None) -> List[str]:
    """Dynamic DEFAULT_LABELS including any custom labels (the labels we ask
    GLiNER for). Use this over the DEFAULT_LABELS constant when custom fields
    should participate in detection."""
    return list(load_label_map(path).keys())


# Process-wide model cache (model id → loaded GLiNER instance).
_MODEL_CACHE: Dict[str, object] = {}


def _load_model(model_id: str):
    """Lazy, cached GLiNER load. Returns None if the backend is unavailable
    (fail-open: the engine then behaves as pure-regex)."""
    if model_id in _MODEL_CACHE:
        return _MODEL_CACHE[model_id]
    try:
        from gliner import GLiNER  # heavy import; only when actually used
    except Exception:
        _MODEL_CACHE[model_id] = None
        return None
    try:
        model = GLiNER.from_pretrained(model_id)
        model.eval()
        _MODEL_CACHE[model_id] = model
        return model
    except Exception:
        _MODEL_CACHE[model_id] = None
        return None


def _chunks(text: str, size: int, overlap: int):
    """Yield (base_offset, chunk_text) sliding windows with overlap."""
    if size <= overlap:
        raise ValueError("chunk size must exceed overlap")
    i = 0
    n = len(text)
    while i < n:
        yield i, text[i:i + size]
        if i + size >= n:
            break
        i += size - overlap


def gliner_matches(
    text: str,
    *,
    model_id: str = DEFAULT_MODEL,
    labels: Optional[List[str]] = None,
    chunk_size: int = DEFAULT_CHUNK,
    overlap: int = DEFAULT_OVERLAP,
    threshold: float = DEFAULT_THRESHOLD,
) -> List[Match]:
    """Run chunked GLiNER over `text` and return bubble_shield Match objects.

    Recall-first union across overlapping windows; de-duplicated by
    (entity_type, exact span text), keeping the highest score and the first
    absolute offset. Fail-open: returns [] if GLiNER isn't available.
    """
    model = _load_model(model_id)
    if model is None:
        return []
    # Always resolve the active label map (built-in + custom). When the caller
    # gives no explicit labels, ask GLiNER for the full merged set so custom
    # labels participate; either way the etype lookup uses the merged map so a
    # custom label maps to its configured entity type.
    active_label_map = load_label_map()
    labels = labels or list(active_label_map.keys())

    # key = (entity_type, span_text) → (best_score, abs_start, abs_end)
    best: Dict[tuple, tuple] = {}
    for base, chunk in _chunks(text, chunk_size, overlap):
        try:
            ents = model.predict_entities(chunk, labels, threshold=threshold)
        except Exception:
            continue  # a flaky window never breaks the whole pass
        for e in ents:
            etype = active_label_map.get(e.get("label", "").lower())
            if not etype:
                continue
            span = e.get("text", "").strip()
            if not span:
                continue
            score = float(e.get("score", 0.0))
            abs_start = base + int(e.get("start", 0))
            abs_end = base + int(e.get("end", abs_start + len(span)))
            key = (etype, span)
            if key not in best or score > best[key][0]:
                best[key] = (score, abs_start, abs_end)

    out: List[Match] = []
    for (etype, span), (score, s, en) in best.items():
        out.append(Match(start=s, end=en, entity_type=etype, value=span,
                         score=score, priority=5))  # priority<regex so checksum PII wins
    return out


def make_gliner_detector(**cfg):
    """Return a `Callable[[str], List[Match]]` with config baked in, for
    `AnonymizationEngine(extra_detectors=[make_gliner_detector(...)])`."""
    def _detector(text: str) -> List[Match]:
        return gliner_matches(text, **cfg)
    return _detector
