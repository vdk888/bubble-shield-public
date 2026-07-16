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
GLiNER uses two separate tokenisation stages:

  1. **Word-splitting** (whitespace + punctuation): the model's `words_splitter`
     tokenises the text into "words" before inference.  The hard limit is
     `model.config.max_len = 384` WORDS.  (The DeBERTa-v3 encoder underneath
     has `max_position_embeddings = 512`; GLiNER constrains itself to 384.)

  2. **Subword tokenisation** (DeBERTa DebertaV2Tokenizer): only happens
     inside `tokenize_inputs`; its model_max_length is unconstrained.  Confusion
     here is the source of the "384 tokens" wording in the original docstring —
     the limit is 384 *words*, not 384 subword-pieces.

Empirically measured on 15 real FR KYC/finance docs + 1 FR-IR-class tax doc
(2026-06-26, urchade/gliner_multi_pii-v1):

  - char → word-token ratio: ~5.1 chars/word for normal FR finance text
  - 1000 chars → ~150–200 word-tokens for prose → NO truncation
  - Exception: sections with runs of dots/dashes used as form-fill blanks
    (e.g. "Prénom : ....................................") tokenise as
    individual characters → 1500 chars → 600+ word-tokens (TRUNCATED).

The fix has three parts:

  a) DOT-RUN COMPRESSION: before chunking, collapse runs of 3+ consecutive
     dots, dashes, or underscores to a single space.  These are PDF-extraction
     artifacts of form-fill blanks.  They contain no PII; collapsing them
     reduces word-token count from 600+ to ~220, eliminating most truncation.

  b) CHUNK SIZE REDUCTION (1500 → 1000 chars): eliminates ALL remaining
     truncation warnings on real FR tax docs.  At ~5.1 chars/word-token, a
     1000-char chunk produces ≤~200 word-tokens after compression — safely
     under the 384-word GLiNER limit.  The 300-char overlap ensures no name
     block is ever split across chunk boundaries.  (#318, 2026-06-26)

  c) THRESHOLD TUNING (0.45 → 0.30): FR tax docs embed names in ALL-CAPS
     inside form-field rows ("Déclarant 1 - Nom de naissance : DUPONT  LUC").
     The unusual casing and tabular context lower GLiNER's confidence to
     0.27–0.32.  Lowering to 0.30 captures these borderline cases.  The
     profile_sweep already catches the subsequent occurrences of any detected
     name; GLiNER only needs to seed the profile with ONE occurrence.
     On the bench (sample_fr_finance.json, 2026-06-26):
       threshold=0.45: recall=100%, precision=94.3%, F1=97.1%, FP=2
       threshold=0.30: recall=100%, precision=91.7%, F1=95.7%, FP=3
     The 1 additional FP at 0.30 (an abbreviation "SCPI" misread as a SECU
     number) is acceptable: over-redaction is the safe side of the tradeoff.

  d) NOM CONTAINMENT EXTENSION (#318, fix root-cause-1): when a higher-scoring
     NOM sub-span is kept by the main pass but its lower-scoring parent span
     (which fully contains the sub-span) only appears at the lower containment
     threshold (threshold × 0.7 ≈ 0.21), the sub-span's extent is extended to
     the parent's extent.  This ensures trailing name tokens like "PAUL" in
     "FONTAINE MARC PAUL" are never left in clear just because the parent span
     scored below threshold while the sub-span "FONTAINE MARC" scored above it.
     The sub-span's score is preserved unchanged (NOT polluted by the parent's
     lower score), so the fail-closed threshold gate is not weakened.
     Only NOM-type spans participate — checksum-validated IBAN/ISIN/SECU are
     never extended by this logic.

On the FR-IR-class synthetic validation (tax doc with ALL-CAPS names in
address block + form fields):
  - WITHOUT fix: 5 truncation warnings per doc; names detected but only via
    profile_sweep from a low-scoring GLiNER seed in the non-truncated page-2 chunk.
  - WITH fix (dot-compression + chunk-1000 + threshold=0.30 + containment):
    0 truncation warnings; all name tokens including trailing forenames masked.

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
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from bubble_shield.recognizers import Match

