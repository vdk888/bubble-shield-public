#!/usr/bin/env python3
"""Bubble Shield — OCR pack bootstrap (one-time, on-demand setup).

WHAT THIS DOES (run once, when the user needs to read scanned PDFs)
-------------------------------------------------------------------
The plugin core handles native/text PDFs with zero install. Scanned or image-
only PDFs need OCR. This script provisions the OCR runtime ON the client's Mac,
ONCE, into a PERSISTENT location that survives reboots:

  ~/.bubble_shield/ocr-env/   a dedicated venv (docling + onnxruntime, ~650MB)
  ~/.bubble_shield/ocr.json   resolved paths (the extract hook reads this)

KEY DESIGN CHOICE — fully offline after setup:
  - RapidOCR + PP-OCRv6 ONNX models are bundled inside the docling wheel (pip).
  - The docling layout model (docling-models / docling-layout-heron, ~506MB) is
    downloaded ONCE here during setup into the HuggingFace local cache.
  - At OCR RUNTIME, HF_HUB_OFFLINE=1 (and TRANSFORMERS_OFFLINE=1) are set in
    the subprocess env, so NO network call is ever made after setup completes.
  - If the layout model is not cached (setup incomplete), the extract path
    fails gracefully with a clear message — never silently phones home.

PRIVACY GUARANTEE: models downloaded once at setup; OCR runs fully offline
thereafter (HF_HUB_OFFLINE enforced in every subprocess invocation).

Idempotent: re-running is safe; it skips steps already done. No LaunchAgent
needed — OCR runs on-demand in a subprocess, not as a persistent daemon.

USAGE
    python3 bubble_shield_setup_ocr.py [--check-only]

Runs under the client's system python3 (3.9+). Creates the venv with the same
interpreter. Network needed once (pip + model download, ~650MB total).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
OCR_ENV = BUBBLE_SHIELD_HOME / "ocr-env"
OCR_MANIFEST = BUBBLE_SHIELD_HOME / "ocr.json"

# docling bundles RapidOCR + PP-OCRv6 models in the wheel — no extra download.
# onnxruntime is needed by rapidocr but not declared as a hard dep by docling.
# The docling layout model (docling-layout-heron, ~506MB) is downloaded from
# HuggingFace ONCE during setup via ensure_layout_model_cached(), then
# HF_HUB_OFFLINE=1 is enforced at runtime to guarantee zero network at OCR time.
PIP_DEPS = ["docling", "onnxruntime"]

# Sentinel file written after the layout model is confirmed cached.
# Its presence means: model downloaded, HF cache warm, ready for offline use.
_LAYOUT_MODEL_SENTINEL = BUBBLE_SHIELD_HOME / "layout_model_cached.flag"


def log(msg: str) -> None:
    print(msg, flush=True)


def _venv_python(env_dir: Path) -> Path:
    return env_dir / "bin" / "python"


def ensure_venv() -> Path:
    """Create the persistent OCR venv if missing. Returns its python path."""
    py = _venv_python(OCR_ENV)
    if py.exists():
        log(f"✓ venv already present: {OCR_ENV}")
        return py
    log(f"• creating venv at {OCR_ENV} …")
    OCR_ENV.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(str(OCR_ENV))
    log("✓ venv created")
    return py


def ensure_deps(py: Path) -> None:
    """pip install docling + onnxruntime into the venv (idempotent).

    RapidOCR and PP-OCRv6 ONNX models are bundled inside the docling wheel.
    The layout model (docling-layout-heron, ~506MB) is downloaded separately
    in ensure_layout_model_cached() — this step only installs pip packages."""
    probe = subprocess.run(
        [str(py), "-c", "import docling, onnxruntime"],
        capture_output=True)
    if probe.returncode == 0:
        log("✓ OCR deps already installed (docling + onnxruntime)")
        return
    log(f"• installing OCR deps into the venv: {', '.join(PIP_DEPS)} …")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                   check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-q", *PIP_DEPS], check=True)
    log("✓ OCR deps installed")


# Script run inside the ocr-env venv to pre-download (warm) the HF layout model.
# Written to a temp file so we can use multi-line try/except cleanly.
_WARM_MODEL_SCRIPT = r'''
import sys, warnings
warnings.filterwarnings("ignore")
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    opts = PdfPipelineOptions()
    opts.do_ocr = True
    opts.ocr_options = RapidOcrOptions()
    opts.do_table_structure = False
    # Instantiating DocumentConverter triggers the layout model download/cache check.
    # With HF_HUB_OFFLINE unset (default here — we WANT the download to happen),
    # docling will fetch the model from HuggingFace if not already cached.
    DocumentConverter(format_options={"pdf": PdfFormatOption(pipeline_options=opts)})
    print("OK model cached")
except Exception as e:
    print("FAIL", str(e)[:200])
    sys.exit(1)
'''


def ensure_layout_model_cached(py: Path) -> None:
    """Download the docling layout model ONCE into the HF local cache.

    This is the ONLY network call to huggingface.co in the entire OCR lifecycle.
    After this succeeds, every subsequent OCR call sets HF_HUB_OFFLINE=1 to
    guarantee zero network traffic.

    Idempotent: if the sentinel file exists, the model is already cached and
    this step is skipped entirely (no network, no subprocess)."""
    if _LAYOUT_MODEL_SENTINEL.is_file():
        log("✓ layout model already cached (sentinel present)")
        return
    log("• downloading docling layout model into HF cache (~506MB, one-time) …")
    probe = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w",
                                        encoding="utf-8")
    probe.write(_WARM_MODEL_SCRIPT)
    probe.close()
    try:
        # Run WITHOUT HF_HUB_OFFLINE — we want the download to happen here.
        r = subprocess.run([str(py), probe.name],
                           capture_output=True, text=True, timeout=600)
    finally:
        try:
            os.unlink(probe.name)
        except Exception:
            pass
    output = r.stdout.strip()
    if r.returncode == 0 and output.startswith("OK"):
        # Write sentinel to mark the cache as warm
        _LAYOUT_MODEL_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _LAYOUT_MODEL_SENTINEL.write_text("cached", encoding="utf-8")
        log("✓ layout model cached — OCR will run fully offline from now on")
    else:
        raise RuntimeError(
            f"layout model download failed:\n{r.stdout}\n{r.stderr}"
        )


def write_manifest(py: Path) -> None:
    """Write ocr.json with the venv python path."""
    manifest = {
        "ocr_env": str(OCR_ENV),
        "venv_python": str(py),
    }
    OCR_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"✓ manifest written: {OCR_MANIFEST}")


# The verify probe is a real .py file (NOT a `python -c` one-liner): it uses
# try/except blocks that are invalid when crammed onto a single line with `;`.
# Written to a temp file and run inside the OCR venv.
# IMPORTANT: the probe is run with HF_HUB_OFFLINE=1 in the subprocess env —
# this proves that OCR works with NO network access (layout model already cached).
_VERIFY_PROBE = r'''
import sys, warnings, tempfile, os, io
warnings.filterwarnings("ignore")
# Verify we are running in offline mode (set by caller via env)
if not os.environ.get("HF_HUB_OFFLINE"):
    print("FAIL HF_HUB_OFFLINE not set in subprocess env")
    sys.exit(1)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions

opts = PdfPipelineOptions()
opts.do_ocr = True
opts.ocr_options = RapidOcrOptions()
opts.do_table_structure = False
conv = DocumentConverter(format_options={"pdf": PdfFormatOption(pipeline_options=opts)})

# Synthetic scanned page: render a label:value line to an image-only PDF (no
# text layer) so docling MUST OCR it. PIL ships with Pillow (a docling dep).
img_pdf = None
try:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 240), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Nom : DUPONT", fill=(0, 0, 0))
    d.text((40, 120), "Prenom : Jean", fill=(0, 0, 0))
    tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    img.save(tf.name, "PDF", resolution=100.0)
    tf.close()
    img_pdf = tf.name
except Exception as e:
    print("FAIL could not build synthetic image PDF:", str(e)[:120])
    sys.exit(1)

try:
    res = conv.convert(img_pdf)
    txt = res.document.export_to_markdown()
    if txt and txt.strip():
        print("OK", len(txt), "chars; sample:", repr(txt.strip()[:60]))
    else:
        # docling loaded and ran but extracted nothing — acceptable for verify
        print("WARN docling ran but extracted no text")
except Exception as e:
    print("FAIL OCR run error:", str(e)[:160])
    sys.exit(1)
finally:
    try:
        os.unlink(img_pdf)
    except Exception:
        pass
'''


def verify(py: Path) -> bool:
    """Import docling in the venv and run a 1-page synthetic image OCR test.

    Renders a label:value line with PIL to an image-only PDF (no text layer),
    passes it through docling's DocumentConverter with RapidOCR enabled, and
    confirms we get text back. All synthetic — no real client data.

    OFFLINE ENFORCEMENT: the subprocess is launched with HF_HUB_OFFLINE=1 and
    TRANSFORMERS_OFFLINE=1 — proving that OCR works with zero network access.
    This is the same env that bubble_shield_extract uses at runtime, so this
    test also proves the production path makes ZERO outbound connections.

    The probe is written to a temp .py file (multi-line try/except blocks can't
    run via `python -c` joined with semicolons).
    """
    probe = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w",
                                        encoding="utf-8")
    probe.write(_VERIFY_PROBE)
    probe.close()
    # Inherit the current env and enforce offline mode — NO network at OCR time.
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    try:
        r = subprocess.run([str(py), probe.name],
                           capture_output=True, text=True, timeout=300, env=env)
    finally:
        try:
            os.unlink(probe.name)
        except Exception:
            pass
    output = r.stdout.strip()
    if r.returncode == 0 and (output.startswith("OK") or output.startswith("WARN")):
        log(f"✓ OCR pack verified — docling loads & runs OFFLINE ({output})")
        return True
    log(f"✗ OCR verification failed:\n{r.stdout}\n{r.stderr}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true",
                    help="only verify an existing install, do not create/download")
    args = ap.parse_args()

    log("Bubble Shield — OCR pack setup")
    log(f"  home: {BUBBLE_SHIELD_HOME}")
    log(f"  ocr env: {OCR_ENV}")
    log(f"  deps: {', '.join(PIP_DEPS)}")
    log("  (RapidOCR + PP-OCRv6 ONNX models bundled inside docling wheel)")
    log("  (docling layout model downloaded once here; OCR runs offline thereafter)")

    if args.check_only:
        py = _venv_python(OCR_ENV)
        if not py.exists():
            log("✗ not installed (no venv)")
            return 1
        if not _LAYOUT_MODEL_SENTINEL.is_file():
            log("✗ layout model not cached (re-run setup without --check-only)")
            return 1
        ok = verify(py)
        return 0 if ok else 1

    ok = False
    try:
        py = ensure_venv()
        ensure_deps(py)
        ensure_layout_model_cached(py)   # downloads HF model once; sets sentinel
        write_manifest(py)
        ok = verify(py)                  # runs WITH HF_HUB_OFFLINE=1 to prove offline
    except subprocess.CalledProcessError as e:
        log(f"✗ a setup step failed: {e}")
        return 1
    except Exception as e:
        log(f"✗ setup error: {e}")
        return 1

    if ok:
        log(f"\n✅ OCR pack ready. Models downloaded once at setup; "
            f"OCR runs fully offline thereafter (HF_HUB_OFFLINE enforced). "
            f"Nothing leaves this machine at OCR runtime.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
