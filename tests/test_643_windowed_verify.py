"""
tests/test_643_windowed_verify.py — the structured-form verify covers the WHOLE doc.

#643 (2026-07-15): the Gemma second-pass truncated to text[:6000] — on a ~35k liasse
the verify (whose job is to catch PII the fast pass MISSED anywhere on the form) was
blind to ~83% of it. Fix: extract_pii WINDOWS the whole doc; the client HTTP timeout
SCALES with window count (a fixed timeout would time out a multi-window call); and a
form too large to verify in budget is size-capped → StructuredFormTooLargeError so the
sweep QUARANTINES it (#646) instead of grinding minutes / retrying forever.

Gemma can't be parallelized (measured 2026-07-15), so the cost is genuinely N sequential
calls — the timeout scaling + size cap reflect that reality.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MCP = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/bubble_shield_mcp.py"
_spec = importlib.util.spec_from_file_location("bsmcp_643", _MCP)
bsmcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsmcp)

_GC = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/gemma_classifier.py"


class _FakeVault:
    def __init__(self): self.n = 0
    def token_for(self, value, entity_type):
        self.n += 1
        return f"⟦{entity_type}_{self.n:04d}⟧"


class _Res:
    def __init__(self, anonymized, entity_count=5, has_residual=False):
        self.original = anonymized
        self.anonymized = anonymized
        self.entity_count = entity_count
        self.has_residual = has_residual


class _Engine:
    def __init__(self): self.vault = _FakeVault()


# ── window count + timeout scaling ────────────────────────────────────────────

def test_window_count_covers_whole_doc():
    # 35k → 7 windows (was 1 truncated window seeing 6k).
    assert bsmcp._extract_window_count(35000) == 7
    assert bsmcp._extract_window_count(3000) == 1


def test_client_windows_whole_doc_into_short_requests(monkeypatch):
    """#643 (client-side): _gemma_extract_call POSTs ONE short request PER WINDOW
    (mirrors daemon_classify), so a 35k doc → 7 short requests, each well under the
    daemon's 90s REQ_TIMEOUT_EXTRACT. It unions the spans. A value only present in a
    LATE window (past the old 6000 truncation) is still found."""
    calls = []

    def fake_window(chunk):
        calls.append(chunk)
        # emit a span only for the window containing the planted late value
        return [{"type": "NOM", "text": "LATE_NAME"}] if "LATE_NAME" in chunk else []

    monkeypatch.setattr(bsmcp, "_gemma_extract_one_window", fake_window)
    text = ("bla " * 7500) + " LATE_NAME " + ("bla " * 1000)  # LATE_NAME at ~char 30000
    spans = bsmcp._gemma_extract_call(text)
    assert len(calls) == bsmcp._extract_window_count(len(text)) >= 7, \
        "must POST one short request per window across the whole doc"
    assert any(s["text"] == "LATE_NAME" for s in spans), \
        "a value only in a LATE window (past the old 6000 cut) must be extracted"


def test_window_request_error_fails_closed(monkeypatch):
    """A per-window request error must RE-RAISE (caller fails closed) — a dropped
    window is a hole in the verify, which on a form must never be silently accepted."""
    def boom(chunk):
        raise ConnectionError("daemon down")
    monkeypatch.setattr(bsmcp, "_gemma_extract_one_window", boom)
    with pytest.raises(Exception):
        bsmcp._gemma_extract_call("x" * 10000)


# ── size cap → quarantine ─────────────────────────────────────────────────────

def test_giant_form_is_size_capped_to_quarantine(monkeypatch):
    """A form whose window count exceeds the cap raises StructuredFormTooLargeError —
    WITHOUT calling Gemma at all (no multi-minute grind)."""
    called = {"gemma": False}
    monkeypatch.setattr(bsmcp, "_gemma_extract_call",
                        lambda t: called.__setitem__("gemma", True) or [])
    big = "2033-A " * 20000  # way over _GEMMA_VERIFY_MAX_WINDOWS windows
    assert bsmcp._extract_window_count(len(big)) > bsmcp._GEMMA_VERIFY_MAX_WINDOWS
    res = _Res(big, entity_count=10)
    with pytest.raises(bsmcp.StructuredFormTooLargeError):
        bsmcp._gemma_second_pass(res, _Engine())
    assert called["gemma"] is False, "a size-capped form must NOT call Gemma (no grind)"


def test_too_large_still_failcloses_everywhere():
    """StructuredFormTooLargeError must be a subclass of StructuredFormUnverifiedError
    so every existing `except StructuredFormUnverifiedError` fail-closes on it (no leak,
    no body) — the size cap must never open a leak."""
    assert issubclass(bsmcp.StructuredFormTooLargeError, bsmcp.StructuredFormUnverifiedError)
    try:
        raise bsmcp.StructuredFormTooLargeError("x")
    except bsmcp.StructuredFormUnverifiedError:
        pass  # caught by the base handler → fail-closed path intact
    else:
        pytest.fail("TooLarge not caught by the Unverified handler → would leak")


def test_normal_liasse_still_verifies_full_doc(monkeypatch):
    """A normal (~35k, 7-window) liasse is NOT size-capped: it calls Gemma and, with
    the fast pass having masked real PII, certifies (verified-clean per #589-D)."""
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda t: [])  # Gemma: nothing to add
    body = "⟦NOM_0001⟧ ⟦SIRET_0002⟧ " + ("mot " * 8000)  # ~35k, entity_count>0
    assert bsmcp._extract_window_count(len(body)) <= bsmcp._GEMMA_VERIFY_MAX_WINDOWS
    res = _Res(body, entity_count=7, has_residual=False)
    out = bsmcp._gemma_second_pass(res, _Engine())
    assert out.startswith("⟦NOM_0001⟧"), "a normal form verifies + returns the masked body"


# ── the daemon-side extract_pii processes ONE window (windowing is client-side) ──

def test_extract_pii_processes_one_window_no_multi_loop(monkeypatch):
    """gemma_classifier.extract_pii is now ONE generate on the window it's handed
    (the WINDOWING moved client-side to _gemma_extract_call, so each daemon request
    stays short and never trips REQ_TIMEOUT_EXTRACT=90s). Assert it calls generate
    exactly ONCE and returns that window's spans — no internal multi-window loop."""
    gspec = importlib.util.spec_from_file_location("gc_643b", _GC)
    gc = importlib.util.module_from_spec(gspec)
    import sys, types
    fake_mlx = types.ModuleType("mlx_lm")
    calls = {"n": 0}

    def fake_generate(model, tok, prompt=None, max_tokens=None, verbose=False):
        calls["n"] += 1
        return "NOM: DUPONT"
    fake_mlx.generate = fake_generate
    fake_mlx.load = lambda *a, **k: (object(), object())
    sys.modules["mlx_lm"] = fake_mlx
    gspec.loader.exec_module(gc)

    clf = gc.GemmaClassifier.__new__(gc.GemmaClassifier)
    clf._model = object(); clf._tok = object()
    spans = gc.GemmaClassifier.extract_pii(clf, "x" * 50000)  # a big input
    assert calls["n"] == 1, "extract_pii must do ONE generate (one window), not loop"
    assert any(s["text"] == "DUPONT" for s in spans)
    sys.modules.pop("mlx_lm", None)
