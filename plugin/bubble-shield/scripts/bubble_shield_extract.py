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

OCR for scanned PDFs is intentionally out of scope (heavy local stack); the
message tells the user so explicitly.

Usage (the skill calls it as a CLI so it works without importing anything):
    python3 bubble_shield_extract.py <path>          # prints extracted text to stdout
    python3 bubble_shield_extract.py <path> --check   # prints OK / the error reason, exit 0/2
"""
from __future__ import annotations

import io
import os
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


class ExtractionError(Exception):
    """Raised when a file can't be turned into usable text."""


def looks_like_pdf(filename: str, raw: bytes) -> bool:
    return raw[:5].startswith(PDF_MAGIC) or filename.lower().endswith(".pdf")


def looks_like_docx(filename: str, raw: bytes) -> bool:
    return filename.lower().endswith(".docx") and raw[:4].startswith(DOCX_MAGIC)


def extract_pdf_text(raw: bytes) -> str:
    """Extract text from a PDF, or raise ExtractionError with a clear reason."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExtractionError(
            "pypdf manquant — installe-le pour lire les PDF : pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:  # malformed / not really a PDF
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
        raise ExtractionError(
            "Aucun texte extractible — PDF probablement scanné (image). "
            "L'OCR n'est pas pris en charge ; colle le texte manuellement.")
    return text


def extract_docx_text(raw: bytes) -> str:
    """Extract text from a .docx (Word), or raise ExtractionError.

    Pure stdlib — a .docx is a zip of XML, so we read word/document.xml directly
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
        raise ExtractionError(".docx sans word/document.xml — fichier invalide.") from exc
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
        raise ExtractionError("Aucun texte dans le .docx (peut-être un document vide ou scanné).")
    return text


def extract_text(filename: str, raw: bytes) -> str:
    """Dispatch on file type. PDF → pypdf, .docx → python-docx, else UTF-8 decode."""
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
    except Exception as e:  # unexpected — still fail closed
        sys.stderr.write(f"Extraction impossible : {e}\n")
        return 2
    if check:
        sys.stdout.write("OK\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
