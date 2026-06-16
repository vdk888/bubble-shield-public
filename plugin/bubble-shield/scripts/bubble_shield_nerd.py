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
    GET  /health                   -> {"ok": true, "model": ..., "warm": bool}
  - idle-shutdown after BUBBLE_SHIELD_NERD_IDLE secs (default 900) to free RAM; launchd
    (or the hook) restarts it on next need.

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
    """Point gliner_ext at the local ONNX model from the manifest."""
    # gliner_ext._load_model uses model_id + BUBBLE_SHIELD_GLINER_ONNX. We pass the
    # local model DIR as the id (from_pretrained accepts a local path) so the
    # daemon never hits the network.
    os.environ["BUBBLE_SHIELD_GLINER_MODEL"] = man["model_dir"]
    os.environ["BUBBLE_SHIELD_GLINER_ONNX"] = man["onnx_file"]
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _import_gliner_ext():
    """Import the vendored gliner_ext (the detection logic we reuse)."""
    # vendor/ is two levels up from scripts/ : plugin/bubble-shield/vendor
    here = Path(__file__).resolve().parent
    vendor = here.parent / "vendor"
    sys.path.insert(0, str(vendor))
    from bubble_shield import gliner_ext  # noqa
    return gliner_ext


def warm_up(gliner_ext) -> None:
    global _warm
    t = time.time()
    gliner_ext.gliner_matches("warmup Jean Dupont")  # forces model load
    _warm = True
    print(f"[bubble-shield-nerd] model warm in {time.time()-t:.1f}s", flush=True)


class Handler(BaseHTTPRequestHandler):
    gliner_ext = None  # injected

    def log_message(self, *a):  # silence default access log
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "warm": _warm,
                             "model": os.environ.get("BUBBLE_SHIELD_GLINER_MODEL", "")})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global _last_activity
        if self.path != "/detect":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            text = payload.get("text", "")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        _last_activity = time.time()
        try:
            # serialise inference (the ONNX session isn't guaranteed thread-safe)
            with _lock:
                ms = self.gliner_ext.gliner_matches(text)
            out = [{"entity_type": m.entity_type, "value": m.value,
                    "start": m.start, "end": m.end, "score": m.score}
                   for m in ms]
            self._send(200, {"matches": out})
        except Exception as e:
            # fail-soft: the client falls back to regex on any error
            self._send(500, {"error": str(e), "matches": []})


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
    gliner_ext = _import_gliner_ext()
    Handler.gliner_ext = gliner_ext

    if not args.no_warm:
        warm_up(gliner_ext)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=_idle_watchdog, args=(server,), daemon=True).start()
    print(f"[bubble-shield-nerd] serving on 127.0.0.1:{args.port} "
          f"(idle-shutdown {IDLE_SECS}s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("[bubble-shield-nerd] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