# ── Configuration (env-overridable, sane defaults for FR finance) ──────────
DEFAULT_MODEL = os.environ.get("BUBBLE_SHIELD_GLINER_MODEL", "urchade/gliner_multi_pii-v1")
DEFAULT_CHUNK = int(os.environ.get("BUBBLE_SHIELD_GLINER_CHUNK", "1000"))     # chars
DEFAULT_OVERLAP = int(os.environ.get("BUBBLE_SHIELD_GLINER_OVERLAP", "300"))  # chars
# fix #318 (truncation): reduced DEFAULT_CHUNK from 1500 → 1000 chars to eliminate
# all `truncated to 384` warnings on real FR tax docs.  At the measured FR finance
# char→word-token ratio of ~5.1 chars/word, 1000 chars → ≤~200 word-tokens after
# dot-compression, well under GLiNER's 384-word hard limit.  The 300-char overlap
# is unchanged and large enough to ensure a "SURNAME FORENAME FORENAME" block
# (≤~30 chars) is NEVER split across chunk boundaries.
#
# Measured 2026-06-26: lowered from 0.45 → 0.30 to capture ALL-CAPS FR tax-form
# names (FR-IR-class) that score 0.27–0.32.  Bench: recall unchanged at 100%,
# precision 94.3% → 91.7% (+1 FP on abbreviations).  Safe side of the tradeoff.
DEFAULT_THRESHOLD = float(os.environ.get("BUBBLE_SHIELD_GLINER_THRESHOLD", "0.30"))

# Dot-run compression: PDF extraction artifacts (".......", "-------", "_______")
# tokenise as individual characters and inflate word-token counts from ~250 to
# 600+ for 1500-char chunks, triggering truncation.  These runs contain no PII.
# Disable only if downstream needs the literal dot fills (unlikely).
DEFAULT_COMPRESS_DOTS = os.environ.get("BUBBLE_SHIELD_GLINER_COMPRESS_DOTS", "1") not in ("0", "false", "no")
# Min run length to compress (3 consecutive identical non-alnum separators).
_DOT_RUN_RE = re.compile(r'[.…]{3,}|[-–—]{3,}|[_]{3,}')

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
    # #582: a bare city with no birth context ("basé à Nice") is a location
    # mention, not a birthplace — retag to ADRESSE (mask-neutral: both types
    # are identifying+default_cloak). Fixes LIEU_NAISSANCE precision (41.2%).
    "city": "ADRESSE",
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


def _compress_dot_runs(text: str) -> str:
    """Collapse runs of 3+ dots/dashes/underscores to a single space.

    PDF extraction of form-fill blanks (e.g. "Prénom : .............")
    produces runs of separator characters that the GLiNER word-splitter
    tokenises individually.  A 1500-char section that is mostly dots can
    produce 600+ word-tokens, far exceeding the model's 384-word limit.
    Compressing these runs eliminates truncation without affecting PII
    detection (form blanks contain no PII — the user fills them in later).
    """
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


# fix #318 (overlap-span-drop): lower threshold for the containment probe pass.
# When a NOM sub-span scores above the main threshold but its parent span scores
# BELOW it (e.g. "FONTAINE MARC" at 0.556 vs "FONTAINE MARC PAUL" at 0.293),
# the main pass only keeps the sub-span and the trailing name token ("PAUL")
# leaks.  We run a SECOND collect pass at this lower threshold ONLY for NOM spans,
# then extend any kept NOM sub-span to the union of all NOM parent spans that
# contain it.  The sub-span's score is preserved (not polluted by the parent's
# lower score), so the fail-closed threshold gate is not weakened.
#
# Chosen as main_threshold * 0.7 (≈ 0.21 at the default 0.30).  This is low
# enough to catch a 0.293-scoring parent but not so low it floods with spurious
# NOM spans on normal prose.  It is ONLY used for the containment geometry check;
# the score of any extended span is the score of the KEPT sub-span, not the parent.
_NOM_CONTAINMENT_THRESHOLD_FACTOR = float(
    os.environ.get("BUBBLE_SHIELD_GLINER_CONTAIN_FACTOR", "0.7"))

