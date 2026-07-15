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

THREADING MODEL (fix/gemmad-threading)
--------------------------------------
mlx-lm inference is THREAD-AFFINE: the MLX/Metal stream is bound to the thread
that loaded the model. ThreadingHTTPServer runs every request on its own thread,
so calling generate() from a request-handler thread throws
`RuntimeError: no Stream(gpu, 0) in current thread` — which the classifier's
except-branch fail-safes to NOM, producing all-NOM verdicts.

Fix: ONE dedicated worker thread owns the model — it calls warm_up() and every
generate() call. HTTP handlers never touch the model; they enqueue a job on a
queue.Queue and block-wait on a threading.Event for the worker to return the
result. The model is therefore always USED on the SAME thread that OWNS it.
/health does not go through the queue — it reports warm state directly.
"""
from __future__ import annotations
import argparse, json, os, queue, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 4h idle-shutdown (#561-B, 2026-07-15). Was 600s — SHORTER than the sweep's
# 1200s interval, so gemmad was always cold when the sweep needed it (a structured
# form needs the Gemma verify pass; a cold-race fail-closed the doc every sweep,
# stranding it pending forever and re-warming ~4GB every 20 min for one stuck file).
# The idle-shutdown must outlast the sweep interval so the daemon stays warm across
# consecutive sweeps. Set BUBBLE_SHIELD_GEMMA_IDLE=0 for an always-warm client.
IDLE_SECS = int(os.environ.get("BUBBLE_SHIELD_GEMMA_IDLE", "14400"))  # 4h (#561-B)
DEFAULT_PORT = 8724
MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"

# Per-request back-pressure timeouts (seconds). A handler that waits this long
# for the single MLX worker gives up and returns a clean error instead of
# hanging forever, keeping the daemon responsive under load. Tunable via env.
REQ_TIMEOUT_EXTRACT = float(os.environ.get("BUBBLE_SHIELD_GEMMA_REQ_TIMEOUT", "90"))
REQ_TIMEOUT_CLASSIFY = float(
    os.environ.get("BUBBLE_SHIELD_GEMMA_CLASSIFY_TIMEOUT", "45"))
# Bounded queue as a backstop: if this many jobs are already waiting, new
# submits fail fast rather than growing the backlog unboundedly. 0 = unbounded.
QUEUE_MAXSIZE = int(os.environ.get("BUBBLE_SHIELD_GEMMA_QUEUE_MAXSIZE", "64"))

_last_activity = time.time()


class _Job:
    """A unit of inference work handed to the worker thread.

    op is the classifier method name ("classify"|"extract_pii"), arg is its
    single positional argument. The worker fills `result` or `error` and sets
    `done`; the enqueuing handler thread blocks on `done` then reads them.

    `abandoned` is set by submit() when the caller times out and gives up. The
    worker checks it BEFORE running the model and drops the job, so a stuck /
    slow backlog cannot starve the worker with work nobody is waiting on. A
    LIVE (non-abandoned) job is untouched by this and runs exactly as before.
    """
    __slots__ = ("op", "arg", "result", "error", "done", "abandoned")

    def __init__(self, op, arg):
        self.op = op
        self.arg = arg
        self.result = None
        self.error = None
        self.done = threading.Event()
        self.abandoned = False


class InferenceWorker:
    """Owns the MLX model on ONE thread and serves inference jobs off a queue.

    The worker thread loads/warms the model (thread-affine MLX stream binds
    here) and then loops pulling _Job items, running the requested classifier
    method on THIS thread, and signalling completion. HTTP handlers call
    submit() from their own threads — that only touches the thread-safe queue
    and a per-job Event, never the model.
    """

    def __init__(self, classifier, lazy=False):
        self._clf = classifier
        # lazy=True defers the model load from start() to the FIRST job (the
        # `--no-warm` production path): a modelless daemon is ~30MB and only
        # pays the ~4GB load when a pseudonymisation sweep actually runs. The
        # load STILL happens on this worker thread (MLX thread-affinity), just
        # later. lazy=False keeps the original eager behaviour byte-for-byte.
        self._lazy = lazy
        self._model_loaded = False  # worker-thread-only flag: warm_up() ran ok
        # Bounded backstop (0 => unbounded). The per-request timeout is the
        # primary defence; this just caps a runaway backlog.
        self._q: "queue.Queue[_Job]" = queue.Queue(
            maxsize=QUEUE_MAXSIZE if QUEUE_MAXSIZE > 0 else 0)
        self._thread = threading.Thread(target=self._run, name="gemma-inference",
                                        daemon=True)
        self._ready = threading.Event()  # set once warm_up() completes (eager)
        self._warm_error = None

    # --- worker-thread side -------------------------------------------------
    def start(self):
        self._thread.start()

    def _run(self):
        if self._lazy:
            # Lazy: do NOT load the model now. Signal "loop running" so main()
            # can open the port WITHOUT blocking on a ~4GB warm. The model is
            # loaded on THIS thread on the first job (see _ensure_loaded below),
            # preserving MLX thread-affinity — the load is only deferred, not
            # moved off the worker thread.
            self._ready.set()
        else:
            # Eager (default): load/warm the model ON THIS THREAD so the
            # MLX/Metal stream is owned here — every generate() below then runs
            # on the owning thread. Unchanged from the original path.
            try:
                self._clf.warm_up()
            except Exception as e:  # pragma: no cover - warm failure is fatal
                self._warm_error = e
                self._ready.set()
                return
            self._model_loaded = True
            self._ready.set()
        while True:
            job = self._q.get()
            if job is None:  # sentinel (unused today; kept for clean shutdown)
                return
            # Drop work whose caller already gave up (timed out). This is the
            # ONLY early exit and it fires only for abandoned jobs — a LIVE job
            # falls through to the unchanged run-and-signal path below. We still
            # set done so nothing can block on this job's event.
            if job.abandoned:
                job.done.set()
                continue
            try:
                # Lazy warm: load the model ON THIS (worker) thread before the
                # first real job, exactly once. Deferred from start() so a
                # `--no-warm` daemon holds ~0 model memory until first use. If
                # this raises, the except below captures it as job.error →
                # handler returns 500 (fail-toward-masking, NO fabricated
                # verdict). _model_loaded stays False, so a later job retries
                # the load; the worker loop keeps running (never crashes).
                if self._lazy and not self._model_loaded:
                    self._clf.warm_up()
                    self._model_loaded = True
                fn = getattr(self._clf, job.op)
                # Wedge fix (defense-in-depth): a classify_via_extract job loops
                # every uncertain token in ONE worker call, which can grind for
                # minutes. The abandoned flag was only checked BEFORE the job ran
                # (above); once running, an already-timed-out batch kept the sole
                # MLX worker busy. Pass a should_abort callable that returns this
                # job's live abandoned state so the classifier stops between
                # tokens the instant the caller gives up. Every OTHER op
                # (classify, extract_pii) is called exactly as before — this is
                # strictly additive and backward-compatible.
                if job.op == "classify_via_extract":
                    job.result = fn(job.arg, should_abort=lambda: job.abandoned)
                else:
                    job.result = fn(job.arg)
            except Exception as e:  # surface to caller thread; do not crash worker
                job.error = e
            finally:
                job.done.set()

    # --- handler-thread side ------------------------------------------------
    @property
    def warm(self):
        return bool(getattr(self._clf, "warm", False))

    @property
    def model_id(self):
        return getattr(self._clf, "model_id", MODEL_ID)

    def wait_ready(self, timeout=None):
        self._ready.wait(timeout)
        if self._warm_error is not None:
            raise self._warm_error

    def submit(self, op, arg, timeout=None):
        """Enqueue an inference job and block until the worker returns it.

        On a per-request `timeout`, mark the job abandoned and raise
        TimeoutError so the handler returns a clean error instead of hanging
        forever. The worker will drop the abandoned job before running the
        model, so a timed-out caller does not keep the single worker busy.
        A normal caller (no timeout) is unaffected: the wait returns True, the
        result is read, and the job runs on the worker exactly as before.
        """
        job = _Job(op, arg)
        try:
            # Non-blocking put when bounded: a full queue means the worker is
            # badly backed up, so fail fast rather than pile on more waiting.
            self._q.put(job, block=(self._q.maxsize == 0))
        except queue.Full:
            raise TimeoutError(f"inference '{op}' queue full (backpressure)")
        if not job.done.wait(timeout):
            # Caller gives up: tell the worker to skip this job when it reaches
            # the head of the queue. There is an inherent race — the worker may
            # already be running it — but marking abandoned is harmless in that
            # case (the flag is only read before the model call).
            job.abandoned = True
            raise TimeoutError(f"inference '{op}' timed out")
        if job.error is not None:
            raise job.error
        return job.result


class _DirectAdapter:
    """Wrap a bare classifier so it presents the worker's submit() interface,
    running inference synchronously on the calling thread.

    The real daemon always passes an InferenceWorker (thread-affine, correct).
    This adapter exists ONLY so unit tests can pass a plain fake/real classifier
    to make_handler_class without spinning up a worker thread — it preserves the
    pre-existing handler contract. It is NOT used for real MLX inference, which
    MUST go through InferenceWorker to stay on the model-owning thread.
    """

    def __init__(self, classifier):
        self._clf = classifier

    @property
    def warm(self):
        return bool(getattr(self._clf, "warm", False))

    @property
    def model_id(self):
        return getattr(self._clf, "model_id", MODEL_ID)

    def submit(self, op, arg, timeout=None):
        return getattr(self._clf, op)(arg)


def make_handler_class(worker):
    # Accept either an InferenceWorker (real daemon) or a bare classifier
    # (unit tests) — anything without submit() is wrapped synchronously.
    if not hasattr(worker, "submit"):
        worker = _DirectAdapter(worker)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # quiet

        @staticmethod
        def _health_payload():
            # /health must stay responsive even while an inference is in flight,
            # so it reads warm state directly and never touches the queue.
            return ({"ok": bool(worker.warm),
                     "warm": bool(worker.warm),
                     "model": worker.model_id}, 200)

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
                    spans = worker.submit("extract_pii", req.get("text", ""),
                                          timeout=REQ_TIMEOUT_EXTRACT)
                    self._send({"ok": True, "spans": spans}, 200)
                except Exception as e:
                    self._send({"ok": False, "error": type(e).__name__}, 500)
                return
            if self.path == "/classify_extract":
                # #589-C extract-based de-pollution judge. Mirrors /classify:
                # same request/response shape, same timeout, same
                # fail-toward-masking 500 (caller keeps entries masked on error).
                n = int(self.headers.get("Content-Length", 0))
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                    tokens = list(req.get("tokens", []))
                    results = worker.submit("classify_via_extract", tokens,
                                            timeout=REQ_TIMEOUT_CLASSIFY)
                    self._send({"results": results}, 200)
                except Exception as e:
                    self._send({"error": type(e).__name__}, 500)
                return
            if self.path != "/classify":
                self._send({"error": "not found"}, 404); return
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                tokens = list(req.get("tokens", []))
                results = worker.submit("classify", tokens,
                                        timeout=REQ_TIMEOUT_CLASSIFY)
                self._send({"results": results}, 200)
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
    ap.add_argument("--no-warm", action="store_true",
                    help="don't preload the model at boot (load lazily on the "
                         "first inference request, on the worker thread)")
    args = ap.parse_args(argv)

    # SINGLETON — bind the port BEFORE starting the worker / loading the model.
    # A duplicate gemmad (LaunchAgent + a sweep spawn, or two spawns racing) must
    # exit INSTANTLY on EADDRINUSE, before spinning up an InferenceWorker that
    # would load/warm a second ~4GB Gemma it can never serve. Binding first is the
    # atomic singleton (mirrors the nerd fix). Exit clean (0) so the LaunchAgent's
    # KeepAlive={SuccessfulExit:false} does not restart-loop us.
    from gemma_classifier import GemmaClassifier  # Task 4
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), None)
    except OSError as exc:
        import errno
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f"[bubble-shield-gemmad] port {args.port} already in use — "
                  "another instance owns it; exiting (singleton).", flush=True)
            return 0
        raise

    clf = GemmaClassifier()
    worker = InferenceWorker(clf, lazy=args.no_warm)
    worker.start()
    if not args.no_warm:
        # Eager (default): block until the model is warm on the worker thread,
        # exactly as before. With --no-warm we skip this block and open the port
        # immediately; the worker loads the model on the first request.
        worker.wait_ready()
    # Attach the real handler now that the worker exists (we bound with a
    # placeholder handler above purely to claim the port atomically).
    srv.RequestHandlerClass = make_handler_class(worker)
    threading.Thread(target=_idle_watchdog, args=(srv,), daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
