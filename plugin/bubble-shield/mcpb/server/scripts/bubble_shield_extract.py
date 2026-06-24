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


class ExtractionError(Exception):
    """Raised when a file can't be turned into usable text."""


def looks_like_pdf(filename: str, raw: bytes) -> bool:
    return raw[:5].startswith(PDF_MAGIC) or filename.lower().endswith(".pdf")


def looks_like_docx(filename: str, raw: bytes) -> bool:
    return filename.lower().endswith(".docx") and raw[:4].startswith(DOCX_MAGIC)


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


def _is_garbled_extraction(text: str) -> bool:
    """Return True if native PDF extraction looks GARBLED (liasse fiscale glue artifact).

    This heuristic detects the class of glue artifacts produced by pypdf on the
    liasse fiscale: words are merged without spaces ("gerantETESTONI",
    "AMELSignature", "FAKENAMESignature").  When triggered, the caller
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
    # [a-z][A-Z] (e.g. "tETESTONI", "tSignature") or [A-Z]{2,}[a-z] (e.g. "AMELs")
    camel_glue_count = len(re.findall(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", text))
    if camel_glue_count < 3:
        return False

    # All three signals fired -- extraction looks garbled
    return True


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
    """Dispatch on file type. PDF -> pypdf, .docx -> python-docx, else UTF-8 decode."""
    if not raw:
        return ""
    if looks_like_pdf(filename or "", raw):
        return extract_pdf_text(raw)
    if looks_like_docx(filename or "", raw):
        return extract_docx_text(raw)
    return raw.decode("utf-8", errors="replace")


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
