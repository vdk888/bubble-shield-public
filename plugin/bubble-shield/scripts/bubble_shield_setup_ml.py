#!/usr/bin/env python3
"""Bubble Shield — ML accuracy-pack bootstrap (the one-time, day-one setup).

WHAT THIS DOES (run once, with the operator, at onboarding)
-----------------------------------------------------------
The plugin core is zero-install regex. The ML "anonymise PII from anywhere"
tier needs a real NER model, which is too big/compiled to ship inside the
<50MB plugin. So this script provisions it ON the client's Mac, ONCE, into a
PERSISTENT location that survives reboots — never the temp plugin dir:

  ~/.bubble_shield/ml-env/                 a dedicated venv (onnxruntime + gliner, ~71MB;
                                    NO torch — inference runs on onnxruntime)
  ~/.bubble_shield/models/<model>/         the pre-exported quantised ONNX model
  ~/.bubble_shield/ml.json                 resolved paths + chosen onnx file (the daemon reads this)

Idempotent: re-running is safe; it skips steps already done. Prints clear
progress so the operator can watch. It does NOT start the daemon or touch
launchd — that's bubble_shield_nerd.py / a separate install step. This script only
makes the model + runtime exist and verifies they load.

ONBOARDING DEFAULT (#387) — download ALL models in one pass
  As of #387 the onboarding/default path pulls BOTH the GLiNER model AND the
  OpenAI Privacy Filter in a single setup pass, so the client is never asked to
  install a model later. The OpenAI-PF download is ON by default now; the
  legacy --openai flag is kept for back-compat (--no-openai opts OUT). Each
  model is SKIPPED if its files are already on disk, and the setup reports a
  clear PER-MODEL status (present | downloading | done) per model by name.

PHASE 2 — OpenAI Privacy Filter support (--openai / --no-openai)
  The OpenAI model uses a SEPARATE models/ dir entry and extends ml.json with a
  "models" block for multi-model support, back-compat with the flat
  single-model format.

  OpenAI model ONNX sizes:
    onnx/model_q4.onnx + .onnx_data   → ~917 MB  (recommended for M4)
    onnx/model_quantized.onnx + data  → ~1.62 GB (INT8, higher accuracy)
  Default: model_q4.onnx (--openai-onnx to override).

USAGE
    python3 bubble_shield_setup_ml.py [--model onnx-community/gliner_multi_pii-v1] \
                               [--onnx onnx/model_quantized.onnx] [--check-only]
                               [--openai] [--openai-onnx onnx/model_q4.onnx]

Runs under the client's system python3 (3.9+). Creates the venv with the same
interpreter. Network needed once (pip + model download, ~420MB for GLiNER,
~917MB+ for OpenAI).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
ML_ENV = BUBBLE_SHIELD_HOME / "ml-env"
MODELS_DIR = BUBBLE_SHIELD_HOME / "models"
MANIFEST = BUBBLE_SHIELD_HOME / "ml.json"

DEFAULT_MODEL = "onnx-community/gliner_multi_pii-v1"
DEFAULT_ONNX = "onnx/model_quantized.onnx"   # int8; smaller variants: model_q4.onnx

OPENAI_DEFAULT_MODEL = "openai/privacy-filter"
OPENAI_DEFAULT_ONNX = "onnx/model_q4.onnx"   # ~917MB, best M4 fit

# Models DROPPED from the product that must never be left behind in the HF hub
# cache (each is benchmark-only / superseded — see the issue numbers).
#   fastino/gliner2-privacy-filter-PII-multi  → dropped in #348 (bench-only now)
# The daemon's PyTorch GLiNER fallback (_gliner_model_id in bubble_shield_nerd.py)
# loads "urchade/gliner_multi_pii-v1" from the hub cache, so urchade is
# LOAD-BEARING and is deliberately NOT in this list.
DROPPED_HUB_MODELS = ["fastino/gliner2-privacy-filter-PII-multi"]

# Phase 2: add `tokenizers` for the OpenAI fast tokenizer (onnxruntime-only path
# doesn't bring tokenizers transitively). GLiNER deps unchanged.
#
# IMPORTANT — ORT VERSION REQUIREMENT FOR OPENAI MODEL:
# openai/privacy-filter uses com.microsoft contrib ops (GatherBlockQuantized with
# 'bits' attribute, QMoE, MatMulNBits) that require onnxruntime >= 1.27.
# ort 1.19.x raises "GatherBlockQuantized not a registered op".
# ort 1.20.x raises "Unrecognized attribute: bits".
# ort >= 1.27 (currently only from ort-nightly on the MS Artifacts feed or the
# GitHub nightly releases) loads the model successfully on Apple M4.
# PyPI stable tops out at 1.19.2 as of 2026-06-21; install from nightly feed:
#   pip install ort-nightly --pre \
#     --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/ORT-Nightly/pypi/simple/
# or wait for the stable 1.27 release on PyPI.
PIP_DEPS = ["onnxruntime", "gliner", "huggingface_hub", "tokenizers"]
PIP_DEPS_OPENAI = ["onnxruntime>=1.27", "tokenizers"]  # openai model needs ort >=1.27

# LaunchAgent so the warm daemon starts at login (the "no intervention" path).
LAUNCH_LABEL = "com.bubbleinvest.bubble-shield-nerd"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def log(msg: str) -> None:
    print(msg, flush=True)


def _venv_python(env_dir: Path) -> Path:
    return env_dir / "bin" / "python"


def ensure_venv() -> Path:
    """Create the persistent venv if missing. Returns its python path."""
    py = _venv_python(ML_ENV)
    if py.exists():
        log(f"✓ venv already present: {ML_ENV}")
        return py
    log(f"• creating venv at {ML_ENV} …")
    ML_ENV.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(str(ML_ENV))
    log("✓ venv created")
    return py


def ensure_deps(py: Path, need_openai: bool = False) -> None:
    """pip install the ONNX runtime + gliner + tokenizers into the venv (idempotent).

    When need_openai=True, also verify that onnxruntime >= 1.27 is present.
    The openai/privacy-filter ONNX uses com.microsoft contrib ops
    (GatherBlockQuantized + QMoE) that only landed in ORT 1.27.  If the
    installed ORT is older, attempt to upgrade it from the ORT nightly feed.
    """
    probe = subprocess.run(
        [str(py), "-c", "import onnxruntime, gliner, huggingface_hub, tokenizers"],
        capture_output=True)
    if probe.returncode == 0:
        log("✓ ML deps already installed (onnxruntime + gliner + tokenizers)")
    else:
        log(f"• installing ML deps into the venv: {', '.join(PIP_DEPS)} …")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                       check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-q", *PIP_DEPS], check=True)
        log("✓ ML deps installed")

    if not need_openai:
        return

    # Check ORT version — OpenAI model requires >= 1.27
    ver_check = subprocess.run(
        [str(py), "-c",
         "import onnxruntime as ort; "
         "v=tuple(int(x) for x in ort.__version__.split('.')[:2]); "
         "exit(0 if v >= (1,27) else 1)"],
        capture_output=True)
    if ver_check.returncode == 0:
        log("✓ onnxruntime >= 1.27 (required for OpenAI model)")
        return

    log("• onnxruntime < 1.27 detected; upgrading from ORT nightly feed "
        "(required for openai/privacy-filter QMoE + GatherBlockQuantized ops) …")
    NIGHTLY_INDEX = ("https://aiinfra.pkgs.visualstudio.com/PublicPackages/"
                     "_packaging/ORT-Nightly/pypi/simple/")
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--pre",
         "ort-nightly",
         "--extra-index-url", NIGHTLY_INDEX],
        capture_output=True, text=True)
    if r.returncode == 0:
        log("✓ ort-nightly installed (>= 1.27)")
    else:
        log(f"⚠️  Could not upgrade onnxruntime to >= 1.27 automatically.\n"
            f"   The openai adapter will fail-open until you manually install:\n"
            f"     pip install --pre ort-nightly \\\n"
            f"       --extra-index-url {NIGHTLY_INDEX}\n"
            f"   pip error: {r.stderr[:300]}")


def model_present(model_id: str, onnx_file: str) -> bool:
    """True iff the model's chosen onnx file already exists on disk.

    This is the skip-if-present predicate (#387): a model whose onnx file is
    present is considered installed and is NOT re-downloaded. Pure function of
    BUBBLE_SHIELD_HOME / MODELS_DIR — safe to call without a venv, so the MCP
    status path and the unit tests can use it directly."""
    return (_local_dir(model_id) / onnx_file).is_file()


def download_model(py: Path, model_id: str, onnx_file: str,
                   extra_patterns: list | None = None) -> str:
    """Download the model snapshot (incl. the chosen onnx file) into MODELS_DIR.

    For the OpenAI model, pass extra_patterns to include the .onnx_data sidecar
    (onnxruntime loads it automatically from the same directory).

    Runs inside the venv so it uses the venv's huggingface_hub. Stores under a
    stable local dir so the daemon loads from disk (no network at run time).

    Returns the resulting per-model state: "present" if it was already on disk
    (skipped, no download) or "done" if it was downloaded this run (#387)."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = MODELS_DIR / model_id.replace("/", "__")
    # Default patterns for any model
    patterns = ["*.json", "*.txt", "tokenizer*", onnx_file]
    # Add .onnx_data sidecar for external-data ONNX files (OpenAI model)
    if extra_patterns:
        patterns.extend(extra_patterns)
    # Also grab the data file by standard naming convention
    onnx_data = f"{onnx_file}_data"
    if onnx_data not in patterns:
        patterns.append(onnx_data)

    patterns_repr = repr(patterns)
    code = (
        "import sys;"
        "from huggingface_hub import snapshot_download;"
        f"p=snapshot_download({model_id!r}, local_dir={str(local)!r},"
        f" allow_patterns={patterns_repr});"
        "print(p)"
    )
    if (local / onnx_file).exists():
        log(f"✓ model already downloaded (skip): {local}")
        return "present"
    log(f"• downloading model {model_id} ({onnx_file}) → {local} …")
    subprocess.run([str(py), "-c", code], check=True)
    if not (local / onnx_file).exists():
        raise SystemExit(f"✗ model downloaded but {onnx_file} missing under {local}")
    sz = sum(f.stat().st_size for f in local.rglob("*") if f.is_file()) / 1e6
    log(f"✓ model ready ({sz:.0f} MB on disk)")
    # #386: stop the double-store. snapshot_download(local_dir=…) on modern
    # huggingface_hub (>=1.0) downloads straight into local_dir and does NOT
    # populate the hub cache. But older hub versions (and any from_pretrained
    # that ran first) leave a redundant models--<org>--<name> copy in the hub
    # cache — pure dead weight once the subset is localized here. Purge it now
    # that the local copy is confirmed on disk, so only ~/.bubble_shield/models/
    # remains for this model. Belt-and-suspenders: a no-op when the cache copy
    # never existed.
    freed = _purge_hub_cache_model(model_id)
    if freed:
        log(f"✓ purged redundant HF hub-cache copy of {model_id} "
            f"({freed/1e6:.0f} MB reclaimed)")
    return "done"


def _local_dir(model_id: str) -> Path:
    return MODELS_DIR / model_id.replace("/", "__")


def _hub_cache_dir(hub_cache: Path | None = None) -> Path:
    """Resolve the HF hub cache root (where snapshot_download stages models--<org>--<name>).

    Order of precedence mirrors huggingface_hub: explicit arg > HF_HUB_CACHE >
    HF_HOME/hub > ~/.cache/huggingface/hub. Pure path resolution — does not
    create or touch anything. Tests pass an explicit tmp path so the real cache
    is never read or written."""
    if hub_cache is not None:
        return Path(hub_cache)
    env_cache = os.environ.get("HF_HUB_CACHE")
    if env_cache:
        return Path(env_cache)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _hub_cache_model_dir(model_id: str, hub_cache: Path | None = None) -> Path:
    """Path of a model's staging dir inside the HF hub cache (models--<org>--<name>)."""
    return _hub_cache_dir(hub_cache) / f"models--{model_id.replace('/', '--')}"


def _purge_hub_cache_model(model_id: str, hub_cache: Path | None = None) -> int:
    """Delete a model's HF hub-cache staging dir + its .locks entry. Returns bytes freed.

    Safe no-op if the dir is absent. Only ever touches the hub cache — never
    MODELS_DIR / the local store. Caller is responsible for confirming the model
    is safe to purge (localized elsewhere, or known-dropped)."""
    cache_dir = _hub_cache_dir(hub_cache)
    model_dir = _hub_cache_model_dir(model_id, hub_cache)
    freed = 0
    if model_dir.is_dir():
        freed += sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        shutil.rmtree(model_dir, ignore_errors=True)
    # The matching lock dir (models--<org>--<name>) under .locks/ is dead too.
    lock_dir = cache_dir / ".locks" / f"models--{model_id.replace('/', '--')}"
    if lock_dir.is_dir():
        shutil.rmtree(lock_dir, ignore_errors=True)
    return freed


def _localized_model_ids() -> set[str]:
    """HF repo ids that are already localized under MODELS_DIR.

    A subdir `models/<org>__<name>/` that contains at least one *.onnx file is
    considered a localized model whose hub-cache staging copy is redundant. We
    require an actual onnx weight (not just config) so a half-written dir never
    licenses deleting the hub copy."""
    out: set[str] = set()
    if not MODELS_DIR.is_dir():
        return out
    for sub in MODELS_DIR.iterdir():
        if not sub.is_dir():
            continue
        if not any(sub.rglob("*.onnx")):
            continue
        # <org>__<name>  → <org>/<name>  (model ids never contain "__")
        out.add(sub.name.replace("__", "/"))
    return out


def gc(hub_cache: Path | None = None, dry_run: bool = False) -> dict:
    """Reclaim disk: remove redundant HF hub-cache copies + dropped models.

    What it deletes (HF HUB CACHE ONLY — never MODELS_DIR / the local store):
      (a) hub-cache copies of models already localized under MODELS_DIR
          (confirmed by an .onnx weight present in the local dir), and
      (b) known-DROPPED models (DROPPED_HUB_MODELS, e.g. fastino #348).

    Conservative by construction: a hub-cache model is removed ONLY if it is
    confirmed-localized OR explicitly in the dropped list. The live local model
    and everything under ~/.bubble_shield/models/ are never touched. Unrelated
    third-party models in the shared HF cache (FLUX, whisper, …) are left alone.
    Load-bearing fallbacks (urchade — the daemon's PyTorch GLiNER path) are not
    in the dropped list and won't be localized as ONNX, so they survive.

    Returns a summary dict; pass dry_run=True to report without deleting."""
    cache_dir = _hub_cache_dir(hub_cache)
    removed: list[dict] = []
    total_freed = 0

    def _consider(model_id: str, reason: str) -> None:
        nonlocal total_freed
        model_dir = _hub_cache_model_dir(model_id, hub_cache)
        if not model_dir.is_dir():
            return
        sz = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        removed.append({"model_id": model_id, "reason": reason, "bytes": sz})
        total_freed += sz
        if not dry_run:
            _purge_hub_cache_model(model_id, hub_cache)

    localized = _localized_model_ids()
    for model_id in sorted(localized):
        _consider(model_id, "localized-duplicate")
    for model_id in DROPPED_HUB_MODELS:
        _consider(model_id, "dropped")

    summary = {
        "hub_cache": str(cache_dir),
        "dry_run": dry_run,
        "removed": removed,
        "bytes_freed": total_freed,
    }
    return summary


def run_gc(hub_cache: Path | None = None, dry_run: bool = False) -> int:
    """CLI entrypoint for `--gc`. Prints a human-readable report. Returns 0."""
    log("Bubble Shield — model garbage collection (#386)")
    log(f"  hub cache: {_hub_cache_dir(hub_cache)}")
    log(f"  local store: {MODELS_DIR}")
    if dry_run:
        log("  mode: DRY RUN (nothing will be deleted)")
    summary = gc(hub_cache=hub_cache, dry_run=dry_run)
    if not summary["removed"]:
        log("✓ nothing to reclaim — no redundant hub-cache copies or dropped models")
        return 0
    for r in summary["removed"]:
        verb = "would remove" if dry_run else "removed"
        log(f"  {verb}: {r['model_id']} ({r['reason']}, {r['bytes']/1e6:.0f} MB)")
    total_mb = summary["bytes_freed"] / 1e6
    log(f"✓ {'would reclaim' if dry_run else 'reclaimed'} {total_mb:.0f} MB "
        f"({len(summary['removed'])} model(s)). Local store untouched.")
    return 0


def write_manifest(
    model_id: str,
    onnx_file: str,
    openai_model_id: str | None = None,
    openai_onnx_file: str | None = None,
) -> None:
    """Write ml.json. Back-compat: top-level keys are always the GLiNER model.
    Phase 2 adds a 'models' block for multi-model support (daemon reads it)."""
    local = _local_dir(model_id)
    manifest: dict = {
        "ml_env": str(ML_ENV),
        "venv_python": str(_venv_python(ML_ENV)),
        # Top-level keys: GLiNER (back-compat with old manifests)
        "model_id": model_id,
        "model_dir": str(local),
        "onnx_file": onnx_file,
        "models": {
            "gliner": {
                "model_id": model_id,
                "model_dir": str(local),
                "onnx_file": onnx_file,
            }
        },
    }
    if openai_model_id and openai_onnx_file:
        openai_local = _local_dir(openai_model_id)
        manifest["models"]["openai"] = {
            "model_id": openai_model_id,
            "model_dir": str(openai_local),
            "onnx_file": openai_onnx_file,
        }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"✓ manifest written: {MANIFEST}")


def install_launchagent(py: Path) -> None:
    """Write + load a LaunchAgent so the warm daemon starts at login and is kept
    alive. This is the 'from then on, no intervention' piece. The daemon script
    lives in the plugin's scripts/ dir; we resolve it relative to THIS file so
    the path is stable (the plugin dir is where bubble_shield_nerd.py ships)."""
    nerd = Path(__file__).resolve().parent / "bubble_shield_nerd.py"
    logf = BUBBLE_SHIELD_HOME / "nerd.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{nerd}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>BUBBLE_SHIELD_HOME</key><string>{BUBBLE_SHIELD_HOME}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{logf}</string>
  <key>StandardErrorPath</key><string>{logf}</string>
</dict>
</plist>
"""
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_PLIST.write_text(plist, encoding="utf-8")
    # reload (unload-then-load) so a re-run picks up changes; ignore unload errors
    subprocess.run(["launchctl", "unload", str(LAUNCH_PLIST)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", str(LAUNCH_PLIST)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log(f"✓ LaunchAgent installed + loaded ({LAUNCH_LABEL}) — daemon starts at login")
    else:
        log(f"⚠️ LaunchAgent written but load returned {r.returncode}: {r.stderr.strip()}\n"
            f"   (the hook will lazy-start the daemon anyway — not fatal)")


def verify(py: Path, model_id: str, onnx_file: str) -> bool:
    """Load the ONNX GLiNER model in the venv and run one detection. Proves it works."""
    local = _local_dir(model_id)
    code = (
        "import os,sys,time;"
        "os.environ['TOKENIZERS_PARALLELISM']='false';"
        "from gliner import GLiNER;"
        f"m=GLiNER.from_pretrained({str(local)!r}, load_onnx_model=True,"
        f" onnx_model_file={onnx_file!r});"
        "t=time.time();"
        "e=m.predict_entities('Madame Sylvie Brunel, IBAN FR76 3000 6000 0112 3456 7890 189',"
        " ['person name','iban'], threshold=0.4);"
        "print('OK', len(e), round((time.time()-t)*1000), [x['text'] for x in e])"
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.startswith("OK"):
        log(f"✓ GLiNER verified — model loads & detects ({r.stdout.strip()})")
        return True
    log(f"✗ GLiNER verification failed:\n{r.stdout}\n{r.stderr}")
    return False


def verify_openai(py: Path, model_id: str, onnx_file: str) -> bool:
    """Verify the OpenAI Privacy Filter adapter loads and runs on a probe text."""
    local = _local_dir(model_id)
    # The adapter imports onnxruntime + tokenizers (no torch / gliner needed)
    here = Path(__file__).resolve().parent
    vendor = here.parent / "vendor"
    code = (
        "import os, sys;"
        f"sys.path.insert(0, {str(vendor)!r});"
        "os.environ['TOKENIZERS_PARALLELISM']='false';"
        "from bubble_shield.openai_pf_ext import openai_pf_matches;"
        "ms = openai_pf_matches("
        "    'Contact Jean Martin at jean.martin@example.com or 06 12 34 56 78',"
        f"   model_dir={str(local)!r},"
        f"   onnx_file={onnx_file!r},"
        ");"
        "print('OK', len(ms), [m.entity_type for m in ms])"
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.startswith("OK"):
        log(f"✓ OpenAI PF verified — adapter loads & detects ({r.stdout.strip()})")
        return True
    log(f"✗ OpenAI PF verification failed:\n{r.stdout}\n{r.stderr}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--onnx", default=DEFAULT_ONNX)
    ap.add_argument("--check-only", action="store_true",
                    help="only verify an existing install, do not create/download")
    ap.add_argument("--gc", action="store_true",
                    help="reclaim disk: purge redundant HF hub-cache copies of "
                         "localized models + dropped benchmark models (#386). "
                         "Never touches ~/.bubble_shield/models/")
    ap.add_argument("--gc-dry-run", action="store_true",
                    help="report what --gc would remove without deleting anything")
    ap.add_argument("--no-launchd", action="store_true",
                    help="skip installing the login LaunchAgent (hook lazy-starts the daemon instead)")
    # Phase 2 / #387 flags. As of #387 the OpenAI Privacy Filter is fetched by
    # DEFAULT (the onboarding pulls all models in one pass). --openai is kept as
    # a harmless no-op for back-compat; --no-openai opts OUT (GLiNER only).
    ap.add_argument("--openai", action="store_true", default=True,
                    help="fetch the OpenAI Privacy Filter model (default ON since #387)")
    ap.add_argument("--no-openai", dest="openai", action="store_false",
                    help="skip the OpenAI Privacy Filter (GLiNER only — back-compat opt-out)")
    ap.add_argument("--openai-model", default=OPENAI_DEFAULT_MODEL,
                    help=f"OpenAI model HF id (default: {OPENAI_DEFAULT_MODEL})")
    ap.add_argument("--openai-onnx", default=OPENAI_DEFAULT_ONNX,
                    help=f"OpenAI ONNX file to use (default: {OPENAI_DEFAULT_ONNX} ~917MB; "
                         "alternative: onnx/model_quantized.onnx ~1.62GB)")
    args = ap.parse_args()

    if args.gc or args.gc_dry_run:
        return run_gc(dry_run=args.gc_dry_run)

    log("Bubble Shield — ML accuracy pack setup")
    log(f"  home: {BUBBLE_SHIELD_HOME}")
    log(f"  GLiNER model: {args.model} ({args.onnx})")
    if args.openai:
        log(f"  OpenAI model: {args.openai_model} ({args.openai_onnx})")

    if args.check_only:
        py = _venv_python(ML_ENV)
        if not py.exists():
            log("✗ not installed (no venv)")
            return 1
        ok = verify(py, args.model, args.onnx)
        if args.openai:
            ok = verify_openai(py, args.openai_model, args.openai_onnx) and ok
        return 0 if ok else 1

    ok = False
    # Per-model state, surfaced as a structured line the MCP/onboarding parses.
    states: dict[str, str] = {}
    try:
        py = ensure_venv()
        ensure_deps(py, need_openai=args.openai)
        states["gliner"] = download_model(py, args.model, args.onnx)

        openai_model_id = None
        openai_onnx_file = None
        if args.openai:
            openai_model_id = args.openai_model
            openai_onnx_file = args.openai_onnx
            # OpenAI model needs the .onnx_data sidecar for external-data ONNX.
            # Fail-open per model: if the OpenAI-PF download fails, report it but
            # don't abort — the daemon falls back to GLiNER (#387).
            try:
                states["openai"] = download_model(
                    py, openai_model_id, openai_onnx_file,
                    extra_patterns=[
                        "viterbi_calibration.json",
                        "tokenizer.json",
                        "config.json",
                        f"{openai_onnx_file}_data",
                    ]
                )
            except Exception as e:  # noqa: BLE001 — fail-open on one model
                states["openai"] = "error"
                log(f"⚠️ OpenAI-PF download failed (continuing GLiNER-only): {e}")
                openai_model_id = None
                openai_onnx_file = None

        write_manifest(args.model, args.onnx, openai_model_id, openai_onnx_file)
        ok = verify(py, args.model, args.onnx)
        if args.openai and ok:
            ok = verify_openai(py, openai_model_id, openai_onnx_file)
        if ok and not args.no_launchd:
            install_launchagent(py)
    except subprocess.CalledProcessError as e:
        log(f"✗ a setup step failed: {e}")
        return 1
    except Exception as e:
        log(f"✗ setup error: {e}")
        return 1

    # Structured per-model status line (#387) — machine-readable for the MCP /
    # onboarding so it can name each model + its state to the user.
    _label = {"present": "déjà présent", "done": "téléchargé",
              "error": "échec", "absent": "absent"}
    parts = [f"GLiNER {_label.get(states.get('gliner', 'absent'))}"]
    if args.openai:
        parts.append(f"OpenAI-PF {_label.get(states.get('openai', 'absent'))}")
    log("MODEL_STATUS " + json.dumps(states))
    log("📦 Modèles : " + " · ".join(parts))

    if ok:
        openai_note = " + OpenAI Privacy Filter" if args.openai else ""
        log(f"\n✅ ML pack ready{openai_note}. The daemon (bubble_shield_nerd.py) can now "
            f"serve fast, on-device NER. Nothing leaves this machine.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
