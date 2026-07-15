#!/usr/bin/env python3
"""bubble_shield_extract.py — turn a client file into plain text to anonymise.

Bundled, self-contained copy for the bubble-shield plugin so the
`/bubble-shield:bubble-shield-anonymize` skill can handle PDFs in one command, even when
the plugin is installed standalone via the marketplace (no bubble_shield repo present).

Mirrors webapp/extract.py in the bubble_shield repo — keep them in sync if either
changes. Plain-text formats (.txt/.md/.csv/.json) decode directly; PDFs and
.docx go through their parser. Anything that can't yield text (encrypted/scanned
PDF) raises ExtractionError with a human FR message rather than silently feeding
garbage to the anonymiser — garbage in means PII could slip through unrecognised.

For scanned PDFs, the optional OCR pack (bubble_shield_setup_ocr) is tried if
installed. Fail-open: OCR errors fall through to the original ExtractionError.

Usage (the skill calls it as a CLI so it works without importing anything):
    python3 bubble_shield_extract.py <path>          # prints extracted text to stdout
    python3 bubble_shield_extract.py <path> --check   # prints OK / the error reason, exit 0/2
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

# Self-contained: the plugin bundles its dependencies under vendor/ (the engine
# `bubble_shield` package + a pure-python `pypdf`), so it runs from a GitHub install or
# a Cowork zip with NO `pip install` and no engine on the client's machine.
# Same idea as Bubble Sentinel. Put the vendor dir on sys.path before any import
# of bubble_shield / pypdf.
_VENDOR = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent)) / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

PDF_MAGIC = b"%PDF"
DOCX_MAGIC = b"PK\x03\x04"  # docx is a zip

_OCR_TAG = "[OCR]"  # prepended to signal OCR-sourced text to callers

# #574 (child of #547, Joris 2026-07-07 → barcode-first) — strip the ANSSI 2D-DOC
# barcode block from extracted text BEFORE tokenization. French tax notices carry a
# machine-decodable 2D-DOC barcode that encodes identity + amounts — a COVERT PII
# channel that survives even when the visible text is tokenized (the barcode payload
# is a base32-ish run, not caught by name/IBAN recognizers). We replace the whole
# block with a single marker so the reader knows a block was removed, not silently
# dropped. Amounts in the VISIBLE text are untouched (per #547 they stay toggleable).
#
# Signature (rendered in a text layer): the header `DC` + 2 version chars + a 4-char
# issuing-CA id + more header, then a long uninterrupted uppercase-alnum payload. Real
# blocks are 100-600 chars. The 40+-char contiguous payload requirement is what stops
# normal uppercase prose/headings (which have spaces) from ever matching.
_2DDOC_MARKER = "⟦2DDOC_BARCODE_STRIPPED⟧"
_2DDOC_RE = re.compile(r"\bDC[0-9A-Z]{2}[0-9A-Z]{4}[0-9A-Z]{40,}")


def strip_2ddoc_barcodes(text: str) -> str:
    """Replace any ANSSI 2D-DOC barcode block with `_2DDOC_MARKER`. Deterministic,
    no models. Idempotent (the marker itself never matches). Best-effort: on any
    regex error the original text is returned unchanged (a strip failure must never
    lose the document)."""
    try:
        return _2DDOC_RE.sub(_2DDOC_MARKER, text or "")
    except Exception:
        return text or ""

# Image formats we route through OCR. Detection is by extension OR magic bytes,
# so a mislabelled/extensionless image still gets caught (a .png renamed .dat
# must NOT fall through to the UTF-8 decoder — that returns a multi-MB binary
# blob as "text" and crashes/bloats the MCP response; #338).
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
_IMAGE_MAGICS = (
    b"\x89PNG\r\n\x1a\n",        # PNG
    b"\xff\xd8\xff",            # JPEG
    b"BM",                       # BMP
    b"GIF87a", b"GIF89a",       # GIF
    b"II*\x00", b"MM\x00*",     # TIFF (little / big endian)
)

# Downscale cap: a 7.6 MP RGBA image crashed the MCP server (#338). Cap the long
# edge so OCR doesn't blow memory/time. Above the HARD pixel cap (and unable to
# downscale) we fail closed rather than attempt OCR on a pathological image.
_IMAGE_MAX_LONG_EDGE = 4000      # px — downscale anything longer than this
_IMAGE_HARD_MAX_PIXELS = 50_000_000  # 50 MP — refuse outright (can't even open safely)


class ExtractionError(Exception):
    """Raised when a file can't be turned into usable text."""


