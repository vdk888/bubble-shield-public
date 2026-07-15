"""
extract.py — turn an uploaded file into plain text to anonymise.

Plain-text formats (.txt/.md/.csv/.json) are decoded directly. PDFs go through
pypdf for text extraction. Anything that can't yield text (encrypted PDF, a
scanned/image-only PDF) raises ExtractionError with a human message instead of
silently feeding garbage into the anonymiser — which would be worse than
failing, because garbage in means PII could slip through unrecognised.

For scanned PDFs, the optional OCR pack (bubble_shield_setup_ocr) is tried if
installed. Fail-open: OCR errors fall through to the original ExtractionError.

pypdf is resolved in this order:
  1. system pypdf (installed in the active venv / site-packages)
  2. vendored copy at plugin/bubble-shield/vendor/pypdf  (shipped with the plugin)
Either path exposes the same PdfReader API; we just make import resolution
robust so the webapp works whether or not pypdf is installed system-wide.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# ── Ensure vendored pypdf is importable ───────────────────────────────────────
# Try system import first; if it fails, prepend the vendor path to sys.path.
try:
    import pypdf as _pypdf_check  # noqa: F401 — probe only
except ModuleNotFoundError:
    _vendor_dir = (
        Path(__file__).resolve().parent.parent   # repo root
        / "plugin" / "bubble-shield" / "vendor"
    )
    if _vendor_dir.is_dir() and str(_vendor_dir) not in sys.path:
        sys.path.insert(0, str(_vendor_dir))
    # If it still can't be found the ImportError will surface at extract_pdf_text
    # call time with a clear message (not a silent failure).

PDF_MAGIC = b"%PDF"

_OCR_TAG = "[OCR]"  # prepended to signal OCR-sourced text to callers

# #574 — strip the ANSSI 2D-DOC barcode block (a covert PII channel on FR tax notices)
# from extracted text before tokenization. Mirrors bubble_shield_extract.py — keep the
# two in sync. Amounts in the visible text are untouched (#547). Best-effort.
import re as _re
_2DDOC_MARKER = "⟦2DDOC_BARCODE_STRIPPED⟧"
_2DDOC_RE = _re.compile(r"\bDC[0-9A-Z]{2}[0-9A-Z]{4}[0-9A-Z]{40,}")


def strip_2ddoc_barcodes(text: str) -> str:
    """Replace any 2D-DOC barcode block with the marker. Deterministic, idempotent,
    best-effort (a strip error returns the original text, never loses the doc)."""
    try:
        return _2DDOC_RE.sub(_2DDOC_MARKER, text or "")
    except Exception:
        return text or ""

# Image formats routed through OCR (#338). Mirrors bubble_shield_extract.py.
# Detection by magic bytes OR extension so a mislabelled image still gets caught
# (it must NOT fall through to the UTF-8 decoder — that returns a multi-MB binary
# blob as "text" and crashes/bloats the response).
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
_IMAGE_MAGICS = (
    b"\x89PNG\r\n\x1a\n",        # PNG
    b"\xff\xd8\xff",            # JPEG
    b"BM",                       # BMP
    b"GIF87a", b"GIF89a",       # GIF
    b"II*\x00", b"MM\x00*",     # TIFF (little / big endian)
)
_IMAGE_MAX_LONG_EDGE = 4000      # px — downscale anything longer than this
_IMAGE_HARD_MAX_PIXELS = 50_000_000  # 50 MP — refuse outright


class ExtractionError(Exception):
    """Raised when an uploaded file can't be turned into usable text."""


def looks_like_pdf(filename: str, raw: bytes) -> bool:
    return raw[:5].startswith(PDF_MAGIC) or filename.lower().endswith(".pdf")


def looks_like_image(filename: str, raw: bytes) -> bool:
    """True if the bytes/extension look like a raster image we should OCR (#338)."""
    head = raw[:16]
    if any(head.startswith(m) for m in _IMAGE_MAGICS):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return (filename or "").lower().endswith(_IMAGE_EXTS)


def _ocr_pack_python() -> "Path | None":
    """Return the ocr-env venv python if the OCR pack is installed, else None."""
    import os
    from pathlib import Path
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
    manifest = home / "ocr.json"
    if not manifest.is_file():
        return None
    try:
        import json
        data = json.loads(manifest.read_text(encoding="utf-8"))
        py = Path(data.get("venv_python", ""))
        return py if py.is_file() else None
    except Exception:
        return None


def _ocr_pdf_if_pack_present(raw: bytes) -> "str | None":
    """Try OCR on a scanned PDF using the optional OCR pack.

    Returns the OCR'd text (prefixed with _OCR_TAG) if successful, None otherwise.
    Layout-aware: docling preserves label:value structure from KYC/form PDFs.
    Called only when pypdf finds no text layer. Completely local — no cloud."""
    py = _ocr_pack_python()
    if py is None:
        return None
    import subprocess
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(raw)
        tmp_pdf = tf.name
    try:
        code = (
            "import sys, warnings; warnings.filterwarnings('ignore');"
            "from docling.document_converter import DocumentConverter, PdfFormatOption;"
            "from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions;"
            "opts = PdfPipelineOptions();"
            "opts.do_ocr = True;"
            "opts.ocr_options = RapidOcrOptions();"
            "opts.do_table_structure = True;"
            f"conv = DocumentConverter(format_options={{'pdf': PdfFormatOption(pipeline_options=opts)}});"
            f"res = conv.convert({tmp_pdf!r});"
            "print(res.document.export_to_markdown(), end='')"
        )
        r = subprocess.run([str(py), "-c", code],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and r.stdout.strip():
            return _OCR_TAG + " " + r.stdout.strip()
        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_pdf)
        except Exception:
            pass


