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
# 4h default (was 900s/15min). The idle-shutdown is a CLEAN exit (exit 0), which
# the LaunchAgent's KeepAlive={SuccessfulExit:false} does NOT auto-restart — so a
# short idle timeout dropped the daemon mid-session and read/mail refused
# (fail-closed) until a cold ~20-37s re-spawn. A 4h timeout keeps it warm across a
# normal working session while still freeing RAM overnight. Set
# BUBBLE_SHIELD_NERD_IDLE=0 (or high) for an "always-warm" client. (#561)
#
# REGRESSION FIX (2026-07-15, #561-B): the literal had drifted back to 600s while
# the comment still said 4h — a doc/code mismatch. 600s is CATASTROPHIC with the
# sweep: the sweep fires every 1200s (StartInterval) but nerd idle-shut at 600s, so
# nerd is ALWAYS cold when the sweep runs. A doc that needs GLiNER to certify (any
# structured form — liasse/CERFA) then fail-closes EVERY sweep, stays pending
# forever, and the sweep re-warms ~4GB every 20 min to retry one file that can
# never complete (observed live: 29/30 indexed, 1 liasse stuck, 4GB CPU loop).
# The idle-shutdown MUST outlast the sweep interval so the daemon warmed for sweep
# N is still alive at N+1. 4h >> 1200s satisfies that with margin.
IDLE_SECS = int(os.environ.get("BUBBLE_SHIELD_NERD_IDLE", "600"))  # 10 min (#561; aligned to gemmad v1.24.6)

_last_activity = time.time()
_lock = threading.Lock()
_warm = False

# Self-test state — populated after warm-up (None = not yet run).
# "pass" means the daemon returned a NOM match for the synthetic probe.
# "fail" means it returned [] — daemon is blind, gate must treat it as DOWN.
_SELF_TEST_PROBE = "Monsieur Jean DUPONT"
_selftest_result: str | None = None  # None | "pass" | "fail"


def _run_selftest(gliner_ext) -> str:
    """Feed a known synthetic name through the detector and return "pass"/"fail".

    Cheap: one tiny string, no I/O, cached after first run.  Never uses real
    names — synthetic only, safe to commit.

    Returns "pass" if at least one NOM match is returned, "fail" otherwise.
    """
    try:
        matches = gliner_ext.gliner_matches(_SELF_TEST_PROBE)
        if any(getattr(m, "entity_type", "") == "NOM" for m in matches):
            return "pass"
        return "fail"
    except Exception:
        return "fail"


def _load_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit(f"✗ no ML manifest at {MANIFEST} — run bubble_shield_setup_ml.py first")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _gliner_model_id(man: dict) -> str:
    """Return the model id to pass to GLiNER.from_pretrained().

    The manifest always stores the LOCAL directory of whatever was downloaded.
    For the onnx-community model family that directory only contains ONNX
    weights (onnx/model_quantized.onnx) — no pytorch_model.bin or
    model.safetensors — so GLiNER.from_pretrained(<local_dir>) raises
    FileNotFoundError and _load_model() silently caches None, making every
    predict_entities call return [] (the "healthy but blind" bug).

    Fix: when the local model_dir lacks PyTorch weights we fall back to the
    PyTorch HuggingFace id stored in the manifest (man["model_id"] points to
    the *original* HF repo id, e.g. "onnx-community/gliner_multi_pii-v1").
    gliner_ext resolves both HF ids and local paths the same way; the PyTorch
    weights are fetched from cache (already downloaded by setup) or HF.

    If no manifest model_id is present we fall back to the gliner_ext default
    ("urchade/gliner_multi_pii-v1") which is known to load correctly.
    """
    model_dir = Path(man.get("model_dir", ""))
    has_pytorch = (model_dir / "pytorch_model.bin").is_file() or \
                  any(model_dir.glob("model*.safetensors"))
    if has_pytorch:
        return str(model_dir)   # local path, has weights → use as-is

    # ONNX-only local dir: use the in-process PyTorch model path instead.
    # Prefer the pytorch_model_id field (new manifests) → model_id field →
    # hard default (the model proven to work in the in-process path).
    return (man.get("pytorch_model_id")
            or "urchade/gliner_multi_pii-v1")