def looks_like_pdf(filename: str, raw: bytes) -> bool:
    return raw[:5].startswith(PDF_MAGIC) or filename.lower().endswith(".pdf")


def looks_like_docx(filename: str, raw: bytes) -> bool:
    return filename.lower().endswith(".docx") and raw[:4].startswith(DOCX_MAGIC)


def looks_like_image(filename: str, raw: bytes) -> bool:
    """True if the bytes/extension look like a raster image we should OCR.

    Magic-byte first (authoritative), extension second (covers truncated reads).
    A WEBP is a RIFF container: "RIFF"<size>"WEBP"."""
    head = raw[:16]
    if any(head.startswith(m) for m in _IMAGE_MAGICS):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return (filename or "").lower().endswith(_IMAGE_EXTS)


def _ocr_pack_python() -> "Path | None":
    """Return the ocr-env venv python if the OCR pack is installed, else None.

    Also checks that the layout model sentinel exists — if setup ran but the
    model was never downloaded, we must not proceed (the subprocess would try
    to fetch from HuggingFace, which is forbidden at runtime).  Returns None
    so the caller falls through to the ExtractionError path."""
    import os
    from pathlib import Path
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
    manifest = home / "ocr.json"
    if not manifest.is_file():
        return None
    # If the layout model has not been cached yet (setup incomplete), refuse
    # to proceed — we must not attempt a HuggingFace download at runtime.
    sentinel = home / "layout_model_cached.flag"
    if not sentinel.is_file():
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
    Called only when pypdf finds no text layer. Completely local — no cloud.

    PRIVACY GUARANTEE: HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 are set in
    the subprocess env, so NO outbound network call is made at runtime.  The
    layout model MUST already be cached (guaranteed by _ocr_pack_python() which
    checks the sentinel before returning the venv path)."""
    py = _ocr_pack_python()
    if py is None:
        return None
    import subprocess
    import tempfile
    import os
    # Write raw PDF to a temp file, run docling in the ocr-env, read result
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
        # CRITICAL: enforce offline mode — NO huggingface.co calls at runtime.
        # _ocr_pack_python() already confirmed the sentinel (model cached), so
        # setting HF_HUB_OFFLINE=1 here is safe and mandatory.
        env = dict(os.environ)
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        r = subprocess.run([str(py), "-c", code],
                           capture_output=True, text=True, timeout=300, env=env)
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
    """OCR a raw image (.png/.jpg/...) via docling's IMAGE pipeline.

    Returns the OCR'd text (prefixed with _OCR_TAG) on success, None otherwise.
    Mirrors _ocr_pdf_if_pack_present but routes through InputFormat.IMAGE with an
    ImageFormatOption (same RapidOCR engine as the scanned-PDF path).

    The downscale runs INSIDE the ocr-env subprocess (Pillow is guaranteed there,
    not in the host plugin venv): a large image is shrunk to <= _IMAGE_MAX_LONG_EDGE
    on its long edge before OCR so a 7.6 MP RGBA screenshot can't blow memory/time
    and crash the MCP server (#338). An image over _IMAGE_HARD_MAX_PIXELS is
    refused there (the subprocess exits non-zero) so we fail closed, not crash.

    PRIVACY: HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE forced — no runtime HF fetch."""
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
            # Refuse pathological images outright -> non-zero exit -> fail closed.
            "sys.exit(7) if (im.width*im.height) > HARD else None;"
            # Downscale long edge to MAX, preserving aspect; re-save in place.
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
        env = dict(os.environ)
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        r = subprocess.run([str(py), "-c", code],
                           capture_output=True, text=True, timeout=300, env=env)
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

    FAIL-CLOSED (#338): an image that can't be OCR'd (pack absent, docling error,
    image too large to downscale) raises ExtractionError with a clear FR message
    — NEVER a UTF-8-decoded binary blob, NEVER an unhandled crash that kills the
    MCP server. _main maps ExtractionError → exit 2 so the caller fails closed."""
    ext = ""
    if filename:
        dot = filename.rfind(".")
        if dot != -1:
            ext = filename[dot:].lower()
    if ext not in _IMAGE_EXTS:
        ext = ".png"  # detected by magic bytes but no usable extension
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


def _is_garbled_extraction(text: str) -> bool:
    """Return True if native PDF extraction looks GARBLED (liasse fiscale glue artifact).

    This heuristic detects the class of glue artifacts produced by pypdf on the
    liasse fiscale: words are merged without spaces ("gerantETESTONI",
    "NOAMSignature", "FAKENAMESignature").  When triggered, the caller
    re-extracts via OCR (clean, layout-aware text) to eliminate the whole artifact
    class at once -- rather than patching individual glue boundaries one by one.

    CONSERVATIVE by design: when unsure -> return False (keep native text).
    A false-positive (OCR a clean doc) wastes time and may lower recall slightly.
    A false-negative (miss a garbled doc) just falls back to the per-boundary fixes
    (#273, #275) that already exist.  Bias hard toward NOT-garbled.

    Signals (all three must fire together -- AND logic avoids over-triggering):

    1. Long-token rate: tokens > 25 chars that are NOT URLs/email addresses make up
       >= 3 % of all tokens.  Normal French prose has very few tokens > 25 chars;
       glued liasse text has many.

    2. Low space density: fewer than 1 space per 10 characters (normal prose:
       ~1 per 5-6 chars; garbled liasse with many glued tokens: much lower).

    3. CamelCase-glue signature: >= 3 occurrences of a lower->upper or
       ALLCAPS->lower transition mid-token ([a-z][A-Z] or [A-Z]{2,}[a-z]).
       These transitions are the exact signature of PDF-extraction glue: the end
       of one word's casing collides with the start of the next.

    All three signals must fire.  Any one signal alone is too noisy.
    """
    if not text:
        return False

    # Signal 2: space density (fast, cheap -- check first)
    char_count = len(text)
    space_count = text.count(" ")
    if char_count == 0 or space_count / char_count >= 0.10:
        # >= 1 space per 10 chars -> normal density -> not garbled
        return False

    # Signal 1: long-token rate
    # Split on whitespace; skip URLs and email-like tokens
    _url_like = re.compile(
        r"(?:https?://|www\.|\S+@\S+\.\S+)", re.IGNORECASE
    )
    tokens = text.split()
    if not tokens:
        return False
    long_tokens = [
        t for t in tokens
        if len(t) > 25 and not _url_like.match(t)
    ]
    long_token_rate = len(long_tokens) / len(tokens)
    if long_token_rate < 0.03:
        # < 3 % long tokens -> probably normal text -> not garbled
        return False

    # Signal 3: CamelCase-glue transitions
    # [a-z][A-Z] (e.g. "tETESTONI", "tSignature") or [A-Z]{2,}[a-z] (e.g. "NOAMs")
    camel_glue_count = len(re.findall(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", text))
    if camel_glue_count < 3:
        return False

    # All three signals fired -- extraction looks garbled
    return True


# A native PDF page of real prose/forms yields hundreds of characters. A scanned
# page yields ~0 (no text layer) — but a MIXED liasse can average a low-but-
# nonzero count when a few pages have labels and most are scans. Below this
# per-page floor we treat the text layer as too thin to trust and prefer OCR.
_SPARSE_CHARS_PER_PAGE = 120


def _is_sparse_text(text: str, n_pages: int) -> bool:
    """True when a multi-page PDF's native text layer is so thin (few chars per
    page) that most pages are almost certainly scanned images the text layer
    doesn't cover — the case where OCR must be tried even though SOME text came
    back. Single-page docs are excluded (a short one-page note is legitimately
    short and shouldn't force OCR)."""
    if n_pages < 2:
        return False
    return (len(text) / n_pages) < _SPARSE_CHARS_PER_PAGE


def extract_pdf_text(raw: bytes) -> str:
    """Extract text from a PDF, or raise ExtractionError with a clear reason.

    For PDFs with a native text layer, uses pypdf (zero extra install). For
    scanned/image-only PDFs (no text layer), falls back to the optional OCR pack
    if installed (docling + RapidOCR, provisioned by bubble_shield_setup_ocr).
    For PDFs where pypdf returns text but it looks GARBLED (glue artifacts --
    #276), re-extracts via OCR when the OCR pack is installed.  Fail-open: OCR
    errors and absent OCR pack both fall back to native text + the per-boundary
    fixes (#273 / #275).
    Fail-open: OCR errors fall through to the original ExtractionError message."""
    try:
        from pypdf import PdfReader
    except ImportError:
        # Self-heal: the module-top vendor insertion (line ~34) keys off a single
        # CLAUDE_PLUGIN_ROOT env var. If that var is unset/wrong (real client case:
        # "pypdf manquant" despite pypdf being vendored in the .mcpb), retry the
        # import once after putting the file's *actual* sibling vendor dirs on the
        # path. Never trust one env var as the only resolution route.
        _here = Path(__file__).resolve()
        for _cand in (_here.parent.parent / "vendor", _here.parent / "vendor"):
            if _cand.is_dir() and str(_cand) not in sys.path:
                sys.path.insert(0, str(_cand))
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ExtractionError(
                "pypdf manquant -- installe-le pour lire les PDF : pip install pypdf"
            ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:  # malformed / not really a PDF
        raise ExtractionError(f"PDF illisible : {exc}") from exc

    if reader.is_encrypted:
        try:
            # many "protected" PDFs use an empty owner password
            if reader.decrypt("") == 0:
                raise ExtractionError("PDF chiffre : mot de passe requis.")
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("PDF chiffre : dechiffrement impossible.") from exc

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n".join(parts).strip()
    n_pages = max(1, len(reader.pages))

    # OCR trigger 1 — NO text layer at all (fully scanned PDF).
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
            "Aucun texte extractible -- PDF probablement scanne (image). "
            "Installez le pack OCR (bubble_shield_setup_ocr) pour lire ce type de fichier, "
            "ou collez le texte manuellement.")

    # OCR trigger 2 — SPARSE text layer (mostly-scanned PDF). A liasse fiscale /
    # KYC pack is often a multi-page PDF where pypdf pulls a thin text layer
    # (headers, a few form labels) while the ACTUAL data lives on scanned image
    # pages. That thin text isn't "garbled" (it's clean labels), so the garble
    # path below never fires — and downstream the anonymiser HARD FAIL-CLOSES on
    # a scanned financial doc where GLiNER "found nothing" (real leak class,
    # mcp #589). Result: the file never indexes. Fix: when the text density is
    # very low per page, prefer OCR (which reads the scanned pages) over the thin
    # native layer. Fail-open: OCR absent/failing keeps the native text.
    if _is_sparse_text(text, n_pages):
        try:
            _ocr_text = _ocr_pdf_if_pack_present(raw)
        except Exception:
            _ocr_text = None
        if _ocr_text and len(_ocr_text) > len(text):
            # OCR read materially MORE than the thin native layer → use it.
            return _ocr_text

    # #276 -- GARBLED extraction path: pypdf returned non-empty text but it looks
    # garbled (glue artifacts: words fused without spaces).  When the OCR pack is
    # installed, prefer clean OCR-sourced text over the native garbled extraction.
    # Fail-open: if OCR pack absent OR OCR fails -> keep the native text + rely on
    # the per-boundary fixes (#273 / #275) that are already applied downstream.
    if _is_garbled_extraction(text):
        try:
            _ocr_text = _ocr_pdf_if_pack_present(raw)
        except Exception:
            _ocr_text = None
        if _ocr_text:
            # OCR returned good text -- use it (the [OCR] quality note is already
            # prepended by _ocr_pdf_if_pack_present, so callers see the caveat).
            return _ocr_text
        # OCR unavailable or failed -- fall through, use native text as-is.

    return text


def extract_docx_text(raw: bytes) -> str:
    """Extract text from a .docx (Word), or raise ExtractionError.

    Pure stdlib -- a .docx is a zip of XML, so we read word/document.xml directly
    with zipfile + ElementTree. NO python-docx / lxml needed (those require a
    compiled C extension and can't be vendored cross-platform). This keeps the
    plugin fully self-contained: the client never installs anything.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:
        raise ExtractionError(f".docx illisible (zip) : {exc}") from exc
    try:
        with zf.open("word/document.xml") as fh:
            tree = ET.parse(fh)
    except KeyError as exc:
        raise ExtractionError(".docx sans word/document.xml -- fichier invalide.") from exc
    except Exception as exc:
        raise ExtractionError(f".docx illisible (xml) : {exc}") from exc

    # Join text per paragraph (<w:p>), tabs between <w:t> runs inside table cells
    # come through naturally; a paragraph break per <w:p> preserves line structure.
    lines = []
    for para in tree.iter(f"{W}p"):
        runs = [node.text for node in para.iter(f"{W}t") if node.text]
        if runs:
            lines.append("".join(runs))
    text = "\n".join(lines).strip()
    if not text:
        raise ExtractionError("Aucun texte dans le .docx (peut-etre un document vide ou scanne).")
    return text


def extract_text(filename: str, raw: bytes) -> str:
    """Dispatch on file type. PDF -> pypdf, .docx -> docx, image -> OCR, else UTF-8.

    The image branch (#338) runs BEFORE the UTF-8 fallback: a raw .png/.jpg must
    never be UTF-8-decoded (that yields a multi-MB binary blob as "text" and
    crashes the MCP server). Images fail CLOSED via extract_image_text."""
    if not raw:
        return ""
    if looks_like_pdf(filename or "", raw):
        text = extract_pdf_text(raw)
    elif looks_like_docx(filename or "", raw):
        text = extract_docx_text(raw)
    elif looks_like_image(filename or "", raw):
        text = extract_image_text(raw, filename or "")
    else:
        text = raw.decode("utf-8", errors="replace")
    # #574 — strip any 2D-DOC barcode block from EVERY extraction branch, before the
    # text reaches tokenization (the single choke-point so no branch can leak it).
    return strip_2ddoc_barcodes(text)


def extract_file(path: str | Path) -> str:
    """Read a file from disk and return its extracted plain text."""
    p = Path(path)
    return extract_text(p.name, p.read_bytes())


def _main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: bubble_shield_extract.py <path> [--check]\n")
        return 2
    path = argv[0]
    check = "--check" in argv[1:]
    try:
        text = extract_file(path)
    except ExtractionError as e:
        # Fail-closed: a file we can't extract must NOT be treated as empty/safe.
        sys.stderr.write(str(e) + "\n")
        return 2
    except Exception as e:  # unexpected -- still fail closed
        sys.stderr.write(f"Extraction impossible : {e}\n")
        return 2
    if check:
        sys.stdout.write("OK\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
