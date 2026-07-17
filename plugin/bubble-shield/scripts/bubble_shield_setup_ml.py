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
~917MB+ for OpenAI, ~4GB for the #568 Gemma NOM/MOT judge via
install_gemma_env()). After this one-time, no-PII install step, every model
is loaded from local disk — the GLiNER/OpenAI daemon and the Gemma daemon
(HF_HUB_OFFLINE=1 in its LaunchAgent, see install_gemma_launchagent) never
reach the network again.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
ML_ENV = BUBBLE_SHIELD_HOME / "ml-env"
MODELS_DIR = BUBBLE_SHIELD_HOME / "models"
MANIFEST = BUBBLE_SHIELD_HOME / "ml.json"

# #568 — Gemma NOM/MOT judge (gazetteer de-pollution) venv. STABLE path (#561
# lesson): a per-session plugin cache path gets garbage-collected on every
# plugin update, orphaning anything installed there. This mirrors ML_ENV.
GEMMA_ENV = BUBBLE_SHIELD_HOME / "gemma-env"

# Must match gemma_classifier.py's / bubble_shield_gemmad.py's MODEL_ID — this
# is the exact HF repo install_gemma_env() pre-downloads so warm_up() never
# hits the network at first login (#568 final-review must-fix).
GEMMA_MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"

# gemma-env pip deps — VERSION-PINNED (learned rebuilding on Python 3.12).
# On 3.12 an UNPINNED `pip install mlx-lm` resolves to mlx-lm 0.31.3, which
# hard-requires transformers>=5.0.0 — but transformers 5.x broke mlx_lm's
# tokenizer registration for this Gemma model:
#   AttributeError: 'str' object has no attribute '__module__'
#     (transformers/models/auto/auto_factory.py register(), 5.x API change)
# so `mlx_lm.load(GEMMA_MODEL_ID)` crashes. The validated combo (identical to
# the working 3.9 baseline) is mlx-lm 0.29.x on transformers 4.x. We pin both:
# mlx-lm<0.31 keeps the transformers>=5 requirement out, and transformers<5
# belt-and-suspenders forces the 4.x line even if a future mlx-lm patch loosens
# its floor. wordfreq is unpinned (stable, no conflict). If you bump mlx-lm,
# RE-TEST mlx_lm.load(GEMMA_MODEL_ID) end-to-end before shipping.
GEMMA_PIP_DEPS = ["mlx-lm>=0.29,<0.31", "transformers>=4.40,<5", "wordfreq"]

# Stable, non-ephemeral home for the daemon SCRIPT + its vendored deps. The
# plugin's own scripts/ dir is an EPHEMERAL per-session Cowork plugin cache
# (…/.mcpb-cache/<hash>/server/scripts/…) that Cowork garbage-collects on every
# plugin update — a LaunchAgent pointing there crash-loops with Errno 2 after
# an update. We copy the daemon out of that cache into this stable root and
# point launchd here instead. Layout mirrors the plugin so the daemon's
# relative imports (here.parent/"vendor", sibling bubble_shield_setup_ml.py)
# all still resolve. See install_daemon_to_stable_path().
STABLE_DAEMON_ROOT = BUBBLE_SHIELD_HOME / "daemon"

# The MINIMAL set of scripts/ files the NER daemon needs at runtime, copied into
# STABLE_DAEMON_ROOT/scripts/. Kept as a tight allowlist (NOT the whole scripts/
# dir) so guard.py, the hook installers, tripwire.py, posttool_anonymize.py and
# the ~30 test_*.py files do NOT get a second stale copy under ~/.bubble_shield/
# — on a privacy tool a duplicate guard.py is a latent footgun. nerd.py's only
# scripts/-sibling reference is bubble_shield_setup_ml.py (the on-demand OpenAI-PF
# fetch at ~L235); everything else it imports comes from vendor/. If nerd.py ever
# imports a new scripts/ module, ADD it here (a missing module = daemon won't
# start), but NEVER add guard.py / hooks / tripwire / test_*.py.
_DAEMON_SCRIPTS = (
    "bubble_shield_nerd.py",       # the daemon itself
    "bubble_shield_setup_ml.py",   # nerd.py invokes it for on-demand weight fetch
)

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

