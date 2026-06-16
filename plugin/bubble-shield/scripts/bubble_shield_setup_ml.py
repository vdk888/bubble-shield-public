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

USAGE
    python3 bubble_shield_setup_ml.py [--model onnx-community/gliner_multi_pii-v1] \
                               [--onnx onnx/model_quantized.onnx] [--check-only]

Runs under the client's system python3 (3.9+). Creates the venv with the same
interpreter. Network needed once (pip + model download, ~420MB total).
"""
from __future__ import annotations

import argparse
import json
import os
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
PIP_DEPS = ["onnxruntime", "gliner", "huggingface_hub"]

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


def ensure_deps(py: Path) -> None:
    """pip install the ONNX runtime + gliner into the venv (idempotent)."""
    # quick probe: are deps already importable?
    probe = subprocess.run(
        [str(py), "-c", "import onnxruntime, gliner, huggingface_hub"],
        capture_output=True)
    if probe.returncode == 0:
        log("✓ ML deps already installed (onnxruntime + gliner)")
        return
    log(f"• installing ML deps into the venv: {', '.join(PIP_DEPS)} (~71MB) …")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                   check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-q", *PIP_DEPS], check=True)
    log("✓ ML deps installed")


def download_model(py: Path, model_id: str, onnx_file: str) -> None:
    """Download the model snapshot (incl. the chosen onnx file) into MODELS_DIR.

    Runs inside the venv so it uses the venv's huggingface_hub. Stores under a
    stable local dir so the daemon loads from disk (no network at run time)."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = MODELS_DIR / model_id.replace("/", "__")
    code = (
        "import sys;"
        "from huggingface_hub import snapshot_download;"
        f"p=snapshot_download({model_id!r}, local_dir={str(local)!r},"
        f" allow_patterns=['*.json','*.txt','tokenizer*','{onnx_file}']);"
        "print(p)"
    )
    if (local / onnx_file).exists():
        log(f"✓ model already downloaded: {local}")
        return
    log(f"• downloading model {model_id} ({onnx_file}) → {local} …")
    subprocess.run([str(py), "-c", code], check=True)
    if not (local / onnx_file).exists():
        raise SystemExit(f"✗ model downloaded but {onnx_file} missing under {local}")
    sz = sum(f.stat().st_size for f in local.rglob("*") if f.is_file()) / 1e6
    log(f"✓ model ready ({sz:.0f} MB on disk)")


def write_manifest(model_id: str, onnx_file: str) -> None:
    local = MODELS_DIR / model_id.replace("/", "__")
    MANIFEST.write_text(json.dumps({
        "ml_env": str(ML_ENV),
        "venv_python": str(_venv_python(ML_ENV)),
        "model_id": model_id,
        "model_dir": str(local),
        "onnx_file": onnx_file,
    }, indent=2), encoding="utf-8")
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
    """Load the ONNX model in the venv and run one detection. Proves it works."""
    local = MODELS_DIR / model_id.replace("/", "__")
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
        log(f"✓ verified — model loads & detects ({r.stdout.strip()})")
        return True
    log(f"✗ verification failed:\n{r.stdout}\n{r.stderr}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--onnx", default=DEFAULT_ONNX)
    ap.add_argument("--check-only", action="store_true",
                    help="only verify an existing install, do not create/download")
    ap.add_argument("--no-launchd", action="store_true",
                    help="skip installing the login LaunchAgent (hook lazy-starts the daemon instead)")
    args = ap.parse_args()

    log("Bubble Shield — ML accuracy pack setup")
    log(f"  home: {BUBBLE_SHIELD_HOME}")
    log(f"  model: {args.model} ({args.onnx})")

    if args.check_only:
        py = _venv_python(ML_ENV)
        if not py.exists():
            log("✗ not installed (no venv)")
            return 1
        return 0 if verify(py, args.model, args.onnx) else 1

    try:
        py = ensure_venv()
        ensure_deps(py)
        download_model(py, args.model, args.onnx)
        write_manifest(args.model, args.onnx)
        ok = verify(py, args.model, args.onnx)
        if ok and not args.no_launchd:
            install_launchagent(py)
    except subprocess.CalledProcessError as e:
        log(f"✗ a setup step failed: {e}")
        return 1
    except Exception as e:
        log(f"✗ setup error: {e}")
        return 1

    if ok:
        log("\n✅ ML pack ready. The daemon (bubble_shield_nerd.py) can now serve fast,"
            " on-device NER. Nothing leaves this machine.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
