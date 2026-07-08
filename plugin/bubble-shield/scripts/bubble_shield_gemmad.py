#!/usr/bin/env python3
"""Bubble Shield — warm Gemma classifier daemon (bubble-shield-gemmad).

Mirrors bubble_shield_nerd.py: a long-lived localhost process holding the MLX
Gemma model resident so per-call classification is fast. Binds 127.0.0.1 ONLY
(on-device, no egress). Idle-shutdown to free RAM; launchd restarts on need.

  POST /classify {"tokens": ["Déclarant", "Dupont"]}
       -> {"results": [{"token": "Déclarant", "verdict": "MOT"},
                       {"token": "Dupont",    "verdict": "NOM"}]}
  POST /extract_pii {"text": "..."}
       -> {"ok": true, "spans": [{"type": "PRENOM", "text": "Jean"}, ...]}
  GET  /health -> {"ok": true, "warm": true, "model": "..."}

Run with the gemma venv python (has mlx_lm):
    ~/.bubble_shield/gemma-env/bin/python bubble_shield_gemmad.py [--port 8724]
"""
from __future__ import annotations
import argparse, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDLE_SECS = int(os.environ.get("BUBBLE_SHIELD_GEMMA_IDLE", "14400"))
DEFAULT_PORT = 8724
MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"

_last_activity = time.time()


def make_handler_class(classifier):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # quiet

        @staticmethod
        def _health_payload():
            return ({"ok": bool(getattr(classifier, "warm", False)),
                     "warm": bool(getattr(classifier, "warm", False)),
                     "model": getattr(classifier, "model_id", MODEL_ID)}, 200)

        def _send(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            global _last_activity
            _last_activity = time.time()
            if self.path == "/health":
                body, status = self._health_payload()
                self._send(body, status)
            else:
                self._send({"error": "not found"}, 404)

        def do_POST(self):
            global _last_activity
            _last_activity = time.time()
            if self.path == "/extract_pii":
                n = int(self.headers.get("Content-Length", 0))
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                    spans = classifier.extract_pii(req.get("text", ""))
                    self._send({"ok": True, "spans": spans}, 200)
                except Exception as e:
                    self._send({"ok": False, "error": type(e).__name__}, 500)
                return
            if self.path != "/classify":
                self._send({"error": "not found"}, 404); return
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                tokens = list(req.get("tokens", []))
                self._send({"results": classifier.classify(tokens)}, 200)
            except Exception as e:
                # fail-toward-masking: on any error, caller keeps entries masked
                self._send({"error": type(e).__name__}, 500)
    return Handler


def _idle_watchdog(server: ThreadingHTTPServer) -> None:
    """Mirror bubble_shield_nerd.py's watchdog: shut the server down after
    IDLE_SECS of inactivity. IDLE_SECS <= 0 means always-warm — never shut down.
    """
    if IDLE_SECS <= 0:
        return
    while True:
        time.sleep(30)
        if time.time() - _last_activity > IDLE_SECS:
            print(f"[bubble-shield-gemmad] idle > {IDLE_SECS}s — shutting down", flush=True)
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    from gemma_classifier import GemmaClassifier  # Task 4
    clf = GemmaClassifier()
    clf.warm_up()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler_class(clf))
    threading.Thread(target=_idle_watchdog, args=(srv,), daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
