"""
test_vision_ocr_626.py — #626: Vision-first, docling-rescue OCR path.

Vision (Swift helper, ~1s) is tried first for a scanned PDF; on ANY failure
(binary absent, non-zero exit, timeout, empty output) the EXISTING docling/
RapidOCR path runs unchanged. Fail-closed preserved (both fail → None).

Synthetic only. Stubs the two OCR backends so no real model/ANE is needed.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
import bubble_shield_extract as ext


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BUBBLE_SHIELD_DISABLE_VISION_OCR", raising=False)
    yield


def _fake_pdf() -> bytes:
    return b"%PDF-1.4 fake scanned pdf bytes"


# ── _vision_ocr_binary resolution ────────────────────────────────────────────

def test_vision_binary_none_when_sentinel_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    assert ext._vision_ocr_binary() is None


def test_vision_binary_found_when_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    (tmp_path / "vision_ocr.flag").write_text("ready")
    binp = tmp_path / "visionocr"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    monkeypatch.setattr(ext, "_vision_ocr_binary_path", lambda home: binp)
    assert ext._vision_ocr_binary() == binp


# ── Vision-first / docling-rescue routing ────────────────────────────────────

def test_vision_success_returns_tagged_and_skips_docling(monkeypatch):
    monkeypatch.setattr(ext, "_ocr_pdf_via_vision",
                        lambda raw: "IBAN FR76… nom Dupont")
    called = {"docling": False}
    def _docling(raw):
        called["docling"] = True
        return "docling text"
    monkeypatch.setattr(ext, "_ocr_pdf_via_docling", _docling)
    out = ext._ocr_pdf_if_pack_present(_fake_pdf())
    assert out is not None and out.startswith(ext._OCR_TAG)
    assert "Dupont" in out
    assert called["docling"] is False   # Vision won → docling never called


def test_vision_empty_falls_back_to_docling(monkeypatch):
    monkeypatch.setattr(ext, "_ocr_pdf_via_vision", lambda raw: None)
    monkeypatch.setattr(ext, "_ocr_pdf_via_docling",
                        lambda raw: ext._OCR_TAG + " docling rescued")
    out = ext._ocr_pdf_if_pack_present(_fake_pdf())
    assert out == ext._OCR_TAG + " docling rescued"


def test_vision_error_falls_back_to_docling(monkeypatch):
    def _boom(raw):
        raise RuntimeError("vision crashed")
    monkeypatch.setattr(ext, "_ocr_pdf_via_vision", _boom)
    monkeypatch.setattr(ext, "_ocr_pdf_via_docling",
                        lambda raw: ext._OCR_TAG + " docling rescued")
    out = ext._ocr_pdf_if_pack_present(_fake_pdf())
    assert out == ext._OCR_TAG + " docling rescued"


def test_both_fail_returns_none_failclosed(monkeypatch):
    monkeypatch.setattr(ext, "_ocr_pdf_via_vision", lambda raw: None)
    monkeypatch.setattr(ext, "_ocr_pdf_via_docling", lambda raw: None)
    assert ext._ocr_pdf_if_pack_present(_fake_pdf()) is None


def test_disable_env_forces_docling(monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_DISABLE_VISION_OCR", "1")
    vision_called = {"v": False}
    def _v(raw):
        vision_called["v"] = True
        return "vision text"
    monkeypatch.setattr(ext, "_ocr_pdf_via_vision", _v)
    monkeypatch.setattr(ext, "_ocr_pdf_via_docling",
                        lambda raw: ext._OCR_TAG + " docling")
    out = ext._ocr_pdf_if_pack_present(_fake_pdf())
    assert out == ext._OCR_TAG + " docling"
    assert vision_called["v"] is False   # disabled → Vision never invoked


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