# NOM-type entity types that participate in the containment extension.
# Scoped to person-name types only — does NOT include IBAN, ISIN, ADRESSE etc.
# This prevents the "longer IBAN wins over its sub-span" checksum-precedence
# logic from being bypassed (those types are never involved here).
_NOM_ENTITY_TYPES = frozenset({"NOM"})


def _extend_nom_containment(
    kept: Dict[tuple, tuple],
    parent_spans: List[tuple],
) -> Dict[tuple, tuple]:
    """Extend NOM sub-spans to cover any containing NOM parent spans.

    fix #318 — root cause 1: when a higher-scoring NOM sub-span is kept but a
    lower-scoring parent NOM span (containing the sub-span) was collected only at
    the lower containment threshold, the sub-span's extent is extended to the
    parent's union.  This ensures "PIERRE" (and any other trailing name token) is
    not left unmasked just because the parent scored below the main threshold.

    WHY this doesn't break IBAN-wins-over-NOM checksum precedence:
      - This function only operates on spans where both the kept span AND the
        parent span are NOM-type ("NOM" ∈ _NOM_ENTITY_TYPES).
      - IBAN / ISIN / SECU / SIRET are never in _NOM_ENTITY_TYPES, so a checksum-
        validated structured PII span is NEVER passed in as a parent candidate.
      - The containment extension only WIDENS a NOM span, never narrows it.
        resolve_overlaps() still runs after gliner_matches() returns, so if the
        widened NOM span now overlaps a checksum-validated IBAN, the IBAN
        (priority 95, score 1.0) still wins over the NOM (priority 5, score ≤1).

    WHY this doesn't over-mask non-name types:
      - parent_spans contains ONLY NOM-type spans collected at the low threshold.
      - No other entity type is extended.

    Args:
        kept: the main-threshold result dict: (etype, span_text) → (score, s, e)
        parent_spans: list of (etype, span_text, score, abs_start, abs_end) from
                      the low-threshold pass, NOM-type only.

    Returns:
        A new dict with NOM sub-spans extended where appropriate.
    """
    if not parent_spans:
        return kept

    # Build an index: for each kept NOM span, find any parent span that STRICTLY
    # CONTAINS it (parent.start ≤ sub.start AND sub.end ≤ parent.end, with at
    # least one of those inequalities strict so it's not the same span).
    out = dict(kept)
    for key, (score, s, e) in kept.items():
        etype = key[0]
        if etype not in _NOM_ENTITY_TYPES:
            continue
        # Find the widest NOM parent that contains [s, e).
        best_start, best_end = s, e
        extended = False
        for p_etype, _p_span, _p_score, p_s, p_e in parent_spans:
            if p_etype not in _NOM_ENTITY_TYPES:
                continue
            # Strict containment: parent contains the kept sub-span.
            if p_s <= s and e <= p_e and (p_s < s or e < p_e):
                if (p_e - p_s) > (best_end - best_start):
                    best_start, best_end = p_s, p_e
                    extended = True
        if extended:
            # Build the extended span text from the original `text` stored in
            # caller scope.  We don't have `text` here — the caller will look up
            # the new extent from `text` directly; we signal the new offsets by
            # updating the tuple.  Span text is recomputed by the caller.
            out[key] = (score, best_start, best_end)
    return out


def _collect_gliner_spans(
    model,
    text: str,
    labels: List[str],
    active_label_map: Dict[str, str],
    chunk_size: int,
    overlap: int,
    threshold: float,
    compress_dots: bool,
) -> Dict[tuple, tuple]:
    """Inner collection loop: run chunked GLiNER and return the best-score
    dedup dict: (etype, span_text) → (score, abs_start, abs_end)."""
    best: Dict[tuple, tuple] = {}
    for base, chunk in _chunks(text, chunk_size, overlap):
        inference_chunk = _compress_dot_runs(chunk) if compress_dots else chunk
        try:
            ents = model.predict_entities(inference_chunk, labels, threshold=threshold)
        except Exception:
            continue
        for e in ents:
            etype = active_label_map.get(e.get("label", "").lower())
            if not etype:
                continue
            span = e.get("text", "").strip()
            if not span:
                continue
            score = float(e.get("score", 0.0))
            e_start_in_chunk = int(e.get("start", 0))
            e_end_in_chunk = int(e.get("end", e_start_in_chunk + len(span)))
            if compress_dots:
                orig_idx = chunk.find(span)
                if orig_idx >= 0:
                    e_start_in_chunk = orig_idx
                    e_end_in_chunk = orig_idx + len(span)
            abs_start = base + e_start_in_chunk
            abs_end = base + e_end_in_chunk
            key = (etype, span)
            if key not in best or score > best[key][0]:
                best[key] = (score, abs_start, abs_end)
    return best


