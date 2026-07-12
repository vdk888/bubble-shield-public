"""
test_lazywarm_gemmad.py — Task 1: gemmad lazy warm (`--no-warm`).

The daemon must be able to run WITHOUT loading its ~4GB MLX model at boot: the
model loads on the FIRST inference request (on the worker thread), not at start.
A running-but-modelless daemon is HEALTHY (reachable) but reports warm:false.

Covered here (the 6 Task-1 tests):
  1. Lazy daemon: server reachable + /health warm:false BEFORE any request +
     the fake classifier's warm_up has NOT been called yet.
  2. First inference request triggers warm_up exactly once + correct verdict +
     /health then reports warm:true.
  3. Second request does NOT re-warm (loaded once).
  4. warm_up runs on the WORKER thread (the same thread inference runs on) —
     thread-affinity preserved.
  5. Eager path unchanged: without lazy, warm_up runs at start and /health is
     warm immediately (existing tests also cover this; asserted here too).
  6. Fail-toward: if warm_up raises on the first job, the request errors (no
     fabricated verdict), the worker does not crash, and a later request retries
     the load.

Uses a FAKE classifier with a spy on warm_up — NO real MLX model is loaded.
Mirrors the InferenceWorker / _RunningServer conventions of
test_gemmad_threading.py and test_568_gemmad_health.py.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from bubble_shield_gemmad import (  # noqa: E402
    InferenceWorker,
    make_handler_class,
)


class _SpyClassifier:
    """Fake MLX classifier that records warm_up calls and their thread ident.

    Starts NOT warm. warm_up() flips warm=True, counts the calls, and records
    the calling thread's ident and the classify thread ident so tests can prove
    (a) lazy defers the load, (b) it loads exactly once, and (c) it loads on the
    same (worker) thread inference runs on.
    """

    model_id = "fake-spy"

    def __init__(self):
        self.warm = False
        self.warm_up_calls = 0
        self.warm_up_thread = None
        self.classify_threads = []

    def warm_up(self):
        self.warm_up_calls += 1
        self.warm_up_thread = threading.get_ident()
        self.warm = True

    def classify(self, tokens):
        self.classify_threads.append(threading.get_ident())
        return [{"token": t, "verdict": "NOM"} for t in tokens]

    def extract_pii(self, text):
        return [{"type": "NOM", "text": "synthetic"}] if text else []


class _WarmFailsOnceClassifier:
    """warm_up() raises on the FIRST call, then succeeds. Proves fail-toward:
    the first job errors (no fabricated verdict) and a later job retries the
    load and succeeds — the worker never crashes."""

    model_id = "fake-warmfail"

    def __init__(self):
        self.warm = False
        self.warm_up_calls = 0

    def warm_up(self):
        self.warm_up_calls += 1
        if self.warm_up_calls == 1:
            raise RuntimeError("model load boom")
        self.warm = True

    def classify(self, tokens):
        return [{"token": t, "verdict": "MOT"} for t in tokens]


class _RunningWorkerServer:
    """Start the real daemon handler backed by an InferenceWorker over HTTP.

    `lazy` selects the deferred-warm path (the `--no-warm` production path):
    the server opens WITHOUT blocking on the model load.
    """

    def __init__(self, classifier, lazy):
        self.worker = InferenceWorker(classifier, lazy=lazy)
        self.worker.start()
        if not lazy:
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

    def get_health(self):
        with urllib.request.urlopen(self.url("/health"), timeout=5) as resp:
            return json.loads(resp.read())

    def post_classify(self, tokens):
        data = json.dumps({"tokens": tokens}).encode()
        req = urllib.request.Request(self.url("/classify"), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())


# ---------------------------------------------------------------------------
# Test 1 — lazy: reachable + warm:false + warm_up NOT called before a request.
# ---------------------------------------------------------------------------
def test_lazy_daemon_reachable_and_not_warm_before_any_request():
    clf = _SpyClassifier()
    with _RunningWorkerServer(clf, lazy=True) as srv:
        health = srv.get_health()
        # Reachable and healthy, but the model has NOT been loaded yet.
        assert health["warm"] is False
        assert clf.warm_up_calls == 0


# ---------------------------------------------------------------------------
# Test 2 — first request triggers warm_up once + correct verdict + warm:true.
# ---------------------------------------------------------------------------
def test_first_request_triggers_warmup_once_and_flips_health_warm():
    clf = _SpyClassifier()
    with _RunningWorkerServer(clf, lazy=True) as srv:
        assert clf.warm_up_calls == 0
        status, body = srv.post_classify(["Dupont", "facture"])
        assert status == 200
        assert body["results"] == [
            {"token": "Dupont", "verdict": "NOM"},
            {"token": "facture", "verdict": "NOM"},
        ]
        assert clf.warm_up_calls == 1
        # /health now reports the real, loaded state.
        assert srv.get_health()["warm"] is True


# ---------------------------------------------------------------------------
# Test 3 — second request does NOT re-warm (loaded once).
# ---------------------------------------------------------------------------
def test_second_request_does_not_rewarm():
    clf = _SpyClassifier()
    with _RunningWorkerServer(clf, lazy=True) as srv:
        srv.post_classify(["a"])
        srv.post_classify(["b"])
        assert clf.warm_up_calls == 1


# ---------------------------------------------------------------------------
# Test 4 — warm_up runs on the WORKER thread (thread-affinity preserved).
# ---------------------------------------------------------------------------
def test_warmup_runs_on_the_worker_thread():
    clf = _SpyClassifier()
    with _RunningWorkerServer(clf, lazy=True) as srv:
        srv.post_classify(["x"])
    # The model must load on the SAME thread every generate() runs on, else the
    # MLX/Metal stream affinity is violated. Assert warm_up's thread == the
    # inference call's thread, and that inference ran on exactly one thread.
    assert clf.warm_up_thread is not None
    assert clf.classify_threads == [clf.warm_up_thread]


# ---------------------------------------------------------------------------
# Test 5 — eager path unchanged: warm at start, /health warm immediately.
# ---------------------------------------------------------------------------
def test_eager_path_warms_at_start_and_is_warm_immediately():
    clf = _SpyClassifier()
    with _RunningWorkerServer(clf, lazy=False) as srv:
        # Warm happened at start (before any request), health is warm at once.
        assert clf.warm_up_calls == 1
        assert srv.get_health()["warm"] is True
        # Inference still works and does not re-warm.
        srv.post_classify(["y"])
        assert clf.warm_up_calls == 1


# ---------------------------------------------------------------------------
# Test 6 — fail-toward: warm_up raises on the first job -> request errors (no
# fabricated verdict), worker survives, a later request retries + succeeds.
# ---------------------------------------------------------------------------
def test_lazy_warm_failure_errors_the_request_and_worker_survives():
    clf = _WarmFailsOnceClassifier()
    with _RunningWorkerServer(clf, lazy=True) as srv:
        # First request: lazy warm raises -> 500 (fail-toward), NO fabricated
        # verdict returned to the caller.
        try:
            srv.post_classify(["Dupont"])
            assert False, "expected HTTPError (500) when lazy warm fails"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            body = json.loads(e.read())
            assert "results" not in body  # never fabricate a verdict
            assert body["error"] == "RuntimeError"
        # Health must NOT claim warm after a failed load.
        assert srv.get_health()["warm"] is False
        # Worker did not crash: a later request retries the load and succeeds.
        status, body = srv.post_classify(["Dupont"])
        assert status == 200
        assert body["results"] == [{"token": "Dupont", "verdict": "MOT"}]
        assert clf.warm_up_calls == 2  # first (failed) + retry (ok)


# ---------------------------------------------------------------------------
# main() --no-warm wiring — the flag must exist and select the lazy worker.
# We patch out the real classifier import + the blocking serve to assert main()
# does NOT block on model warm and constructs a lazy worker.
# ---------------------------------------------------------------------------
def test_main_no_warm_builds_lazy_worker_without_blocking_on_model(monkeypatch):
    import bubble_shield_gemmad as g

    created = {}

    real_worker_cls = g.InferenceWorker

    class _CapturingWorker(real_worker_cls):
        def __init__(self, classifier, lazy=False):
            created["lazy"] = lazy
            super().__init__(classifier, lazy=lazy)

        def wait_ready(self, timeout=None):
            created["wait_ready_called"] = True
            return super().wait_ready(timeout)

    # Fake classifier module so main()'s `from gemma_classifier import ...` works.
    fake_mod = type(sys)("gemma_classifier")
    fake_mod.GemmaClassifier = _SpyClassifier
    monkeypatch.setitem(sys.modules, "gemma_classifier", fake_mod)
    monkeypatch.setattr(g, "InferenceWorker", _CapturingWorker)

    # Stop main() before it blocks in serve_forever — raise from the server ctor.
    class _StopHere(Exception):
        pass

    def _fake_server(*a, **k):
        raise _StopHere

    monkeypatch.setattr(g, "ThreadingHTTPServer", _fake_server)

    with pytest.raises(_StopHere):
        g.main(["--no-warm", "--port", "0"])

    assert created.get("lazy") is True
    # Lazy path must NOT block on model warm before opening the port.
    assert created.get("wait_ready_called") is not True


def test_main_eager_default_blocks_on_warm(monkeypatch):
    import bubble_shield_gemmad as g

    created = {}
    real_worker_cls = g.InferenceWorker

    class _CapturingWorker(real_worker_cls):
        def __init__(self, classifier, lazy=False):
            created["lazy"] = lazy
            super().__init__(classifier, lazy=lazy)

        def wait_ready(self, timeout=None):
            created["wait_ready_called"] = True
            return super().wait_ready(timeout)

    fake_mod = type(sys)("gemma_classifier")
    fake_mod.GemmaClassifier = _SpyClassifier
    monkeypatch.setitem(sys.modules, "gemma_classifier", fake_mod)
    monkeypatch.setattr(g, "InferenceWorker", _CapturingWorker)

    class _StopHere(Exception):
        pass

    def _fake_server(*a, **k):
        raise _StopHere

    monkeypatch.setattr(g, "ThreadingHTTPServer", _fake_server)

    with pytest.raises(_StopHere):
        g.main(["--port", "0"])

    assert created.get("lazy") is False
    # Eager path MUST block on warm (existing behaviour).
    assert created.get("wait_ready_called") is True