def _prepare_env(man: dict) -> None:
    """Point gliner_ext and openai_pf_ext at models from the manifest.

    Fix (daemon-onnx-detection): uses _gliner_model_id() to select a model id
    that GLiNER.from_pretrained() can actually load — the ONNX-only local dir
    causes a silent FileNotFoundError that makes every detection return []."""
    # GLiNER: must be a path with PyTorch weights, or a HF repo id (not ONNX-only dir)
    os.environ["BUBBLE_SHIELD_GLINER_MODEL"] = _gliner_model_id(man)
    os.environ["BUBBLE_SHIELD_GLINER_ONNX"] = man.get("onnx_file", "")
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
    global _warm, _selftest_result
    t = time.time()
    gliner_ext.gliner_matches("warmup Jean Dupont")  # forces GLiNER model load
    if mode in ("openai", "both"):
        # Warm up OpenAI model too — will silently fail-open if not installed
        openai_pf_ext.openai_pf_matches("warmup Jean Dupont")
    _warm = True
    # Run detection self-test so /health can expose "self_test": "pass"/"fail".
    # A "fail" here means the model loaded but returns [] — the "healthy but
    # blind" scenario that caused client data to leak through uncaught.
    _selftest_result = _run_selftest(gliner_ext)
    print(f"[bubble-shield-nerd] model(s) warm in {time.time()-t:.1f}s "
          f"(mode={mode}, self_test={_selftest_result})", flush=True)
    if _selftest_result != "pass":
        print("[bubble-shield-nerd] WARNING: self-test FAILED — "
              "detection returns [] on a known synthetic name. "
              "Model may be ONNX-only without PyTorch weights. "
              "Gate will treat this daemon as DOWN (fail-closed).", flush=True)


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
    """Read detector.mode from custom_fields.json. Default = "both" (#348).

    "both" = the GLiNER∪OpenAI-PF union (merged via merge.py:merge_soft), proven
    at 95% recall on real docs with OpenAI-PF's clean precision. When the
    configured/default mode resolves to "both" but the OpenAI-PF weights are
    unavailable, the runtime degrades to "gliner"-only — see
    _resolve_runtime_mode(). The union ALWAYS degrades to the GLiNER core; it
    never crashes detection.
    """
    if not cfg_path.is_file():
        return "both"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return str(cfg.get("detector", {}).get("mode", "both")).lower()
    except Exception:
        return "both"


def _openai_weights_present(man: dict) -> bool:
    """True iff the OpenAI-PF ONNX weights named in the manifest exist on disk."""
    try:
        openai_cfg = man.get("models", {}).get("openai", {})
        model_dir = openai_cfg.get("model_dir")
        onnx_file = openai_cfg.get("onnx_file")
        if not model_dir or not onnx_file:
            return False
        return (Path(model_dir) / onnx_file).is_file()
    except Exception:
        return False


def _fetch_openai_weights(man: dict) -> bool:
    """Attempt the documented on-demand fetch of the OpenAI-PF weights.

    Invokes bubble_shield_setup_ml.py --openai (the documented fetch path) using
    the ML-pack venv python from the manifest. Returns True iff the weights are
    present afterwards. Never raises — any failure returns False so the caller
    fails open to gliner-only.

    NOTE: this is a ~900MB download; it only runs when mode=both AND the weights
    are genuinely absent. In normal operation the weights are already installed.
    """
    try:
        py = man.get("venv_python") or man.get("models", {}).get("gliner", {}).get("venv_python")
        if not py or not Path(py).is_file():
            return False
        setup = Path(__file__).resolve().parent / "bubble_shield_setup_ml.py"
        if not setup.is_file():
            return False
        import subprocess
        subprocess.run([str(py), str(setup), "--openai"], check=True, timeout=3600)
        return _openai_weights_present(man)
    except Exception as e:
        print(f"[bubble-shield-nerd] OpenAI-PF on-demand fetch failed: {e}", flush=True)
        return False


