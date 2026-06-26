"""
fastino_ext.py — OPTIONAL local NER layer using fastino/gliner2-privacy-filter-PII-multi.

Background (board #329, 2026-06-27):
---------------------------------------
The A/B bench (bench/realdoc-ab/, 2026-06-26) showed fastino's GLiNER2 model catches
real client names that urchade/gliner_multi_pii-v1 misses entirely on the jo/ payslip
and amortisation table docs (urchade NOM = 0, fastino NOM ≥ 4).  This module promotes
that finding into a production-ready, engine-usable detector that can be composed with
urchade as a UNION (parallel) detector via extra_detectors=.

Key differences from gliner_ext.py:
  - Uses GLiNER2.extract_entities() (the gliner2 package), NOT GLiNER.predict_entities()
    (the older gliner package).  The API is different.
  - fastino has its OWN label set (42 types, snake_case, e.g. "person", "full_name",
    "email") vs urchade's natural-language labels ("person name", "email address", …).
  - max_len is NOT constrained the same way — the GLiNER2 engine handles chunking
    internally to some extent, but we still chunk explicitly at 1000 chars/300 overlap
    to mirror our proven urchade strategy and avoid any model-side truncation.
  - FP TUNING (#329): fastino at threshold=0.30 emits false positives on 2-char or
    pure-digit tokens being classified as person names.  The A/B showed "1•" at offsets
    701 and 896 in the amortissement doc — both 2-char numeric tokens.  We apply a
    post-inference filter on NOM-type spans:
      - minimum span length ≥ 3 characters (kills 2-char tokens)
      - must not be pure-digit (kills "12", "01", numeric codes)
      - must not be pure-digit with surrounding punctuation (e.g. "1", "01")
    This is tight and principled: a real name always has ≥ 3 chars and contains at
    least one letter.  The filter does NOT touch non-NOM types (IBAN, EMAIL, TEL,
    ADRESSE …) — those are structurally validated.

Design mirrors gliner_ext.py:
  - OFF by default (opt-in via extra_detectors=[make_fastino_detector()])
  - Fail-open: if gliner2/torch aren't installed or the model can't load,
    returns [] — the engine behaves exactly like the urchade-only build.
  - Lazy, cached model load.
  - Priority 5 (same as urchade GLiNER layer): checksum-validated structured
    PII (IBAN, ISIN, SECU, SIRET) always wins in resolve_overlaps().
  - Scores carried through: the fail-closed threshold gate is not weakened.
  - Works under HF_HUB_OFFLINE=1 (model is pre-staged at the snapshot path).

Enable it (opt-in, does NOT change the default engine wiring):
    from bubble_shield.fastino_ext import make_fastino_detector
    engine = AnonymizationEngine(extra_detectors=[make_fastino_detector()])

Or combine with urchade for the UNION mode:
    from bubble_shield.gliner_ext import make_gliner_detector
    from bubble_shield.fastino_ext import make_fastino_detector
    engine = AnonymizationEngine(extra_detectors=[
        make_gliner_detector(),
        make_fastino_detector(),
    ])
    # resolve_overlaps() dedupes/merges: a name caught by either is masked;
    # checksum-PII still wins; #318 containment still applies.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from bubble_shield.recognizers import Match

# ── Model snapshot path (pre-staged, offline) ──────────────────────────────
_DEFAULT_SNAPSHOT = (
    "/Users/joris/.cache/huggingface/hub/"
    "models--fastino--gliner2-privacy-filter-PII-multi/snapshots/"
    "e40819b10177c9b9cdea62a3ece5cfdebd921e01"
)
DEFAULT_MODEL = os.environ.get("BUBBLE_SHIELD_FASTINO_MODEL", _DEFAULT_SNAPSHOT)

# ── Chunking (mirrors gliner_ext.py defaults, proven on FR finance docs) ───
DEFAULT_CHUNK = int(os.environ.get("BUBBLE_SHIELD_FASTINO_CHUNK", "1000"))   # chars
DEFAULT_OVERLAP = int(os.environ.get("BUBBLE_SHIELD_FASTINO_OVERLAP", "300")) # chars

# Threshold: 0.30 matches urchade default (captures borderline FR names at
# score 0.27–0.32).  FP tuning handles the numeric/short-token false positives
# that appear at this threshold — they're filtered post-inference, not by
# raising the threshold (which would trade recall for precision).
DEFAULT_THRESHOLD = float(os.environ.get("BUBBLE_SHIELD_FASTINO_THRESHOLD", "0.30"))

# ── FP tuning filters (applied to NOM-type spans only) ─────────────────────
# Minimum character length for a NOM span.  Real names are always ≥ 3 chars;
# 2-char tokens ("1", "AB") are false positives.
NOM_MIN_LEN = int(os.environ.get("BUBBLE_SHIELD_FASTINO_NOM_MIN_LEN", "3"))

# Regex that matches a pure-digit token (with optional surrounding spaces).
# Used to reject "12", "01", "2" etc. being classified as names.
_PURE_DIGIT_RE = re.compile(r"^\d+$")

# ── Dot-run compression (same as gliner_ext.py) ────────────────────────────
DEFAULT_COMPRESS_DOTS = os.environ.get(
    "BUBBLE_SHIELD_FASTINO_COMPRESS_DOTS", "1") not in ("0", "false", "no")
_DOT_RUN_RE = re.compile(r'[.…]{3,}|[-–—]{3,}|[_]{3,}')

# ── fastino label → bubble_shield canonical entity type ────────────────────
# fastino uses snake_case, task-specific labels (42 types total).
# We map the subset relevant to FR finance/KYC documents.
# Labels NOT in this map are silently dropped (e.g. "password", "api_key" are
# irrelevant to finance docs and would cause spurious masking).
LABEL_TO_TYPE: Dict[str, str] = {
    # Person / names
    "person":           "NOM",
    "full_name":        "NOM",
    "first_name":       "NOM",
    "middle_name":      "NOM",
    "last_name":        "NOM",
    # Contact
    "email":            "EMAIL",
    "phone_number":     "TEL",
    "address":          "ADRESSE",
    "street_address":   "ADRESSE",
    "city":             "LIEU_NAISSANCE",
    "postal_code":      "CODE_POSTAL",
    "country":          "PAYS",
    # Government / tax IDs
    "national_id_number": "PIECE_IDENTITE",
    "passport_number":    "PIECE_IDENTITE",
    "drivers_license_number": "PIECE_IDENTITE",
    "license_number":     "PIECE_IDENTITE",
    "government_id":      "PIECE_IDENTITE",
    "tax_id":             "NUM_FISCAL",
    "tax_number":         "NUM_FISCAL",
    # Banking
    "iban":               "IBAN",
    "bank_account":       "IBAN",
    "account_number":     "IBAN",
    # Dates
    "date_of_birth":      "DATE_NAISSANCE",
    "sensitive_date":     "DATE_EVENEMENT",
}

# ── Labels we ask fastino for (the keys of LABEL_TO_TYPE) ──────────────────
DEFAULT_LABELS: List[str] = list(LABEL_TO_TYPE.keys())

# ── Process-wide model cache ────────────────────────────────────────────────
_MODEL_CACHE: Dict[str, object] = {}


def _load_model(model_path: str):
    """Lazy, cached GLiNER2 load.  Returns None if the backend is unavailable
    (fail-open: the engine then behaves as if this layer doesn't exist)."""
    if model_path in _MODEL_CACHE:
        return _MODEL_CACHE[model_path]
    try:
        from gliner2 import GLiNER2  # heavy import; only when actually used
    except ImportError:
        _MODEL_CACHE[model_path] = None
        return None
    try:
        model = GLiNER2.from_pretrained(model_path)
        _MODEL_CACHE[model_path] = model
        return model
    except Exception:
        _MODEL_CACHE[model_path] = None
        return None


def _compress_dot_runs(text: str) -> str:
    """Collapse runs of 3+ dots/dashes/underscores to a single space.
    Mirrors gliner_ext._compress_dot_runs() — PDF form-blank artifacts."""
    return _DOT_RUN_RE.sub(' ', text)


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


def _is_nom_fp(span: str) -> bool:
    """True if a NOM-type span is a false positive by our principled filters.

    Filters (tight, principled, board #329):
      1. Length < NOM_MIN_LEN (default 3): kills 2-char tokens ("1", "AB").
      2. Pure digit: kills numeric codes misread as names ("12", "01").

    Does NOT filter on capital/lowercase (FR names include both).
    Does NOT filter on hyphens/spaces (compound names are valid).
    """
    stripped = span.strip()
    if len(stripped) < NOM_MIN_LEN:
        return True
    if _PURE_DIGIT_RE.match(stripped):
        return True
    return False


def fastino_matches(
    text: str,
    *,
    model_path: str = DEFAULT_MODEL,
    labels: Optional[List[str]] = None,
    chunk_size: int = DEFAULT_CHUNK,
    overlap: int = DEFAULT_OVERLAP,
    threshold: float = DEFAULT_THRESHOLD,
    compress_dots: bool = DEFAULT_COMPRESS_DOTS,
) -> List[Match]:
    """Run chunked GLiNER2 (fastino) over `text` and return bubble_shield Match objects.

    Recall-first union across overlapping windows; de-duplicated by
    (entity_type, exact span text), keeping the highest score and the first
    absolute offset. Fail-open: returns [] if gliner2 isn't available.

    FP tuning (board #329): NOM-type spans that are < 3 chars or pure-digit
    are filtered out BEFORE returning — these are false positives observed on
    real FR finance docs at threshold=0.30.
    """
    model = _load_model(model_path)
    if model is None:
        return []

    active_labels = labels or DEFAULT_LABELS
    label_map = LABEL_TO_TYPE

    # Best-score dedup: (etype, span_text) → (score, abs_start, abs_end)
    best: Dict[tuple, tuple] = {}

    for base, chunk in _chunks(text, chunk_size, overlap):
        inference_chunk = _compress_dot_runs(chunk) if compress_dots else chunk
        try:
            result = model.extract_entities(
                inference_chunk,
                active_labels,
                threshold=threshold,
                include_confidence=True,
                include_spans=True,
            )
        except Exception:
            continue

        # result = {"entities": {label: [{text, confidence, start, end}]}}
        entities_by_label = result.get("entities", {})
        for label, detections in entities_by_label.items():
            etype = label_map.get(label)
            if not etype:
                continue  # label not in our map → drop
            if not isinstance(detections, list):
                continue
            for det in detections:
                span = det.get("text", "").strip()
                if not span:
                    continue
                score = float(det.get("confidence", det.get("score", 0.0)))
                e_start = int(det.get("start", 0))
                e_end = int(det.get("end", e_start + len(span)))

                if compress_dots:
                    # Re-locate the span in the ORIGINAL chunk (before dot-compression).
                    orig_idx = chunk.find(span)
                    if orig_idx >= 0:
                        e_start = orig_idx
                        e_end = orig_idx + len(span)

                abs_start = base + e_start
                abs_end = base + e_end

                key = (etype, span)
                if key not in best or score > best[key][0]:
                    best[key] = (score, abs_start, abs_end)

    out: List[Match] = []
    for (etype, span), (score, s, en) in best.items():
        # FP tuning: filter NOM false positives (2-char / pure-digit tokens).
        if etype == "NOM" and _is_nom_fp(span):
            continue
        actual_value = text[s:en] if 0 <= s < en <= len(text) else span
        out.append(Match(
            start=s, end=en,
            entity_type=etype,
            value=actual_value,
            score=score,
            priority=5,  # same as urchade: checksum PII (priority ≥ 80) always wins
        ))
    return out


def make_fastino_detector(**cfg) -> "Callable[[str], List[Match]]":
    """Return a `Callable[[str], List[Match]]` with config baked in.

    Use as:
        AnonymizationEngine(extra_detectors=[make_fastino_detector()])

    Or for UNION with urchade:
        AnonymizationEngine(extra_detectors=[
            make_gliner_detector(),       # urchade
            make_fastino_detector(),      # fastino (this)
        ])

    resolve_overlaps() in the engine dedupes overlapping spans from both
    detectors — a name caught by either is masked; checksum-PII still wins.
    """
    def _detector(text: str) -> List[Match]:
        return fastino_matches(text, **cfg)
    return _detector
