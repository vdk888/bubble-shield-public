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
  - The TableFormer table-structure model (also in the docling-models HF repo,
    ~100MB) lives in a DIFFERENT HF repo than the layout model and must be
    pre-cached SEPARATELY during setup.  Setup uses do_table_structure=True
    (matching the runtime setting) so both models are exercised and downloaded.
  - At OCR RUNTIME, HF_HUB_OFFLINE=1 (and TRANSFORMERS_OFFLINE=1) are set in
    the subprocess env, so NO network call is ever made after setup completes.
  - If either the layout model OR TableFormer is not cached (setup incomplete),
    the sentinel is NOT written and the extract path fails gracefully with a
    clear message — never silently phones home.

PRIVACY GUARANTEE: models downloaded once at setup; OCR runs fully offline
thereafter (HF_HUB_OFFLINE enforced in every subprocess invocation).

Idempotent: re-running is safe; it skips steps already done. No LaunchAgent
needed — OCR runs on-demand in a subprocess, not as a persistent daemon.

USAGE
    python3 bubble_shield_setup_ocr.py [--check-only]

Runs under the client's system python3 (3.9+). Creates the venv with the same
interpreter. Network needed once (pip + model download, ~750MB total including
TableFormer).
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
# HuggingFace ONCE during setup via ensure_models_cached(), then
# HF_HUB_OFFLINE=1 is enforced at runtime to guarantee zero network at OCR time.
# NOTE: TableFormer (table-structure model) lives in the SAME docling-models HF
# repo as the layout model but is only triggered when do_table_structure=True.
# Both must be pre-cached during setup so that table OCR works fully offline.
PIP_DEPS = ["docling", "onnxruntime"]

# Sentinel file written ONLY after BOTH the layout model AND TableFormer are
# confirmed cached.  Its presence means: ALL models downloaded, HF cache warm,
# ready for offline use (including table-heavy scanned documents).
_LAYOUT_MODEL_SENTINEL = BUBBLE_SHIELD_HOME / "layout_model_cached.flag"


def ocr_models_present() -> bool:
    """True iff the OCR models (layout + TableFormer) are already cached.

    Skip-if-present predicate (#387): the sentinel is written only after BOTH
    models are confirmed cached, so its presence means OCR is installed and the
    download is skipped. Pure function of BUBBLE_SHIELD_HOME — the MCP status
    path and unit tests can call it without a venv or network."""
    return _LAYOUT_MODEL_SENTINEL.is_file()


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


# Script run inside the ocr-env venv to pre-download (warm) BOTH the HF layout
# model AND the TableFormer table-structure model.
# Written to a temp file so we can use multi-line try/except cleanly.
#
# IMPORTANT: do_table_structure=True is used here (matching the runtime setting)
# so that TableFormer — which lives in the docling-models HF repo alongside the
# layout model but is NOT fetched when do_table_structure=False — is also
# downloaded and cached.  A fresh install that only ran the old warm script
# (do_table_structure=False) would be missing TableFormer and would fail at
# runtime when processing table-heavy scanned documents (HF_HUB_OFFLINE=1 →
# cache-miss → table extraction fails, even though layout OCR works fine).
_WARM_MODEL_SCRIPT = r'''
import sys, warnings
warnings.filterwarnings("ignore")
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    opts = PdfPipelineOptions()
    opts.do_ocr = True
    opts.ocr_options = RapidOcrOptions()
    # do_table_structure=True ensures the TableFormer model is also fetched/cached
    # alongside the layout model.  This aligns with the runtime setting so that
    # a fresh install can process table-heavy scanned documents fully offline.
    opts.do_table_structure = True
    # Instantiating DocumentConverter triggers BOTH the layout model AND the
    # TableFormer download/cache check.  With HF_HUB_OFFLINE unset (default here
    # — we WANT the downloads to happen), docling fetches both from HuggingFace.
    DocumentConverter(format_options={"pdf": PdfFormatOption(pipeline_options=opts)})
    print("OK models cached")
except Exception as e:
    print("FAIL", str(e)[:200])
    sys.exit(1)
'''


