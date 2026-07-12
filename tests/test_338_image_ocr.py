#!/usr/bin/env python3
"""test_338_image_ocr.py — raw image (.png/.jpg) OCR path + fail-closed (#338).

THE BUG (LIVE in Cowork): bubble_shield_read on a raw .png (3420x2214, 7.6 MP RGBA)
CRASHED the MCP server ('MCP error -32000: Connection closed'). Root cause:
extract_text() had no image branch, so a .png fell through to
raw.decode('utf-8', errors='replace') → a multi-MB binary blob returned as "text"
→ bloated/crashed the MCP response.

THE FIX: an IMAGE branch routes raster images through docling's IMAGE OCR pipeline
(same RapidOCR engine as scanned PDFs), with a downscale cap, and FAILS CLOSED with
'OCR image indisponible — document non vérifié' instead of crashing.

Tests (all PII synthetic):
  1. detection: looks_like_image by magic bytes AND by extension; .txt is NOT an image
  2. NO BINARY BLOB: a .png never returns UTF-8-decoded binary as text
     (with pack absent → ExtractionError, not a giant string)
  3. fail-closed message: pack-absent image → ExtractionError carries the FR message
  4. [pack present] synthetic PNG with known text → OCR'd text returned (not a blob)
  5. [pack present] pathological huge image → downscales+OCRs OR fails closed,
     NEVER an unhandled crash / never a giant binary string
  6. .txt still returns its text (no regression)

Tests 4 & 5 need the REAL ocr-env (~/.bubble_shield/ocr-env); they point
BUBBLE_SHIELD_HOME at the real store (read-only: OCR reads ocr.json, writes nothing)
and SKIP if the pack isn't installed. Everything else runs with no OCR pack.

Run standalone: .venv312/bin/python -W ignore -m pytest tests/test_338_image_ocr.py -q
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

# Import the extract module the MCP daemon actually uses.
_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "plugin" / "bubble-shield" / "scripts"
_VENDOR = _REPO / "plugin" / "bubble-shield" / "vendor"
for _p in (_VENDOR, _SCRIPTS):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bubble_shield_extract as bx  # noqa: E402

_REAL_HOME = Path.home() / ".bubble_shield"
_REAL_OCR_MANIFEST = _REAL_HOME / "ocr.json"
_REAL_OCR_SENTINEL = _REAL_HOME / "layout_model_cached.flag"
_PACK_PRESENT = _REAL_OCR_MANIFEST.is_file() and _REAL_OCR_SENTINEL.is_file()

# The test venv (.venv312) has NO Pillow — it lives only in the ocr-env. So we
# render synthetic text PNGs by shelling to the ocr-env python (the SAME engine
# the fix uses). Detection/blob tests use a hand-built minimal PNG (no Pillow).
_OCR_PY = None
if _PACK_PRESENT:
    import json as _json
    try:
        _OCR_PY = _json.loads(_REAL_OCR_MANIFEST.read_text())["venv_python"]
    except Exception:
        _OCR_PY = None


def _minimal_png(width=8, height=8) -> bytes:
    """A valid 8-bit RGB PNG built with stdlib zlib only (no Pillow).

    Enough to exercise magic-byte detection and the 'never a binary blob' path —
    its pixels are irrelevant (OCR yields nothing, which is the pack-absent case)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def _png_with_text(text_lines, size=(1000, 400)) -> bytes:
    """Render known text onto a white PNG via the ocr-env Pillow (synthetic PII)."""
    import subprocess
    import tempfile
    assert _OCR_PY, "ocr-env python required to render text PNG"
    w, h = size
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out_path = tf.name
    code = (
        "from PIL import Image, ImageDraw, ImageFont;"
        f"lines={text_lines!r}; W,H={w},{h};"
        "img=Image.new('RGB',(W,H),'white'); d=ImageDraw.Draw(img);"
        "import os;"
        "f='/System/Library/Fonts/Supplemental/Arial.ttf';"
        "font=ImageFont.truetype(f,36) if os.path.exists(f) else ImageFont.load_default();"
        "y=30\n"
        "for ln in lines:\n"
        "    d.text((30,y),ln,fill='black',font=font); y+=70\n"
        f"img.save({out_path!r},format='PNG')"
    )
    subprocess.run([_OCR_PY, "-c", code], check=True, capture_output=True, text=True)
    data = Path(out_path).read_bytes()
    Path(out_path).unlink(missing_ok=True)
    return data