def _ocr_image_if_pack_present(raw: bytes, suffix: str) -> "str | None":
    """OCR a raw image (.png/.jpg/...) via docling's IMAGE pipeline (#338).

    Mirrors _ocr_pdf_if_pack_present but routes through InputFormat.IMAGE with an
    ImageFormatOption (same RapidOCR engine). Downscale runs INSIDE the ocr-env
    subprocess (Pillow guaranteed there): a large image is shrunk to
    <= _IMAGE_MAX_LONG_EDGE on its long edge before OCR so a 7.6 MP screenshot
    can't blow memory/time. An image over _IMAGE_HARD_MAX_PIXELS is refused there
    (subprocess exits non-zero) so we fail closed, not crash."""
    py = _ocr_pack_python()
    if py is None:
        return None
    import subprocess
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=suffix or ".png", delete=False) as tf:
        tf.write(raw)
        tmp_img = tf.name
    try:
        code = (
            "import sys, warnings; warnings.filterwarnings('ignore');"
            "from PIL import Image;"
            f"MAX={_IMAGE_MAX_LONG_EDGE}; HARD={_IMAGE_HARD_MAX_PIXELS};"
            f"path={tmp_img!r};"
            "im=Image.open(path); im.load();"
            "sys.exit(7) if (im.width*im.height) > HARD else None;"
            "longest=max(im.size);"
            "im2=(im.resize((max(1,round(im.width*MAX/longest)), max(1,round(im.height*MAX/longest)))) if longest>MAX else im);"
            "im2=im2.convert('RGB');"
            "im2.save(path, format='PNG');"
            "from docling.document_converter import DocumentConverter, ImageFormatOption;"
            "from docling.datamodel.base_models import InputFormat;"
            "from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions;"
            "opts = PdfPipelineOptions();"
            "opts.do_ocr = True;"
            "opts.ocr_options = RapidOcrOptions();"
            "opts.do_table_structure = True;"
            "conv = DocumentConverter(allowed_formats=[InputFormat.IMAGE], format_options={InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts)});"
            "res = conv.convert(path);"
            "print(res.document.export_to_markdown(), end='')"
        )
        r = subprocess.run([str(py), "-c", code],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and r.stdout.strip():
            return _OCR_TAG + " " + r.stdout.strip()
        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_img)
        except Exception:
            pass


def extract_image_text(raw: bytes, filename: str) -> str:
    """OCR a raw image file (.png/.jpg/...) → text, or raise ExtractionError.

    FAIL-CLOSED (#338): an image that can't be OCR'd raises ExtractionError with a
    clear FR message — NEVER a UTF-8-decoded binary blob, NEVER an unhandled crash."""
    ext = ""
    if filename:
        dot = filename.rfind(".")
        if dot != -1:
            ext = filename[dot:].lower()
    if ext not in _IMAGE_EXTS:
        ext = ".png"
    try:
        text = _ocr_image_if_pack_present(raw, ext)
    except Exception:
        text = None
    if text:
        return text
    raise ExtractionError(
        "OCR image indisponible — document non vérifié. "
        "Image illisible, trop volumineuse, ou pack OCR (bubble_shield_setup_ocr) absent. "
        "Réduisez l'image ou collez le texte manuellement.")


def extract_pdf_text(raw: bytes) -> str:
    """Extract text from a PDF, or raise ExtractionError with a clear reason.

    For PDFs with a native text layer, uses pypdf (zero extra install). For
    scanned/image-only PDFs (no text layer), falls back to the optional OCR pack
    if installed (docling + RapidOCR, provisioned by bubble_shield_setup_ocr).
    Fail-open: OCR errors fall through to the original ExtractionError message."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:                       # malformed / not really a PDF
        raise ExtractionError(f"PDF illisible : {exc}") from exc

    if reader.is_encrypted:
        try:
            # many "protected" PDFs use an empty owner password
            if reader.decrypt("") == 0:
                raise ExtractionError("PDF chiffré : mot de passe requis.")
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("PDF chiffré : déchiffrement impossible.") from exc

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n".join(parts).strip()

    if not text:
        # OCR pack: if installed, try layout-aware local OCR on the scanned pages.
        # Fail-open: any OCR error falls through to the original message.
        try:
            _ocr_text = _ocr_pdf_if_pack_present(raw)
        except Exception:
            _ocr_text = None
        if _ocr_text:
            return _ocr_text
        raise ExtractionError(
            "Aucun texte extractible — PDF probablement scanné (image). "
            "Installez le pack OCR (bubble_shield_setup_ocr) pour lire ce type de fichier, "
            "ou collez le texte manuellement.")
    return text


def extract_text(filename: str, raw: bytes) -> str:
    """Dispatch on file type. PDF → pypdf, image → OCR, everything else → UTF-8.

    The image branch (#338) runs BEFORE the UTF-8 fallback: a raw .png/.jpg must
    never be UTF-8-decoded (that yields a multi-MB binary blob as "text" and
    crashes the server). Images fail CLOSED via extract_image_text."""
    if not raw:
        return ""
    if looks_like_pdf(filename or "", raw):
        text = extract_pdf_text(raw)
    elif looks_like_image(filename or "", raw):
        text = extract_image_text(raw, filename or "")
    else:
        text = raw.decode("utf-8", errors="replace")
    return strip_2ddoc_barcodes(text)  # #574 — strip 2D-DOC on every branch