# #568 — LaunchAgent for the Gemma NOM/MOT judge daemon (bubble_shield_gemmad.py).
# Mirrors LAUNCH_LABEL/LAUNCH_PLIST above; separate label/plist since it's a
# distinct process (own venv, own port 8724) from the GLiNER nerd daemon.
GEMMA_LAUNCH_LABEL = "com.bubbleshield.gemmad"
GEMMA_LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{GEMMA_LAUNCH_LABEL}.plist"

# The daemon script + its one sibling import, copied into STABLE_DAEMON_ROOT
# alongside the GLiNER daemon's files (same #561 stable-path lesson: launchd
# must never point at the ephemeral per-session plugin cache).
_GEMMA_DAEMON_SCRIPTS = (
    "bubble_shield_gemmad.py",   # the daemon itself
    "gemma_classifier.py",       # bubble_shield_gemmad.py imports this sibling
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _venv_python(env_dir: Path) -> Path:
    return env_dir / "bin" / "python"


# Bubble Shield PINS its ML venvs to Python 3.12 (NOT "whatever python launched
# this script"). On a stock Mac the launcher is /usr/bin/python3 == 3.9.6, which
# was pinning the venvs to 3.9 BY ACCIDENT — nobody chose it. That accidental
# 3.9 causes LibreSSL warnings + env flakiness AND blocks mlx_vlm (the Gemma
# vision judge needs Python 3.10+). We deliberately choose 3.12 for all three
# venvs (ml-env, gemma-env, ocr-env) so a fresh client install is consistent and
# vision-capable. 3.12 (not 3.13) is the pin because the ML dep trees
# (onnxruntime/gliner, mlx, docling) are validated there.
#
# We search for a 3.12 interpreter GENERICALLY on PATH — NO hardcoded
# /opt/homebrew (that's a Homebrew path specific to the build machine; a client
# Mac won't have it). If none is found we raise a clear, actionable error rather
# than silently falling back to the accidental 3.9.
PY312_ERROR = (
    "Python 3.12 is required to provision Bubble Shield's ML venvs but no "
    "`python3.12` was found on PATH.\n"
    "  • This Mac's system python3 is likely 3.9 (stock macOS), which is too "
    "old: it triggers LibreSSL warnings and cannot run the Gemma vision model "
    "(mlx_vlm needs Python 3.10+).\n"
    "  • Install a Python 3.12 runtime and re-run. On a machine with Homebrew: "
    "`brew install python@3.12`. For a bare client Mac WITHOUT Homebrew, the "
    "installer is expected to provision a relocatable 3.12 (see the provisioning "
    "TODO in install-app.sh / zero-prereq card #604)."
)

# #604 — the stable path install-app.sh's bare-Mac provisioner extracts a
# relocatable Python 3.12 into, when no Homebrew/PATH 3.12 is available. The
# install_only python-build-standalone tarball unpacks a single top-level
# `python/` dir, so the interpreter lands at
# ~/.bubble_shield/py312/python/bin/python3.12 (see install-app.sh's
# PY312_ROOT/PY312_BIN_DIR + provision_python312()). This setup script runs in
# a SEPARATE process from install-app.sh and does NOT inherit its PATH
# mutation, so find_python312() must ALSO probe this stable path directly —
# it's resolved via the home dir (Path.home()), not a machine-specific
# assumption like /opt/homebrew.
STABLE_PY312 = str(Path.home() / ".bubble_shield" / "py312" / "python" / "bin" / "python3.12")


def find_python312() -> str:
    """Return the path to a Python 3.12 interpreter, searched GENERICALLY on PATH.

    Order: `python3.12` on PATH first (the canonical name a 3.12 install
    exposes), then the stable ~/.bubble_shield/py312 path install-app.sh's
    bare-Mac provisioner extracts a relocatable 3.12 into (see STABLE_PY312 —
    needed because this script runs in a separate process that doesn't inherit
    install-app.sh's PATH), then any `python3.x`/`python3`/`sys.executable`
    that self-reports as 3.12 — so a provisioned-but-oddly-named 3.12 is still
    found. Deliberately does NOT hardcode /opt/homebrew: that path only exists
    on Homebrew machines (this build box), never on a bare client Mac. Raises
    RuntimeError(PY312_ERROR) if no 3.12 is present, so setup fails LOUDLY
    with an actionable message instead of silently pinning the accidental
    system 3.9."""
    candidates = [shutil.which("python3.12"), STABLE_PY312]
    # Also consider the current interpreter + generic python3 in case a 3.12 is
    # installed under a non-standard name; we still VERIFY the version is 3.12.
    candidates += [sys.executable, shutil.which("python3")]
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            out = subprocess.run(
                [cand, "-c",
                 "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if out.returncode == 0 and out.stdout.strip() == "3.12":
            return cand
    raise RuntimeError(PY312_ERROR)


def _create_venv_py312(env_dir: Path) -> None:
    """Create a venv at `env_dir` using a Python 3.12 interpreter (pinned, not
    "whatever ran this script"). Locates 3.12 via find_python312() and shells out
    to `<py312> -m venv --clear` so the resulting venv's interpreter is 3.12
    regardless of what Python is running this setup script."""
    py312 = find_python312()
    log(f"• using Python 3.12 interpreter: {py312}")
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([py312, "-m", "venv", "--clear", str(env_dir)], check=True)


def ensure_venv() -> Path:
    """Create the persistent venv if missing. Returns its python path.

    Pins the venv to Python 3.12 (see find_python312 / _create_venv_py312) —
    NOT the interpreter that launched this script (which on a stock Mac is 3.9)."""
    py = _venv_python(ML_ENV)
    if py.exists():
        log(f"✓ venv already present: {ML_ENV}")
        return py
    log(f"• creating venv at {ML_ENV} (Python 3.12) …")
    _create_venv_py312(ML_ENV)
    log("✓ venv created (Python 3.12)")
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


def install_gemma_env() -> Path:
    """Create the #568 Gemma NOM/MOT judge venv (mlx-lm + wordfreq) AND
    pre-download the Gemma model snapshot into the HF hub cache.

    Mirrors ensure_venv()/ensure_deps() for the ML pack: a dedicated venv at
    the STABLE path GEMMA_ENV (~/.bubble_shield/gemma-env by default, or
    BUBBLE_SHIELD_HOME-relative under test) — never a per-session plugin cache
    path (#561 lesson: a plugin update must not orphan it). Idempotent: skips
    venv creation if already present, and pip install is safe to re-run.

    #568 final-review must-fix: this used to stop at pip-install, leaving the
    Gemma model itself to be fetched from HuggingFace lazily at the daemon's
    FIRST warm_up() (i.e. at first login) — contradicting the "no egress"
    claim and, if HF was unreachable at that moment, silently leaving
    de-pollution off (fail-toward-masking still holds, but the accuracy
    feature never turns on). Mirrors the GLiNER path's download_model(): we
    now pre-fetch the model snapshot HERE, at install time, into the HF hub
    cache (via `mlx_lm.load`, which is also what warm_up() calls — so the
    exact same cache entry warm_up() will read is populated here). Combined
    with HF_HUB_OFFLINE=1 in the daemon's LaunchAgent env
    (install_gemma_launchagent), the daemon can then NEVER reach HF at
    runtime — genuinely zero egress after this one-time install step.

    Returns the venv dir (not the python path) per the brief's interface.
    """
    if not (GEMMA_ENV / "bin" / "python").exists():
        log(f"• creating gemma-env venv at {GEMMA_ENV} (Python 3.12) …")
        # Pin 3.12 (NOT the launching interpreter). The Gemma judge's optional
        # vision model (mlx_vlm) needs Python 3.10+, and the accidental stock
        # 3.9 pin is exactly what blocked the earlier vision swap — so gemma-env
        # in particular must be 3.12.
        _create_venv_py312(GEMMA_ENV)
        log("✓ gemma-env venv created (Python 3.12)")
    else:
        log(f"✓ gemma-env venv already present: {GEMMA_ENV}")
    py = GEMMA_ENV / "bin" / "python"
    subprocess.run([str(py), "-m", "pip", "install", "-q", *GEMMA_PIP_DEPS], check=True)
    log(f"✓ gemma-env deps installed ({', '.join(GEMMA_PIP_DEPS)})")
    download_gemma_model(py)
    return GEMMA_ENV


def download_gemma_model(py: Path, model_id: str = GEMMA_MODEL_ID) -> str:
    """Pre-download the Gemma model snapshot into the HF hub cache, using the
    SAME call (`mlx_lm.load`) that GemmaClassifier.warm_up() makes at runtime
    — so this populates exactly the cache entry warm_up() will later read.

    Skip-if-present (mirrors download_model()'s model_present() convention):
    if the model repo is already staged in the HF hub cache, mlx_lm.load()
    is a fast local load with no network — safe to re-run every install.
    Runs WITHOUT HF_HUB_OFFLINE so the (one-time, no-PII) fetch can happen;
    the runtime daemon later runs WITH HF_HUB_OFFLINE=1 so it never re-fetches.

    Returns "present" if the model was already cached, "done" if this call
    performed the download. Raises on failure (surfaced by main()'s existing
    subprocess.CalledProcessError / generic Exception handling) — never
    silently continues without model weights, since fail-toward-masking
    depends on the daemon simply not starting if setup fails."""
    already_cached = _hub_cache_model_dir(model_id).is_dir()
    if already_cached:
        log(f"✓ gemma model already cached (skip): {model_id}")
    else:
        log(f"• downloading gemma model {model_id} into the HF hub cache "
            f"(one-time, no PII) …")
    code = (
        "import sys;"
        "from mlx_lm import load;"
        f"load({model_id!r});"
        "print('OK')"
    )
    subprocess.run([str(py), "-c", code], check=True)
    log(f"✓ gemma model ready (cached, will load offline from now on): {model_id}")
    return "present" if already_cached else "done"


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


def install_daemon_to_stable_path() -> Path:
    """Copy the daemon SCRIPT + its vendored deps out of the ephemeral plugin
    cache into a STABLE home under ~/.bubble_shield/daemon, and return the stable
    nerd.py path.

    WHY: install_launchagent used to point launchd at
    ``Path(__file__).parent/bubble_shield_nerd.py``. Inside Cowork __file__ is an
    EPHEMERAL per-session plugin cache dir
    (…/.mcpb-cache/<hash>/server/scripts/…) that Cowork garbage-collects on every
    plugin update. The LaunchAgent then points at a DELETED path → launchd
    crash-loops (Errno 2) and the daemon never starts from the agent, so reads
    fail-closed ("NER down") after every update.

    We copy an EXPLICIT ALLOWLIST of scripts/ files (only what the daemon uses
    at runtime) plus the WHOLE vendor/ tree, into STABLE_DAEMON_ROOT, PRESERVING
    the layout the daemon expects:
        STABLE_DAEMON_ROOT/scripts/bubble_shield_nerd.py       (the daemon)
        STABLE_DAEMON_ROOT/scripts/bubble_shield_setup_ml.py   (nerd.py refs it)
        STABLE_DAEMON_ROOT/vendor/…                            (whole vendor tree)
    bubble_shield_nerd.py resolves vendor as ``here.parent/"vendor"`` and reads
    ``here.parent/"vendor"/"bubble_shield"/"custom_fields.json"`` and the sibling
    ``bubble_shield_setup_ml.py`` — all of which resolve correctly under this
    layout.

    WHY AN ALLOWLIST, NOT THE WHOLE scripts/ DIR: scripts/ also contains guard.py,
    the hook installers (install_user_hooks.py / uninstall_user_hooks.py),
    posttool_anonymize.py, tripwire.py and ~30 test_*.py files — NONE of which
    the daemon imports (verified: nerd.py's only scripts/-sibling reference is
    bubble_shield_setup_ml.py at ~L235; all its other imports come from vendor/).
    On a PRIVACY tool, dropping a SECOND stale copy of guard.py under
    ~/.bubble_shield/ is a latent footgun — something could resolve the wrong
    guard. So we copy only the daemon's real runtime deps and deliberately leave
    guard/hooks/tripwire/tests out of the stable home.

    _DAEMON_SCRIPTS is the allowlist. If nerd.py ever grows a new scripts/ import,
    add it here — a MISSING module means the daemon won't start (the very bug this
    whole change fixes), which is strictly worse than a bit of dead weight, so
    err toward including. guard.py/hooks/tripwire/tests must NEVER be added.

    Overwrites on each run: a re-run after a plugin update refreshes the stable
    copy to the new code — this is exactly what makes the daemon update-safe.
    (For scripts/ we copy2 each allowlisted file, overwriting; vendor/ uses
    dirs_exist_ok.) Returns the stable nerd path."""
    src_scripts = Path(__file__).resolve().parent
    src_vendor = src_scripts.parent / "vendor"
    dst_scripts = STABLE_DAEMON_ROOT / "scripts"
    dst_vendor = STABLE_DAEMON_ROOT / "vendor"

    STABLE_DAEMON_ROOT.mkdir(parents=True, exist_ok=True)
    dst_scripts.mkdir(parents=True, exist_ok=True)
    # EXPLICIT allowlist of scripts/ files the daemon actually needs at runtime.
    # Keep this tight — guard.py / hook installers / tripwire / test_*.py are
    # deliberately EXCLUDED (see docstring). copy2 overwrites any stale copy, so
    # a re-run after a plugin update refreshes the stable files.
    for name in _DAEMON_SCRIPTS:
        src_f = src_scripts / name
        if src_f.is_file():
            shutil.copy2(src_f, dst_scripts / name)
    # vendor/ is the detection ENGINE — no guard/hooks/tests live there, so the
    # whole tree is copied. __pycache__/*.pyc are skipped (stale bytecode carries
    # the ephemeral source path; regenerated on demand at the stable path).
    if src_vendor.is_dir():
        shutil.copytree(src_vendor, dst_vendor, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    stable_nerd = dst_scripts / "bubble_shield_nerd.py"
    if not stable_nerd.is_file():
        raise FileNotFoundError(
            f"daemon copy incomplete: {stable_nerd} missing after allowlist copy")
    log(f"✓ daemon installed to stable path: {STABLE_DAEMON_ROOT}")
    return stable_nerd


def install_launchagent(py: Path) -> None:
    """Write + load a LaunchAgent so the warm daemon starts at login and is kept
    alive. This is the 'from then on, no intervention' piece.

    The daemon path is STABLE because we first COPY the daemon (script + vendored
    deps) out of the ephemeral per-session plugin cache into
    ~/.bubble_shield/daemon (see install_daemon_to_stable_path). We point launchd
    at that stable copy, NOT at Path(__file__) — which under Cowork is a
    per-session .mcpb-cache dir that gets garbage-collected on every plugin
    update, leaving launchd pointing at a deleted path (Errno 2 crash-loop).

    If the copy fails (permissions, disk), we FALL BACK to the old __file__ path
    so setup never hard-fails — the hook's on-demand lazy-start still works."""
    try:
        nerd = install_daemon_to_stable_path()
    except Exception as e:  # noqa: BLE001 — never hard-fail setup on the copy
        nerd = Path(__file__).resolve().parent / "bubble_shield_nerd.py"
        log(f"⚠️ could not install daemon to stable path ({e}); "
            f"falling back to ephemeral plugin path {nerd}. The daemon may stop "
            f"starting from launchd after the next plugin update — the hook's "
            f"lazy-start still covers live sessions.")
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
    <string>--no-warm</string>
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


def install_gemma_daemon_to_stable_path() -> Path:
    """Copy bubble_shield_gemmad.py + its sibling gemma_classifier.py out of the
    ephemeral plugin cache into STABLE_DAEMON_ROOT, mirroring
    install_daemon_to_stable_path() (the GLiNER nerd equivalent, #561).

    The Gemma daemon has no vendor/ dependency (it only imports mlx_lm at
    runtime, from the gemma-env venv, plus its one sibling module) — so this
    only needs the two-file allowlist in _GEMMA_DAEMON_SCRIPTS, copied into the
    SAME STABLE_DAEMON_ROOT/scripts/ dir the GLiNER daemon uses (no separate
    root needed; the two daemons' files simply coexist there).

    Overwrites on each run, same as the GLiNER copy — a re-run after a plugin
    update refreshes the stable copy to the current source. Returns the stable
    bubble_shield_gemmad.py path."""
    src_scripts = Path(__file__).resolve().parent
    dst_scripts = STABLE_DAEMON_ROOT / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    for name in _GEMMA_DAEMON_SCRIPTS:
        src_f = src_scripts / name
        if src_f.is_file():
            shutil.copy2(src_f, dst_scripts / name)
    stable_gemmad = dst_scripts / "bubble_shield_gemmad.py"
    if not stable_gemmad.is_file():
        raise FileNotFoundError(
            f"gemma daemon copy incomplete: {stable_gemmad} missing after allowlist copy")
    log(f"✓ gemma daemon installed to stable path: {stable_gemmad}")
    return stable_gemmad


def install_gemma_launchagent(py: Path) -> None:
    """Write + load a LaunchAgent so the Gemma NOM/MOT judge daemon
    (bubble_shield_gemmad.py) starts at login and is kept alive on crash.

    Mirrors install_launchagent() above: LABEL=com.bubbleshield.gemmad,
    ProgramArguments=[<gemma-env python>, <stable path>/bubble_shield_gemmad.py],
    RunAtLoad=True, KeepAlive on non-zero exit. Stable path (#561): we first
    copy the daemon script out of the ephemeral per-session plugin cache into
    ~/.bubble_shield/daemon, and point launchd at THAT copy — never at
    Path(__file__), which Cowork garbage-collects on every plugin update.

    If the stable copy fails (permissions, disk), falls back to the __file__
    path so setup never hard-fails — mirrors install_launchagent's fallback.

    #568 final-review must-fix: also sets HF_HUB_OFFLINE=1 in the plist's
    EnvironmentVariables. bubble_shield_gemmad.py's warm_up() calls
    mlx_lm.load() IN-PROCESS (not a subprocess like the OCR pack), so an env
    var set here on the launchd-spawned process is inherited by that call.
    The model is already local from install_gemma_env()'s pre-download, so
    this just removes the daemon's ABILITY to ever reach HuggingFace at
    runtime — the same offline-enforcement pattern bubble_shield_setup_ocr.py /
    bubble_shield_extract.py already use for the OCR pack."""
    try:
        gemmad = install_gemma_daemon_to_stable_path()
    except Exception as e:  # noqa: BLE001 — never hard-fail setup on the copy
        gemmad = Path(__file__).resolve().parent / "bubble_shield_gemmad.py"
        log(f"⚠️ could not install gemma daemon to stable path ({e}); "
            f"falling back to ephemeral plugin path {gemmad}. The daemon may "
            f"stop starting from launchd after the next plugin update.")
    logf = BUBBLE_SHIELD_HOME / "gemmad.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{GEMMA_LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{gemmad}</string>
    <string>--no-warm</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BUBBLE_SHIELD_HOME</key><string>{BUBBLE_SHIELD_HOME}</string>
    <key>HF_HUB_OFFLINE</key><string>1</string>
    <key>BUBBLE_SHIELD_GEMMA_IDLE</key><string>600</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{logf}</string>
  <key>StandardErrorPath</key><string>{logf}</string>
</dict>
</plist>
"""
    # BUBBLE_SHIELD_GEMMA_IDLE=600 (10 min): free the ~4GB Gemma model when the
    # daemon has been idle 10 min — right for a daily-driver Mac (Shield used
    # occasionally). Re-warm is ~15s. On the mini during an 81k backfill the
    # daemon is never idle 10 min (docs flow continuously), so it stays warm
    # throughout the sweep and only releases RAM once the backfill truly stops —
    # so 600s is correct on BOTH the personal Mac and the indexer. (The 4h code
    # default was over-conservative; an operator can still override via this env.)
    GEMMA_LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    GEMMA_LAUNCH_PLIST.write_text(plist, encoding="utf-8")
    # reload (unload-then-load) so a re-run picks up changes; ignore unload errors
    subprocess.run(["launchctl", "unload", str(GEMMA_LAUNCH_PLIST)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", str(GEMMA_LAUNCH_PLIST)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log(f"✓ LaunchAgent installed + loaded ({GEMMA_LAUNCH_LABEL}) — "
            f"gemma daemon starts at login")
    else:
        log(f"⚠️ LaunchAgent written but load returned {r.returncode}: {r.stderr.strip()}\n"
            f"   (not fatal — the gemma judge falls back to skipped when the daemon is down)")


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
    # #568 gap fix — the Gemma NOM/MOT de-pollution judge is fetched by DEFAULT
    # now too: a non-technical client onboarding via the MCP tool must get the
    # Gemma daemon provisioned automatically (no terminal). --no-gemma is kept
    # as an explicit opt-out, mirroring --no-openai.
    ap.add_argument("--gemma", action="store_true", default=True,
                    help="provision the Gemma NOM/MOT de-pollution judge (default ON since #568-autowire)")
    ap.add_argument("--no-gemma", dest="gemma", action="store_false",
                    help="skip provisioning the Gemma judge (de-pollution safely no-ops without it)")
    args = ap.parse_args()

    if args.gc or args.gc_dry_run:
        return run_gc(dry_run=args.gc_dry_run)

    log("Bubble Shield — ML accuracy pack setup")
    log(f"  home: {BUBBLE_SHIELD_HOME}")
    log(f"  GLiNER model: {args.model} ({args.onnx})")
    if args.openai:
        log(f"  OpenAI model: {args.openai_model} ({args.openai_onnx})")
    if args.gemma:
        log(f"  Gemma judge: {GEMMA_MODEL_ID} (#568 de-pollution)")

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
            # #568 gap fix — provision the Gemma de-pollution judge right after
            # the GLiNER daemon, so the agent's normal onboarding pass (the
            # bubble_shield_setup_ml MCP tool) auto-installs it with zero
            # terminal for the client. FAIL-OPEN PER MODEL (mirrors the
            # OpenAI-PF pattern above): any failure here logs + records
            # states["gemma"] = "error" but must NEVER abort the GLiNER
            # install or flip the overall return code — de-pollution simply
            # no-ops safely if Gemma isn't provisioned, which is already the
            # correct degraded behaviour.
            if args.gemma:
                try:
                    gemma_env = install_gemma_env()
                    install_gemma_daemon_to_stable_path()
                    install_gemma_launchagent(_venv_python(gemma_env))
                    states["gemma"] = "ready"
                except Exception as e:  # noqa: BLE001 — fail-open on one model
                    states["gemma"] = "error"
                    log(f"⚠️ Gemma judge provisioning failed (de-pollution will "
                        f"no-op; GLiNER install unaffected): {e}")
    except subprocess.CalledProcessError as e:
        log(f"✗ a setup step failed: {e}")
        return 1
    except Exception as e:
        log(f"✗ setup error: {e}")
        return 1

    # Structured per-model status line (#387) — machine-readable for the MCP /
    # onboarding so it can name each model + its state to the user.
    _label = {"present": "déjà présent", "done": "téléchargé", "ready": "prêt",
              "error": "échec", "absent": "absent"}
    parts = [f"GLiNER {_label.get(states.get('gliner', 'absent'))}"]
    if args.openai:
        parts.append(f"OpenAI-PF {_label.get(states.get('openai', 'absent'))}")
    if args.gemma:
        parts.append(f"Gemma {_label.get(states.get('gemma', 'absent'))}")
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