def gliner_matches(
    text: str,
    *,
    model_id: str = DEFAULT_MODEL,
    labels: Optional[List[str]] = None,
    chunk_size: int = DEFAULT_CHUNK,
    overlap: int = DEFAULT_OVERLAP,
    threshold: float = DEFAULT_THRESHOLD,
    compress_dots: bool = DEFAULT_COMPRESS_DOTS,
) -> List[Match]:
    """Run chunked GLiNER over `text` and return bubble_shield Match objects.

    Recall-first union across overlapping windows; de-duplicated by
    (entity_type, exact span text), keeping the highest score and the first
    absolute offset. Fail-open: returns [] if GLiNER isn't available.

    `compress_dots` (default True): collapse runs of 3+ dots/dashes/underscores
    before chunking.  This eliminates truncation on PDF-extracted forms without
    affecting PII detection (see module docstring for details).

    fix #318 — overlap-span-drop:
    Runs an additional low-threshold containment probe (threshold * 0.7) for NOM
    spans only. Any NOM sub-span kept by the main pass that is strictly contained
    in a NOM parent found at the lower threshold is EXTENDED to the parent's extent.
    This ensures trailing name tokens (e.g. "PAUL" in "FONTAINE MARC PAUL") are
    not left unmasked when the full-name parent span scored below the main threshold
    but the sub-span scored above it.  The sub-span's score is preserved unchanged.
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

    # Main pass: collect spans at the configured threshold.
    best = _collect_gliner_spans(
        model, text, labels, active_label_map,
        chunk_size, overlap, threshold, compress_dots)

    # fix #318 — NOM containment probe: run a second pass at a lower threshold
    # to discover parent NOM spans that contain a kept sub-span but scored below
    # the main threshold.  Only NOM labels are asked for (minimal, targeted).
    nom_labels = [lbl for lbl, et in active_label_map.items()
                  if et in _NOM_ENTITY_TYPES]
    if nom_labels and best:
        contain_threshold = threshold * _NOM_CONTAINMENT_THRESHOLD_FACTOR
        # Only bother if the low threshold is meaningfully lower than the main.
        if contain_threshold < threshold - 0.01:
            try:
                low_best = _collect_gliner_spans(
                    model, text, nom_labels, active_label_map,
                    chunk_size, overlap, contain_threshold, compress_dots)
                # Build the parent-span list: spans from the low-threshold pass
                # that are NOM-type and NOT already in the main-threshold result.
                parent_spans = [
                    (et, sp, sc, s, en)
                    for (et, sp), (sc, s, en) in low_best.items()
                    if et in _NOM_ENTITY_TYPES and (et, sp) not in best
                ]
                if parent_spans:
                    best = _extend_nom_containment(best, parent_spans)
            except Exception:
                pass  # fail-open: containment probe failure never drops spans

    out: List[Match] = []
    for (etype, span), (score, s, en) in best.items():
        # Re-read the actual span text from `text` using the (possibly extended)
        # offsets — the key still holds the original span text, but after
        # _extend_nom_containment the offsets may be wider.
        actual_value = text[s:en] if 0 <= s < en <= len(text) else span
        out.append(Match(start=s, end=en, entity_type=etype, value=actual_value,
                         score=score, priority=5))  # priority<regex so checksum PII wins
    return out


def make_gliner_detector(**cfg):
    """Return a `Callable[[str], List[Match]]` with config baked in, for
    `AnonymizationEngine(extra_detectors=[make_gliner_detector(...)])`."""
    def _detector(text: str) -> List[Match]:
        return gliner_matches(text, **cfg)
    return _detector
