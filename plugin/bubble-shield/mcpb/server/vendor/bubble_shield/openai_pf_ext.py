"""
openai_pf_ext.py — OpenAI Privacy Filter ONNX adapter (Phase 2, DEFAULT OFF).

WHY A SEPARATE ADAPTER (not GLiNER)
-------------------------------------
openai/privacy-filter is OpenAIPrivacyFilterForTokenClassification — a sparse
MoE token-classifier (~1.5B params, ~50M active). It is NOT a GLiNER model:
  • GLiNER.from_pretrained() / predict_entities() will NOT load it.
  • It uses BIOES tagging with constrained Viterbi decoding, controlled by
    viterbi_calibration.json (6 transition-bias scalars).
  • It outputs per-token logit vectors (num_tokens × num_labels), not span
    probabilities — we decode spans ourselves.
  • It needs the HF `tokenizers` fast tokenizer for offset_mapping.

ONNX FILES (huggingface.co/openai/privacy-filter)
  onnx/model.onnx + external data (graph only, ~137KB, data file ~2.8GB)
  onnx/model_quantized.onnx + .onnx_data (~1.62 GB) ← INT8
  onnx/model_q4.onnx + .onnx_data (~917 MB)         ← Q4, recommended for M4

CATEGORY → CANONICAL TYPE MAPPING
  private_person  → NOM
  private_address → ADRESSE
  private_email   → EMAIL
  private_phone   → TEL
  private_date    → DATE_EVENEMENT
  private_url     → URL          (new type, see policy.py)
  account_number  → (skipped)    regex core owns IBAN/ISIN
  secret          → SECRET       (new type, see policy.py)

CHUNKING
  Same 384-token / ~1500-char window problem as GLiNER. We reuse `_chunks`
  from gliner_ext and union across windows (recall-first).

FAIL-OPEN
  If onnxruntime / tokenizers are unavailable, or the model dir is missing,
  returns [] — the engine keeps all regex-core matches intact.

This module does NOT download the model. Use bubble_shield_setup_ml.py --openai
to fetch it. In unit tests a MockONNXSession is injected via the
BUBBLE_SHIELD_OPENAI_MOCK environment variable or by monkey-patching
_MODEL_CACHE.

DEFAULT OFF
  The daemon only calls this when detector.mode is "openai" or "both",
  which requires an explicit config change (custom_fields.json). The default
  mode is "gliner", so production behaviour is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bubble_shield.recognizers import Match

# ── Configuration (env-overridable) ────────────────────────────────────────
DEFAULT_MODEL_DIR = os.environ.get(
    "BUBBLE_SHIELD_OPENAI_MODEL", "")       # set by daemon from ml.json
DEFAULT_ONNX_FILE = os.environ.get(
    "BUBBLE_SHIELD_OPENAI_ONNX", "onnx/model_q4.onnx")  # q4 = best M4 fit
DEFAULT_CHUNK = int(os.environ.get("BUBBLE_SHIELD_OPENAI_CHUNK", "1500"))  # chars
DEFAULT_OVERLAP = int(os.environ.get("BUBBLE_SHIELD_OPENAI_OVERLAP", "300"))
DEFAULT_THRESHOLD = float(os.environ.get("BUBBLE_SHIELD_OPENAI_THRESHOLD", "0.45"))

# 8 OpenAI categories (note: "private_" prefixes). account_number and
# secret use the full key from the ONNX label vocabulary.
OPENAI_LABEL_TO_TYPE: Dict[str, str] = {
    "private_person":  "NOM",
    "private_address": "ADRESSE",
    "private_email":   "EMAIL",
    "private_phone":   "TEL",
    "private_date":    "DATE_EVENEMENT",
    "private_url":     "URL",
    # account_number → skipped: regex core owns IBAN/ISIN/SIREN with checksums
    "secret":          "SECRET",
}

# BIOES tag prefixes for each label
_BIOES_PREFIXES = ("B-", "I-", "E-", "S-")

# Process-level model cache: model_dir → (session, tokenizer, label_list, viterbi_bias)
_MODEL_CACHE: Dict[str, Any] = {}


# ── Viterbi transition constraints ──────────────────────────────────────────

def _build_transition_mask(num_labels: int) -> List[List[float]]:
    """Return a (num_states × num_states) matrix where -1e9 marks forbidden
    transitions and 0.0 marks allowed ones.

    State encoding (for each entity type c, 0-indexed):
      S-c = c*4+0, B-c = c*4+1, I-c = c*4+2, E-c = c*4+3, O = num_labels-1

    BIOES valid transition rules:
      After S: can start new S/B, or go O
      After B: must go I or E of same type
      After I: must go I or E of same type
      After E: can start new S/B, or go O
      After O: can start S/B, or stay O

    This implements the "constrained" in "constrained Viterbi".
    """
    # We don't actually build a full matrix per entity type here; instead we
    # check validity at decode time based on the previous state's BIOES prefix
    # and entity type — more readable and avoids a large matrix allocation.
    # This function is reserved for future matrix-based implementation.
    pass


def _decode_viterbi(
    logits: List[List[float]],   # [seq_len, num_states]
    labels: List[str],           # e.g. ["S-private_person", "B-private_person", …, "O"]
    bias: Optional[Dict[str, float]] = None,
) -> List[Tuple[int, int, str, float]]:
    """Constrained Viterbi decode over BIOES logits.

    Applies the BIOES validity constraints (invalid transitions get -1e9
    penalty) plus optional calibration biases from viterbi_calibration.json.

    Returns: list of (token_start_idx, token_end_idx_inclusive, category, score)
    """
    bias = bias or {}
    begin_b = bias.get("begin_bias", 0.0)
    inside_b = bias.get("inside_bias", 0.0)
    end_b = bias.get("end_bias", 0.0)
    single_b = bias.get("single_bias", 0.0)
    outside_b = bias.get("outside_bias", 0.0)
    temp = bias.get("transition_temp", 1.0) or 1.0  # avoid divide-by-zero

    n_tokens = len(logits)
    n_states = len(labels)
    NEG_INF = -1e9

    if n_tokens == 0 or n_states == 0:
        return []

    # Build index lookups
    # label_info[i] = (prefix, category) or ("O", None)
    label_info: List[Tuple[str, Optional[str]]] = []
    for lbl in labels:
        if lbl == "O":
            label_info.append(("O", None))
        elif lbl.startswith("S-"):
            label_info.append(("S", lbl[2:]))
        elif lbl.startswith("B-"):
            label_info.append(("B", lbl[2:]))
        elif lbl.startswith("I-"):
            label_info.append(("I", lbl[2:]))
        elif lbl.startswith("E-"):
            label_info.append(("E", lbl[2:]))
        else:
            label_info.append(("O", None))  # fallback

    def _apply_bias(score: float, prefix: str) -> float:
        if prefix == "S":
            return score + single_b
        elif prefix == "B":
            return score + begin_b
        elif prefix == "I":
            return score + inside_b
        elif prefix == "E":
            return score + end_b
        else:  # O
            return score + outside_b

    def _is_valid_transition(prev_prefix: str, prev_cat: Optional[str],
                              cur_prefix: str, cur_cat: Optional[str]) -> bool:
        """Return True if prev→cur is a legal BIOES transition."""
        if prev_prefix in ("O", "S", "E"):
            # After O/S/E: can start S, B, or O
            return cur_prefix in ("S", "B", "O")
        elif prev_prefix in ("B", "I"):
            # After B/I: MUST continue with I or E of the SAME type
            return cur_prefix in ("I", "E") and cur_cat == prev_cat
        return False  # shouldn't happen

    # Viterbi DP
    # dp[t][s] = best log-prob arriving at state s at token t
    # back[t][s] = previous state
    dp = [[NEG_INF] * n_states for _ in range(n_tokens)]
    back = [[-1] * n_states for _ in range(n_tokens)]

    # Initialise t=0: only S, B, O are valid starts
    for s in range(n_states):
        pfx, cat = label_info[s]
        if pfx in ("S", "B", "O"):
            dp[0][s] = _apply_bias(logits[0][s] / temp, pfx)
        # else: NEG_INF (can't start with I or E)

    for t in range(1, n_tokens):
        for s in range(n_states):
            cur_pfx, cur_cat = label_info[s]
            best_prev = NEG_INF
            best_s = -1
            for ps in range(n_states):
                if dp[t - 1][ps] <= NEG_INF:
                    continue
                prev_pfx, prev_cat = label_info[ps]
                if _is_valid_transition(prev_pfx, prev_cat, cur_pfx, cur_cat):
                    v = dp[t - 1][ps]
                    if v > best_prev:
                        best_prev = v
                        best_s = ps
            if best_s >= 0:
                dp[t][s] = best_prev + _apply_bias(logits[t][s] / temp, cur_pfx)
                back[t][s] = best_s

    # Traceback — only valid end states: S, E, O
    best_end = NEG_INF
    best_end_s = 0
    for s in range(n_states):
        pfx, _ = label_info[s]
        if pfx in ("S", "E", "O") and dp[n_tokens - 1][s] > best_end:
            best_end = dp[n_tokens - 1][s]
            best_end_s = s

    # Reconstruct path
    path = [best_end_s]
    for t in range(n_tokens - 1, 0, -1):
        path.append(back[t][path[-1]])
    path.reverse()

    # Extract spans from BIOES path
    spans: List[Tuple[int, int, str, float]] = []
    i = 0
    while i < n_tokens:
        pfx, cat = label_info[path[i]]
        if pfx == "S" and cat:
            score = logits[i][path[i]]
            spans.append((i, i, cat, float(score)))
        elif pfx == "B" and cat:
            j = i + 1
            # Scan forward for matching E (with I in between)
            while j < n_tokens:
                jp, jc = label_info[path[j]]
                if jp == "E" and jc == cat:
                    score = max(logits[k][path[k]] for k in range(i, j + 1))
                    spans.append((i, j, cat, float(score)))
                    i = j
                    break
                elif jp == "I" and jc == cat:
                    j += 1
                else:
                    # Malformed sequence — partial span, skip
                    i = j - 1
                    break
        i += 1

    return spans


# ── Model loading ─────────────────────────────────────────────────────────

def _load_viterbi_bias(model_dir: str,
                       operating_point: str = "default") -> Optional[Dict[str, float]]:
    """Load viterbi_calibration.json and normalise to the flat bias dict used by
    _decode_viterbi.

    Real schema (from openai/privacy-filter):
        {"operating_points": {"default": {"biases": {
            "transition_bias_background_stay":    0.0,  # O→O
            "transition_bias_background_to_start":0.0,  # O→B/S  → begin_bias
            "transition_bias_end_to_background":  0.0,  # E→O    → (no separate key)
            "transition_bias_end_to_start":       0.0,  # E→B/S  → (implicit in allow-list)
            "transition_bias_inside_to_continue": 0.0,  # I→I    → inside_bias
            "transition_bias_inside_to_end":      0.0,  # I→E    → end_bias
        }}}}

    Legacy flat schema (original stub expected):
        {"begin_bias": …, "inside_bias": …, "end_bias": …,
         "single_bias": …, "outside_bias": …, "transition_temp": …}

    Both are normalised to the flat form so _decode_viterbi is unchanged.
    When all biases are 0.0 (the current model default), the result is None
    to avoid allocating a dict of zeros (the caller uses {} as fallback).
    """
    p = Path(model_dir) / "viterbi_calibration.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Detect which schema we have
    if "operating_points" in raw:
        # Real openai/privacy-filter schema
        try:
            biases = (raw["operating_points"]
                         .get(operating_point, raw["operating_points"].get("default", {}))
                         .get("biases", {}))
        except (KeyError, AttributeError):
            return None
        flat: Dict[str, float] = {
            "begin_bias":   float(biases.get("transition_bias_background_to_start", 0.0)),
            "inside_bias":  float(biases.get("transition_bias_inside_to_continue", 0.0)),
            "end_bias":     float(biases.get("transition_bias_inside_to_end", 0.0)),
            # single_bias, outside_bias, transition_temp have no direct counterpart
            # in the real schema — default to 0 / 1.0
            "single_bias":      float(biases.get("transition_bias_background_to_start", 0.0)),
            "outside_bias":     float(biases.get("transition_bias_background_stay", 0.0)),
            "transition_temp":  1.0,
        }
        # If everything is zero, return None (caller treats {} / None identically)
        if all(v == 0.0 for k, v in flat.items() if k != "transition_temp"):
            return None
        return flat

    # Legacy flat schema — pass through directly
    flat_keys = {"begin_bias", "inside_bias", "end_bias",
                 "single_bias", "outside_bias", "transition_temp"}
    if flat_keys & raw.keys():
        return {k: float(v) for k, v in raw.items() if k in flat_keys}

    return None  # unrecognised schema → no bias


def _load_model(model_dir: str, onnx_file: str = DEFAULT_ONNX_FILE) -> Optional[Any]:
    """Lazy-load the ONNX session + tokenizer + label list. Returns None on failure (fail-open)."""
    cache_key = f"{model_dir}::{onnx_file}"
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    # Allow injection of a mock for unit tests
    mock = os.environ.get("BUBBLE_SHIELD_OPENAI_MOCK")
    if mock == "1":
        _MODEL_CACHE[cache_key] = None
        return None

    try:
        import onnxruntime as ort  # noqa
    except ImportError:
        _MODEL_CACHE[cache_key] = None
        return None

    # The openai/privacy-filter ONNX uses com.microsoft contrib ops
    # (GatherBlockQuantized with 'bits' attr, QMoE, MatMulNBits) that were
    # introduced in onnxruntime ≥ 1.27.  Versions ≤ 1.19 raise
    # "GatherBlockQuantized is not a registered function/op"; 1.20 raises
    # "Unrecognized attribute: bits for operator GatherBlockQuantized".
    # Fail-open with a clear log message rather than a cryptic ort error.
    _ORT_MIN = (1, 27)
    try:
        _ort_ver = tuple(int(x) for x in ort.__version__.split(".")[:2])
    except Exception:
        _ort_ver = (0, 0)
    if _ort_ver < _ORT_MIN:
        import sys
        print(
            f"[bubble-shield] openai_pf_ext: onnxruntime {ort.__version__} is too old; "
            f">= 1.27 required for openai/privacy-filter (GatherBlockQuantized + QMoE ops). "
            f"Upgrade with: pip install 'onnxruntime>=1.27'. Failing open (returns []).",
            file=sys.stderr,
        )
        _MODEL_CACHE[cache_key] = None
        return None

    onnx_path = Path(model_dir) / onnx_file
    if not onnx_path.is_file():
        _MODEL_CACHE[cache_key] = None
        return None

    try:
        # onnxruntime loads the external .onnx_data sidecar automatically when
        # it lives in the same directory as the .onnx graph file.
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(str(onnx_path), sess_options=opts,
                                       providers=["CPUExecutionProvider"])
    except Exception:
        _MODEL_CACHE[cache_key] = None
        return None

    # Load tokenizer (HF fast tokenizer with offset_mapping support)
    try:
        from tokenizers import Tokenizer  # noqa
        tok_path = Path(model_dir) / "tokenizer.json"
        tokenizer = Tokenizer.from_file(str(tok_path))
        tokenizer.enable_padding()
        tokenizer.enable_truncation(max_length=512)
    except Exception:
        _MODEL_CACHE[cache_key] = None
        return None

    # Load label list from config.json (id2label)
    try:
        cfg_path = Path(model_dir) / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        id2label: Dict[int, str] = {int(k): v for k, v in cfg.get("id2label", {}).items()}
        labels = [id2label[i] for i in range(len(id2label))]
    except Exception:
        _MODEL_CACHE[cache_key] = None
        return None

    viterbi_bias = _load_viterbi_bias(model_dir)
    _MODEL_CACHE[cache_key] = (session, tokenizer, labels, viterbi_bias)
    return _MODEL_CACHE[cache_key]


# ── Chunking (reused from gliner_ext) ────────────────────────────────────

def _chunks(text: str, size: int, overlap: int):
    """Yield (base_offset, chunk_text) sliding windows. Same as gliner_ext._chunks."""
    if size <= overlap:
        raise ValueError("chunk size must exceed overlap")
    i = 0
    n = len(text)
    while i < n:
        yield i, text[i:i + size]
        if i + size >= n:
            break
        i += size - overlap


# ── Inference ────────────────────────────────────────────────────────────

def _run_chunk(
    session: Any,
    tokenizer: Any,
    labels: List[str],
    viterbi_bias: Optional[Dict[str, float]],
    chunk: str,
    base: int,
    threshold: float,
) -> List[Match]:
    """Run one chunk through the ONNX model and return Matches with absolute offsets."""
    import numpy as np  # noqa — available in the ML venv

    encoding = tokenizer.encode(chunk)
    input_ids = np.array([encoding.ids], dtype=np.int64)
    attention_mask = np.array([encoding.attention_mask], dtype=np.int64)

    # Model may expect token_type_ids as well
    inputs: Dict[str, Any] = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }
    input_names = {inp.name for inp in session.get_inputs()}
    if "token_type_ids" in input_names:
        inputs["token_type_ids"] = np.zeros_like(input_ids)

    try:
        outputs = session.run(None, inputs)
    except Exception:
        return []

    # outputs[0] shape: [1, seq_len, num_labels] — raw logits
    logits_3d = outputs[0]
    logits_2d = logits_3d[0].tolist()  # [seq_len, num_labels]

    # Decode with constrained Viterbi
    token_spans = _decode_viterbi(logits_2d, labels, viterbi_bias)

    # Map token offsets → char offsets via offset_mapping
    offsets = encoding.offsets  # list of (char_start, char_end) per token

    matches: List[Match] = []
    for tok_start, tok_end, cat, score in token_spans:
        etype = OPENAI_LABEL_TO_TYPE.get(cat)
        if not etype:
            continue  # account_number, or unknown → skip; regex core handles
        if score < threshold:
            continue
        # Guard index bounds
        if tok_start >= len(offsets) or tok_end >= len(offsets):
            continue
        char_start = offsets[tok_start][0]
        char_end = offsets[tok_end][1]
        if char_end <= char_start:
            continue
        span_text = chunk[char_start:char_end]
        abs_start = base + char_start
        abs_end = base + char_end
        matches.append(Match(
            start=abs_start, end=abs_end,
            entity_type=etype, value=span_text,
            score=float(score), priority=5,
        ))
    return matches


# ── Public API ────────────────────────────────────────────────────────────

def openai_pf_matches(
    text: str,
    *,
    model_dir: str = DEFAULT_MODEL_DIR,
    onnx_file: str = DEFAULT_ONNX_FILE,
    chunk_size: int = DEFAULT_CHUNK,
    overlap: int = DEFAULT_OVERLAP,
    threshold: float = DEFAULT_THRESHOLD,
    viterbi_bias: Optional[Dict[str, float]] = None,
) -> List[Match]:
    """Run chunked OpenAI Privacy Filter over `text`. Returns bubble_shield Matches.

    Recall-first union across overlapping windows. Fail-open: returns [] if
    the model isn't available (so the engine keeps all regex-core matches).
    Priority=5 (same as GLiNER) — below the regex/checksum core which wins
    on any overlapping span (IBAN mod-97 etc. keep score=1.0, priority=0-100).
    """
    packed = _load_model(model_dir, onnx_file)
    if packed is None:
        return []
    session, tokenizer, labels, repo_bias = packed
    effective_bias = viterbi_bias if viterbi_bias is not None else repo_bias

    # key = (entity_type, span_text) → (best_score, abs_start, abs_end)
    best: Dict[tuple, tuple] = {}
    for base, chunk in _chunks(text, chunk_size, overlap):
        try:
            chunk_matches = _run_chunk(
                session, tokenizer, labels, effective_bias,
                chunk, base, threshold)
        except Exception:
            continue
        for m in chunk_matches:
            key = (m.entity_type, m.value)
            if key not in best or m.score > best[key][0]:
                best[key] = (m.score, m.start, m.end)

    return [
        Match(start=s, end=e, entity_type=et, value=v, score=sc, priority=5)
        for (et, v), (sc, s, e) in best.items()
    ]


def make_openai_detector(**cfg):
    """Return a Callable[[str], List[Match]] with config baked in.
    Mirrors make_gliner_detector for use with AnonymizationEngine(extra_detectors=[...])."""
    def _detector(text: str) -> List[Match]:
        return openai_pf_matches(text, **cfg)
    return _detector
