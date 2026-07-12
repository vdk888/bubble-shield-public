"""
test_348_union_default.py — Task 5 (#348): default detector mode = GLiNER∪OpenAI-PF
union, with fail-open to gliner-only when the OpenAI-PF weights are absent.

The real resolver is `bubble_shield_nerd._load_detector_mode(cfg_path: Path)`
(NOT the plan's hypothetical `_resolve_detector_mode({})` — adapted to source).
Default flips from "gliner" to "both".

Fail-open is exercised via the new `_resolve_runtime_mode(mode, man)` seam, which
decides the EFFECTIVE runtime mode without loading any heavy model: if the
configured mode is "both" but the OpenAI-PF weights cannot be made available, it
degrades to "gliner" (union always degrades to the GLiNER core, never crashes).

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow importing the daemon script + vendor modules (mirror existing daemon tests).
_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
_VENDOR = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "vendor"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_VENDOR))

import bubble_shield_nerd as nerd  # noqa: E402


# --- default mode is now "both" (the union) ---------------------------------

def test_default_detector_mode_is_both_when_no_config(tmp_path):
    """No custom_fields.json on disk → default must be 'both' (was 'gliner')."""
    missing = tmp_path / "does_not_exist.json"
    assert nerd._load_detector_mode(missing) == "both"


def test_default_detector_mode_is_both_when_key_absent(tmp_path):
    """custom_fields.json present but no detector.mode key → default 'both'."""
    cfg = tmp_path / "custom_fields.json"
    cfg.write_text('{"detector": {}}', encoding="utf-8")
    assert nerd._load_detector_mode(cfg) == "both"


def test_default_detector_mode_is_both_when_corrupt(tmp_path):
    """Corrupt config → still defaults to 'both' (the configured union default)."""
    cfg = tmp_path / "custom_fields.json"
    cfg.write_text("{ broken json", encoding="utf-8")
    assert nerd._load_detector_mode(cfg) == "both"


def test_explicit_mode_still_honoured(tmp_path):
    """An explicit detector.mode in config still wins over the default."""
    cfg = tmp_path / "custom_fields.json"
    cfg.write_text('{"detector": {"mode": "gliner"}}', encoding="utf-8")
    assert nerd._load_detector_mode(cfg) == "gliner"
    cfg.write_text('{"detector": {"mode": "openai"}}', encoding="utf-8")
    assert nerd._load_detector_mode(cfg) == "openai"


# --- fail-open: mode=both with no OpenAI-PF weights degrades to gliner -------

def test_runtime_mode_both_with_weights_present_stays_both(tmp_path):
    """OpenAI-PF weights present on disk → runtime mode stays 'both'."""
    openai_dir = tmp_path / "openai__privacy-filter"
    openai_dir.mkdir(parents=True)
    (openai_dir / "model_q4.onnx").write_text("x", encoding="utf-8")
    man = {"models": {"openai": {"model_dir": str(openai_dir),
                                 "onnx_file": "model_q4.onnx"}}}
    assert nerd._resolve_runtime_mode("both", man) == "both"


def test_runtime_mode_both_no_manifest_openai_falls_back_to_gliner(tmp_path):
    """mode=both but the manifest has no OpenAI block and fetch fails →
    degrade to 'gliner' (fail-open), NEVER crash."""
    man = {"models": {"gliner": {"model_dir": str(tmp_path)}}}  # no 'openai' key

    # Stub the fetch so the test never downloads ~900MB. Returns False = couldn't fetch.
    def _no_fetch(_man):
        return False

    assert nerd._resolve_runtime_mode("both", man, _fetch=_no_fetch) == "gliner"


def test_runtime_mode_both_missing_weights_fetch_succeeds_stays_both(tmp_path):
    """mode=both, weights absent, but the on-demand fetch reports success →
    runtime mode stays 'both'."""
    man = {"models": {"openai": {"model_dir": str(tmp_path / "absent"),
                                 "onnx_file": "model_q4.onnx"}}}

    def _ok_fetch(_man):
        return True

    assert nerd._resolve_runtime_mode("both", man, _fetch=_ok_fetch) == "both"


def test_runtime_mode_gliner_is_passthrough(tmp_path):
    """mode=gliner needs no OpenAI weights → unchanged, no fetch attempted."""
    called = {"n": 0}

    def _spy(_man):
        called["n"] += 1
        return False

    assert nerd._resolve_runtime_mode("gliner", {}, _fetch=_spy) == "gliner"
    assert called["n"] == 0  # gliner path must not touch the OpenAI fetch


def test_runtime_mode_fetch_exception_fails_open(tmp_path):
    """If the fetch raises, _resolve_runtime_mode must NOT propagate — it
    degrades to gliner (the union never crashes detection)."""
    man = {"models": {"openai": {"model_dir": str(tmp_path / "absent"),
                                 "onnx_file": "model_q4.onnx"}}}

    def _boom(_man):
        raise RuntimeError("network down")

    assert nerd._resolve_runtime_mode("both", man, _fetch=_boom) == "gliner"