@pytest.fixture
def real_ocr_home(monkeypatch):
    """Point BUBBLE_SHIELD_HOME at the real store so the real ocr-env is found.

    Read-only w.r.t. the store: the OCR path only reads ocr.json + the sentinel
    and shells to the ocr-env; it never writes the store. Overrides the autouse
    tmp-home fixture for the few tests that genuinely need the real pack."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(_REAL_HOME))
    yield


# ── 1. detection ──────────────────────────────────────────────────────────────
def test_looks_like_image_by_magic_and_extension():
    png = _minimal_png()
    assert bx.looks_like_image("whatever.dat", png)          # magic bytes win
    assert bx.looks_like_image("photo.PNG", b"not really")   # extension fallback
    assert bx.looks_like_image("scan.jpeg", b"")             # extension only
    assert not bx.looks_like_image("notes.txt", b"hello world")
    assert not bx.looks_like_image("doc.pdf", b"%PDF-1.7")


# ── 2 & 3. NO binary blob, fail-closed message (pack ABSENT) ──────────────────
def test_image_never_returns_binary_blob_pack_absent(monkeypatch, tmp_path):
    """With NO OCR pack, a real PNG must raise ExtractionError — NOT return the
    UTF-8-decoded image binary as a multi-MB "text" string (the #338 crash)."""
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(empty_home))  # no ocr.json here
    png = _minimal_png(64, 64)  # real PNG bytes; pack-absent → must raise, not blob
    with pytest.raises(bx.ExtractionError) as ei:
        bx.extract_text("screenshot.png", png)
    msg = str(ei.value)
    assert "OCR image indisponible" in msg
    assert "non vérifié" in msg  # fail-closed verdict, not a silent empty=safe pass


# ── 4. pack PRESENT: synthetic PNG → OCR'd text ───────────────────────────────
@pytest.mark.skipif(not _PACK_PRESENT, reason="real OCR pack not installed")
def test_png_ocr_returns_text(real_ocr_home):
    png = _png_with_text([
        "Client Testname Surname",
        "IBAN FR76 3000 4000 5000",
        "Telephone 06 12 34 56 78",
    ])
    out = bx.extract_text("client_scan.png", png)
    assert isinstance(out, str)
    # It's OCR'd text, not a binary blob: the OCR tag is present and known
    # tokens come back. (OCR may vary on spacing/case — match robustly.)
    assert bx._OCR_TAG in out
    low = out.lower()
    assert "testname" in low or "surname" in low
    assert "fr76" in low or "iban" in low
    # Sanity: not a giant binary dump.
    assert len(out) < 5000


# ── 5. pack PRESENT: pathological huge image → downscale-or-fail-closed ────────
@pytest.mark.skipif(not _PACK_PRESENT, reason="real OCR pack not installed")
def test_huge_image_downscales_or_fails_closed_never_crashes(real_ocr_home):
    """A large image must NOT crash and must NOT return a binary blob: either it
    downscales + OCRs (returns text) or it fails closed (ExtractionError)."""
    big = _png_with_text(
        ["Grand document scanne", "Nom Testclient", "Reference 999888777"],
        size=(6000, 3500),  # 21 MP — over the 4000px long-edge downscale cap
    )
    try:
        out = bx.extract_text("huge_scan.png", big)
        # downscaled + OCR'd → real text, not a binary blob
        assert isinstance(out, str)
        assert bx._OCR_TAG in out
        assert len(out) < 20000
    except bx.ExtractionError as e:
        # acceptable alternative: clean fail-closed verdict
        assert "OCR image indisponible" in str(e)


# ── 6. .txt regression: still returns its text ────────────────────────────────
def test_txt_still_returns_text():
    raw = "Bonjour Testname, votre IBAN FR76 1234.".encode("utf-8")
    out = bx.extract_text("note.txt", raw)
    assert out == "Bonjour Testname, votre IBAN FR76 1234."


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
