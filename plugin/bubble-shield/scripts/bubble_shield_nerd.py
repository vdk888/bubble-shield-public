#!/usr/bin/env python3
"""Bubble Shield — warm NER daemon (bubble-shield-nerd).

WHY A DAEMON
------------
Loading the GLiNER ONNX model costs ~50s cold but ~32ms warm. A per-tool-call
hook process can't pay 50s each time, so we hold the model resident in one
long-lived localhost process and let the PostToolUse hook POST text to it.

  - binds 127.0.0.1 ONLY (never a public interface) — 100% on-device, no egress.
  - loads the model from the persistent ~/.bubble_shield install (see bubble_shield_setup_ml.py)
    using the vendored gliner_ext.gliner_matches (chunking/union/threshold reused).
  - POST /detect  {"text": "..."}  -> {"matches": [{type,value,start,end,score}]}
    GET  /health                   -> {"ok": true, "model": ..., "warm": bool, "mode": ...}
    GET  /labels                   -> {"labels": [...]}  (active GLiNER labels)
  - idle-shutdown after BUBBLE_SHIELD_NERD_IDLE secs (default 900) to free RAM; launchd
    (or the hook) restarts it on next need.

PHASE 2 — detector mode dispatch (DEFAULT OFF):
  detector.mode in custom_fields.json controls which soft layer runs:
    "gliner" (default) → existing path, zero behaviour change
    "openai"           → OpenAI Privacy Filter ONNX adapter
    "both"             → run both, merge via merge.py §B.4

  The HTTP contract {matches:[...]} is UNCHANGED — clients (MCP, hook) are
  unaffected by the mode. The daemon owns detector selection.

Run it with the venv python from the ML pack (it has onnxruntime + gliner):
    ~/.bubble_shield/ml-env/bin/python bubble_shield_nerd.py [--port 8723]

This file itself is pure-stdlib for the server part; it imports gliner_ext +
the gliner/onnxruntime libs, which exist in the ML-pack venv.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
MANIFEST = BUBBLE_SHIELD_HOME / "ml.json"
DEFAULT_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
IDLE_SECS = int(os.environ.get("BUBBLE_SHIELD_NERD_IDLE", "900"))

_last_activity = time.time()
_lock = threading.Lock()
_warm = False


def _load_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit(f"✗ no ML manifest at {MANIFEST} — run bubble_shield_setup_ml.py first")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _prepare_env(man: dict) -> None:
    """Point gliner_ext and openai_pf_ext at local ONNX models from the manifest."""
    # GLiNER: local model dir as the id (from_pretrained accepts a local path)
    os.environ["BUBBLE_SHIELD_GLINER_MODEL"] = man["model_dir"]
    os.environ["BUBBLE_SHIELD_GLINER_ONNX"] = man["onnx_file"]
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # OpenAI PF: read from multi-model manifest if present (back-compat: old
    # manifests without "models" key just won't set the OpenAI vars, which is
    # fine — mode "openai" will fail-open and return [] if the dir is missing).
    models = man.get("models", {})
    openai_cfg = models.get("openai", {})
    if openai_cfg.get("model_dir"):
        os.environ.setdefault("BUBBLE_SHIELD_OPENAI_MODEL", openai_cfg["model_dir"])
    if openai_cfg.get("onnx_file"):
        os.environ.setdefault("BUBBLE_SHIELD_OPENAI_ONNX", openai_cfg["onnx_file"])


def _import_vendor_modules():
    """Import the vendored bubble_shield modules (gliner_ext, openai_pf_ext, merge)."""
    here = Path(__file__).resolve().parent
    vendor = here.parent / "vendor"
    sys.path.insert(0, str(vendor))
    from bubble_shield import gliner_ext  # noqa
    from bubble_shield import openai_pf_ext  # noqa
    from bubble_shield import merge as merge_mod  # noqa
    return gliner_ext, openai_pf_ext, merge_mod


def warm_up(gliner_ext, openai_pf_ext, mode: str) -> None:
    global _warm
    t = time.time()
    gliner_ext.gliner_matches("warmup Jean Dupont")  # forces GLiNER model load
    if mode in ("openai", "both"):
        # Warm up OpenAI model too — will silently fail-open if not installed
        openai_pf_ext.openai_pf_matches("warmup Jean Dupont")
    _warm = True
    print(f"[bubble-shield-nerd] model(s) warm in {time.time()-t:.1f}s (mode={mode})",
          flush=True)


def _custom_fields_path() -> Path:
    """Resolve custom_fields.json the same way the MCP server / loader does."""
    override = os.environ.get("BUBBLE_SHIELD_CUSTOM_FIELDS")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    vendor_path = here.parent / "vendor" / "bubble_shield" / "custom_fields.json"
    if vendor_path.is_file():
        return vendor_path
    return Path(os.path.expanduser("~/.config/bubble_shield/custom_fields.json"))


def _load_detector_mode(cfg_path: Path) -> str:
    """Read detector.mode from custom_fields.json. Default = "gliner" (production-safe)."""
    if not cfg_path.is_file():
        return "gliner"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return str(cfg.get("detector", {}).get("mode", "gliner")).lower()
    except Exception:
        return "gliner"


def _load_viterbi_bias_override(cfg_path: Path):
    """Read detector.openai_viterbi_bias from config, if set."""
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        bias = cfg.get("detector", {}).get("openai_viterbi_bias", {})
        if not bias:
            return None
        # Filter out null values — None means "use repo calibration"
        return {k: v for k, v in bias.items() if v is not None} or None
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    gliner_ext = None       # injected
    openai_pf_ext = None    # injected
    merge_mod = None        # injected
    _cfg_mtime = None       # last-seen custom_fields.json mtime

    def log_message(self, *a):  # silence default access log
        pass

    @classmethod
    def _maybe_reload_config(cls) -> None:
        """If custom_fields.json changed on disk, the next detect() will pick
        up updated labels and detector mode via fresh reads."""
        p = _custom_fields_path()
        mtime = p.stat().st_mtime if p.is_file() else None
        if mtime != cls._cfg_mtime:
            cls._cfg_mtime = mtime

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            cfg_path = _custom_fields_path()
            mode = _load_detector_mode(cfg_path)
            self._send(200, {"ok": True, "warm": _warm,
                             "model": os.environ.get("BUBBLE_SHIELD_GLINER_MODEL", ""),
                             "mode": mode})
        elif self.path == "/labels":
            self._maybe_reload_config()
            try:
                labels = self.gliner_ext.default_labels()
            except Exception:
                labels = list(self.gliner_ext.DEFAULT_LABELS)
            self._send(200, {"labels": labels})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global _last_activity
        if self.path != "/detect":
            self._send(404, {"error": "not found"})
            return
        self._maybe_reload_config()
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            text = payload.get("text", "")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        _last_activity = time.time()

        cfg_path = _custom_fields_path()
        mode = _load_detector_mode(cfg_path)

        try:
            with _lock:
                ms = self._detect(text, mode, cfg_path)
            out = [{"entity_type": m.entity_type, "value": m.value,
                    "start": m.start, "end": m.end, "score": m.score}
                   for m in ms]
            self._send(200, {"matches": out})
        except Exception as e:
            # fail-soft: the client falls back to regex on any error
            self._send(500, {"error": str(e), "matches": []})

    def _detect(self, text: str, mode: str, cfg_path: Path):
        """Dispatch to the correct soft detector(s) based on mode."""
        if mode == "openai":
            bias = _load_viterbi_bias_override(cfg_path)
            kwargs = {"viterbi_bias": bias} if bias else {}
            return self.openai_pf_ext.openai_pf_matches(text, **kwargs)
        elif mode == "both":
            bias = _load_viterbi_bias_override(cfg_path)
            kwargs = {"viterbi_bias": bias} if bias else {}
            gliner_ms = self.gliner_ext.gliner_matches(text)
            openai_ms = self.openai_pf_ext.openai_pf_matches(text, **kwargs)
            return self.merge_mod.merge_soft(gliner_ms, openai_ms)
        else:
            # "gliner" (default) — existing path, unchanged behaviour
            return self.gliner_ext.gliner_matches(text)


def _idle_watchdog(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(30)
        if time.time() - _last_activity > IDLE_SECS:
            print(f"[bubble-shield-nerd] idle > {IDLE_SECS}s — shutting down", flush=True)
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-warm", action="store_true",
                    help="don't preload the model (load lazily on first request)")
    args = ap.parse_args()

    man = _load_manifest()
    _prepare_env(man)
    gliner_ext, openai_pf_ext, merge_mod = _import_vendor_modules()

    # Determine mode early so warm_up can decide which models to load
    cfg_path = _custom_fields_path()
    mode = _load_detector_mode(cfg_path)

    Handler.gliner_ext = gliner_ext
    Handler.openai_pf_ext = openai_pf_ext
    Handler.merge_mod = merge_mod

    if not args.no_warm:
        warm_up(gliner_ext, openai_pf_ext, mode)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=_idle_watchdog, args=(server,), daemon=True).start()
    print(f"[bubble-shield-nerd] serving on 127.0.0.1:{args.port} "
          f"(idle-shutdown {IDLE_SECS}s, mode={mode})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("[bubble-shield-nerd] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
