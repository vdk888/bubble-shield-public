"""
test_gemmad_threading.py — regression for the daemon thread-affinity bug.

BUG (fix/gemmad-threading): the daemon used ThreadingHTTPServer, so each HTTP
request ran on a NEW thread. mlx-lm inference is THREAD-AFFINE — the MLX/Metal
stream is bound to the thread that loaded the model (warm_up, at startup). A
generate() call from a per-request handler thread raised
`RuntimeError: no Stream(gpu, 0) in current thread`, the classifier's
except-branch fail-safed to NOM for every token, and the daemon returned
ALL-NOM verdicts.

FIX: one dedicated InferenceWorker thread owns the model (warm_up + every
generate) and serves jobs off a queue.Queue; handlers enqueue and block on an
Event. All inference therefore runs on the ONE model-owning thread.

This file has two layers:

  1. Model-free tests (always run): prove that every classify/extract_pii call
     is executed on the SINGLE worker thread — never on a request-handler
     thread — which is exactly the invariant the bug violated. A "thread-affine"
     fake classifier that only works on its owning thread stands in for MLX.

  2. Real-model end-to-end test (skipped unless mlx_lm importable): starts the
     REAL daemon over HTTP and asserts the 5 canonical tokens classify to
     NOM/MOT/MOT/MOT/NOM, single-shot AND under concurrency — the empirical
     acceptance criterion.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from bubble_shield_gemmad import (  # noqa: E402
    InferenceWorker,
    make_handler_class,
)


# ===========================================================================
# Layer 1 — model-free: prove inference runs on ONE dedicated worker thread,
# never on the request-handler thread (the exact invariant the bug broke).
# ===========================================================================


class _ThreadAffineClassifier:
    """Stand-in for the thread-affine MLX classifier.

    warm_up() records its owning thread. Any classify/extract_pii call NOT on
    that thread raises — mimicking `no Stream(gpu, 0) in current thread`. If the
    daemon ever calls inference on the wrong thread (the bug), verdicts would be
    all-NOM / the call would raise; here we make that failure LOUD.
    """

    model_id = "fake-thread-affine"

    def __init__(self):
        self.warm = False
        self._owner = None
        self.call_threads = []

    def warm_up(self):
        self._owner = threading.get_ident()
        self.warm = True

    def _check_owner(self):
        cur = threading.get_ident()
        self.call_threads.append(cur)
        if cur != self._owner:
            raise RuntimeError("no Stream(gpu, 0) in current thread")

    def classify(self, tokens):
        self._check_owner()
        return [{"token": t, "verdict": "MOT"} for t in tokens]

    def extract_pii(self, text):
        self._check_owner()
        return [{"type": "NOM", "text": "synthetic"}] if text else []


def test_worker_warms_and_runs_inference_on_its_own_thread():
    clf = _ThreadAffineClassifier()
    worker = InferenceWorker(clf)
    worker.start()
    worker.wait_ready(timeout=5)
    assert worker.warm is True

    # Submitting from the MAIN thread must still execute on the WORKER thread.
    res = worker.submit("classify", ["a", "b"], timeout=5)
    assert res == [{"token": "a", "verdict": "MOT"}, {"token": "b", "verdict": "MOT"}]

    worker_thread_id = clf._owner
    assert clf.call_threads == [worker_thread_id]  # ran on owner, not caller


def test_all_inference_stays_on_single_thread_under_concurrency():
    """The bug's failure mode: concurrent requests on different threads. Here
    many concurrent submit()s must ALL execute on the one worker thread."""
    clf = _ThreadAffineClassifier()
    worker = InferenceWorker(clf)
    worker.start()
    worker.wait_ready(timeout=5)

    def do_call(i):
        return worker.submit("classify", [f"tok{i}"], timeout=10)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(do_call, range(24)))

    assert len(results) == 24
    # Every inference ran on the single owning thread — no cross-thread call.
    assert set(clf.call_threads) == {clf._owner}
    assert len(clf.call_threads) == 24


class _RunningWorkerServer:
    """Start the real daemon handler backed by an InferenceWorker, over HTTP."""

    def __init__(self, classifier):
        self.worker = InferenceWorker(classifier)
        self.worker.start()
        self.worker.wait_ready(timeout=5)
        Handler = make_handler_class(self.worker)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


def test_http_classify_through_worker_does_not_fail_toward_nom():
    """Through the real do_POST path + InferenceWorker, a thread-affine
    classifier must NOT hit its except-branch — i.e. verdicts are the real
    MOT, not the all-NOM fail-safe. This is the bug, model-free."""
    with _RunningWorkerServer(_ThreadAffineClassifier()) as srv:
        data = json.dumps({"tokens": ["x", "y", "z"]}).encode()
        req = urllib.request.Request(srv.url("/classify"), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    verdicts = [r["verdict"] for r in body["results"]]
    assert verdicts == ["MOT", "MOT", "MOT"]  # would be all-NOM if bug present


def test_http_health_responsive_and_extract_pii_through_worker():
    with _RunningWorkerServer(_ThreadAffineClassifier()) as srv:
        with urllib.request.urlopen(srv.url("/health"), timeout=5) as resp:
            assert json.loads(resp.read())["warm"] is True
        data = json.dumps({"text": "Déclarant Dupont"}).encode()
        req = urllib.request.Request(srv.url("/extract_pii"), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    assert body["ok"] is True and isinstance(body["spans"], list)


# ===========================================================================
# Layer 2 — real-model end-to-end (skipped unless the gemma-env python with
# mlx_lm is present). The empirical acceptance criterion: the 5 canonical
# tokens through the daemon over HTTP, single-shot AND concurrent.
#
# CRITICAL: the daemon is spawned as a SUBPROCESS (exactly as production
# launchd runs it), NOT in-process. mlx-lm's MLX/Metal stream binds to the
# FIRST thread that touches it; if this pytest process imported mlx_lm on its
# main thread, that would poison the worker's thread-affinity and produce a
# spurious all-NOM even with the fix. Keeping all mlx contact inside the
# daemon subprocess (whose worker thread is the only mlx-touching thread) is
# both faithful to production and the correct way to test this.
# ===========================================================================

import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

_GEMMA_PY = Path.home() / ".bubble_shield" / "gemma-env" / "bin" / "python"


def _gemma_env_has_mlx() -> bool:
    if not _GEMMA_PY.exists():
        return False
    try:  # probe in the gemma-env, never importing mlx in THIS process
        subprocess.run([str(_GEMMA_PY), "-c", "import mlx_lm"],
                       check=True, capture_output=True, timeout=60)
        return True
    except Exception:
        return False


_HAS_GEMMA_ENV = _gemma_env_has_mlx()

_CANON_TOKENS = ["Dupont", "facture", "Déclarant", "montant", "Sébastien"]
_CANON_VERDICTS = ["NOM", "MOT", "MOT", "MOT", "NOM"]


def _find_free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(not _HAS_GEMMA_ENV,
                    reason="gemma-env python with mlx_lm not available")
def test_real_model_classify_verdicts_and_concurrency():  # pragma: no cover
    scripts = str(_SCRIPTS)
    port = _find_free_port()
    env = dict(os.environ, BUBBLE_SHIELD_GEMMA_IDLE="0")
    proc = subprocess.Popen(
        [str(_GEMMA_PY), "bubble_shield_gemmad.py", "--port", str(port)],
        cwd=scripts, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for the daemon to load + warm the model on its worker thread.
        deadline = time.time() + 300
        warm = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
                    if json.loads(r.read()).get("warm"):
                        warm = True
                        break
            except Exception:
                pass
            time.sleep(2)
        assert warm, "daemon did not warm within 300s"

        def call():
            data = json.dumps({"tokens": _CANON_TOKENS}).encode()
            req = urllib.request.Request(f"{base}/classify", data=data, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
                return [r["verdict"] for r in json.loads(resp.read())["results"]]

        # Single-shot: exact canonical verdicts THROUGH the daemon.
        assert call() == _CANON_VERDICTS

        # Concurrent: the original failure mode — must not crash, stay correct.
        with ThreadPoolExecutor(max_workers=6) as ex:
            all_results = list(ex.map(lambda _: call(), range(6)))
        assert all(v == _CANON_VERDICTS for v in all_results)

        # /health stays responsive (does not go through the inference queue).
        with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
            assert json.loads(r.read())["warm"] is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