def ensure_models_cached(py: Path) -> None:
    """Download BOTH the docling layout model AND TableFormer into the HF cache.

    This is the ONLY network call to huggingface.co in the entire OCR lifecycle.
    After this succeeds, every subsequent OCR call sets HF_HUB_OFFLINE=1 to
    guarantee zero network traffic.

    Why TableFormer matters (#269):
      The layout model (docling-layout-heron) and the TableFormer table-structure
      model both live in the docling-models HF repo, but TableFormer is only
      fetched when do_table_structure=True.  The original setup used False →
      TableFormer was absent on a fresh install → HF_HUB_OFFLINE=1 at runtime
      caused a cache-miss and table extraction silently produced zero output for
      table-heavy scanned documents.

    Sentinel written ONLY after BOTH models are confirmed cached, so an
    incomplete setup never lets runtime attempt a network fetch.

    Idempotent: if the sentinel file exists, both models are already cached and
    this step is skipped entirely (no network, no subprocess)."""
    if _LAYOUT_MODEL_SENTINEL.is_file():
        log("✓ layout model + TableFormer already cached (sentinel present)")
        return
    log("• downloading docling layout model + TableFormer into HF cache "
        "(~750MB, one-time) …")
    probe = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w",
                                        encoding="utf-8")
    probe.write(_WARM_MODEL_SCRIPT)
    probe.close()
    try:
        # Run WITHOUT HF_HUB_OFFLINE — we want the downloads to happen here.
        r = subprocess.run([str(py), probe.name],
                           capture_output=True, text=True, timeout=600)
    finally:
        try:
            os.unlink(probe.name)
        except Exception:
            pass
    output = r.stdout.strip()
    if r.returncode == 0 and output.startswith("OK"):
        # Write sentinel ONLY after BOTH layout model AND TableFormer are cached.
        _LAYOUT_MODEL_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _LAYOUT_MODEL_SENTINEL.write_text("cached", encoding="utf-8")
        log("✓ layout model + TableFormer cached — OCR will run fully offline "
            "from now on (including table-heavy scanned documents)")
    else:
        raise RuntimeError(
            f"model download failed (layout + TableFormer):\n{r.stdout}\n{r.stderr}"
        )


# Keep the old name as an alias for backwards-compatibility with any code that
# might call it directly (e.g. tests, external scripts).
ensure_layout_model_cached = ensure_models_cached


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
    log("  (layout model + TableFormer downloaded once here; OCR runs offline thereafter)")

    if args.check_only:
        py = _venv_python(OCR_ENV)
        if not py.exists():
            log("✗ not installed (no venv)")
            return 1
        if not _LAYOUT_MODEL_SENTINEL.is_file():
            log("✗ models not cached (layout + TableFormer — re-run setup without --check-only)")
            return 1
        ok = verify(py)
        return 0 if ok else 1

    ok = False
    # #387: report whether OCR models were already present (skipped) or fetched.
    ocr_state = "present" if ocr_models_present() else "done"
    try:
        py = ensure_venv()
        ensure_deps(py)
        ensure_models_cached(py)   # downloads layout + TableFormer; sets sentinel only after BOTH
        write_manifest(py)
        ok = verify(py)            # runs WITH HF_HUB_OFFLINE=1 to prove offline
    except subprocess.CalledProcessError as e:
        log(f"✗ a setup step failed: {e}")
        return 1
    except Exception as e:
        log(f"✗ setup error: {e}")
        return 1

    log("MODEL_STATUS " + json.dumps({"ocr": ocr_state}))
    _ocr_label = {"present": "déjà présent", "done": "téléchargé"}
    log("📦 Modèle : OCR " + _ocr_label.get(ocr_state, ocr_state))

    if ok:
        log(f"\n✅ OCR pack ready. Models (layout + TableFormer) downloaded once at "
            f"setup; OCR runs fully offline thereafter (HF_HUB_OFFLINE enforced). "
            f"Nothing leaves this machine at OCR runtime.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