def _resolve_runtime_mode(mode: str, man: dict, _fetch=_fetch_openai_weights) -> str:
    """Decide the EFFECTIVE runtime mode, fetching OpenAI-PF weights on demand.

    If `mode` is "both" but the OpenAI-PF weights are absent, attempt the
    documented fetch; if they still can't be made available (offline, error,
    etc.) DEGRADE to "gliner"-only. The union always falls back to the GLiNER
    core — detection keeps working, never crashes. `_fetch` is injectable for
    testing so the fail-open logic is exercised without a live download.
    """
    if mode != "both":
        return mode
    if _openai_weights_present(man):
        return "both"
    # Weights absent → try the on-demand fetch. _fetch returns True only after
    # it has confirmed the weights are present (via _openai_weights_present), so
    # we trust its return value here. Fail open to gliner on False/raise.
    try:
        if _fetch(man):
            return "both"
    except Exception as e:
        print(f"[bubble-shield-nerd] OpenAI-PF fetch raised, "
              f"degrading to gliner-only: {e}", flush=True)
    print("[bubble-shield-nerd] mode=both but OpenAI-PF weights unavailable — "
          "degrading to gliner-only (union → GLiNER core, detection intact).",
          flush=True)
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
            # self_test: "pass"/"fail"/null — null means warm-up hasn't run yet
            # (--no-warm flag). A "fail" value is the gate signal: the daemon is
            # UP but blind, so clients must treat it as DOWN (fail-closed).
            self._send(200, {"ok": True, "warm": _warm,
                             "model": os.environ.get("BUBBLE_SHIELD_GLINER_MODEL", ""),
                             "mode": mode,
                             "self_test": _selftest_result})
        elif self.path == "/selftest":
            # On-demand self-test endpoint.  Returns {ok, self_test, probe}.
            # "ok" mirrors whether self_test=="pass" so callers can gate on it.
            result = _selftest_result
            if result is None:
                # warm-up not done yet — run the test now (lazy path)
                if self.gliner_ext:
                    result = _run_selftest(self.gliner_ext)
            self._send(200 if result == "pass" else 503,
                       {"ok": result == "pass",
                        "self_test": result,
                        "probe": _SELF_TEST_PROBE})
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
        global _last_activity, _selftest_result
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
                # Lazy self-test: if warm-up was skipped (--no-warm), run the
                # detection self-test on the first real /detect call and cache
                # the result so /health can report pass/fail truthfully.
                # This closes the "healthy but blind" hole for --no-warm daemons:
                # after this call, _daemon_up() will gate correctly on self_test.
                if _selftest_result is None and self.gliner_ext is not None:
                    _selftest_result = _run_selftest(self.gliner_ext)
                    if _selftest_result != "pass":
                        print(f"[bubble-shield-nerd] WARNING: lazy self-test FAILED "
                              f"— detection returns [] on a known synthetic name "
                              f"(--no-warm path). Gate will treat this daemon as DOWN.",
                              flush=True)
            out = [{"entity_type": m.entity_type, "value": m.value,
                    "start": m.start, "end": m.end, "score": m.score}
                   for m in ms]
            self._send(200, {"matches": out})
        except Exception as e:
            # fail-soft: the client falls back to regex on any error
            self._send(500, {"error": str(e), "matches": []})

    def _detect(self, text: str, mode: str, cfg_path: Path):
        """Dispatch to the correct soft detector(s) based on mode.

        Modes: "gliner" | "openai" | "both" (union, default). NOTE (#348): there is
        deliberately NO "fastino"/gliner2 detector path here. Fastino was evaluated
        in bench/ (eval_recall_boosters_329.py, realdoc_precision_330.py) and dropped
        — 81% recall, slower, native-crash on large docs. It is bench-reference only
        and is never wired as a core/default detector. Locked direction is the
        GLiNER∪OpenAI-PF union + the NOM precision-filter.
        """
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

    # Determine mode early so warm_up can decide which models to load.
    # #348: default is now "both" (GLiNER∪OpenAI-PF). If the OpenAI-PF weights
    # are absent we fetch on demand; if they can't be fetched, fail open to
    # gliner-only (the union degrades to the GLiNER core, never crashes).
    cfg_path = _custom_fields_path()
    mode = _resolve_runtime_mode(_load_detector_mode(cfg_path), man)

    Handler.gliner_ext = gliner_ext
    Handler.openai_pf_ext = openai_pf_ext
    Handler.merge_mod = merge_mod

    # SINGLETON — bind the port BEFORE loading the model. GLiNER's cold load is
    # ~50s; if two daemons start together (LaunchAgent + a spawn from the sweep /
    # posttool, or two sweep warm requests) and each loaded the model FIRST (as
    # the old order did), every loser would burn ~50s + ~2.8GB loading a model it
    # can never serve — N copies thrashing memory/CPU so NONE finishes (the
    # observed 4-nerd stampede that hung the sweep). Binding first makes a
    # duplicate exit INSTANTLY on EADDRINUSE, before any heavy work, so at most
    # one nerd ever loads the model.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        # Port already bound → another nerd owns it. Exit clean (0) so the
        # LaunchAgent's KeepAlive={SuccessfulExit:false} does NOT restart us.
        import errno
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f"[bubble-shield-nerd] port {args.port} already in use — "
                  "another instance owns it; exiting (singleton).", flush=True)
            return 0
        raise

    # Model load happens AFTER the bind now — we hold the port, so no duplicate
    # can start loading. (Lazy `--no-warm` still loads on first request.)
    if not args.no_warm:
        warm_up(gliner_ext, openai_pf_ext, mode)

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
