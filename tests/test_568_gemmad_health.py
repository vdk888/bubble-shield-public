"""
test_568_gemmad_health.py — Task 2 (#568): Gemma classifier daemon — /health.

Mirrors the existing GLiNER nerd daemon's /health contract but for the new
bubble_shield_gemmad daemon (mlx-community/gemma-3n-E4B-it-lm-4bit).

Tests the request-handler logic WITHOUT loading the real model — the
classifier object is mocked (real model wiring is Task 4).

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

# Allow importing the daemon script (mirror existing daemon tests, e.g. #348).
_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from bubble_shield_gemmad import make_handler_class  # noqa: E402


class _FakeClassifier:
    warm = True
    model_id = "fake-gemma"

    def classify(self, tokens):
        return [{"token": t, "verdict": "MOT"} for t in tokens]


def test_health_returns_ok_when_warm():
    Handler = make_handler_class(_FakeClassifier())
    body, status = Handler._health_payload()
    assert status == 200
    assert body["ok"] is True and body["warm"] is True and body["model"] == "fake-gemma"


class _ColdClassifier:
    warm = False
    model_id = "fake-gemma-cold"

    def classify(self, tokens):
        return [{"token": t, "verdict": "MOT"} for t in tokens]


def test_health_reports_not_warm_when_classifier_not_ready():
    Handler = make_handler_class(_ColdClassifier())
    body, status = Handler._health_payload()
    assert status == 200
    assert body["ok"] is False and body["warm"] is False
    assert body["model"] == "fake-gemma-cold"


# ===========================================================================
# Real HTTP routing test — starts the actual server (do_GET/do_POST), makes
# real requests over a socket. Verifies the routing + 500 fail-toward-masking
# path, which Task 3 will build /classify on top of.
# ===========================================================================


class _RaisingClassifier:
    warm = True
    model_id = "fake-gemma-raising"

    def classify(self, tokens):
        raise RuntimeError("boom")


class _RunningServer:
    """Start a real ThreadingHTTPServer on an OS-assigned ephemeral port."""

    def __init__(self, classifier):
        Handler = make_handler_class(classifier)
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


def test_real_http_get_health_returns_200_and_json():
    with _RunningServer(_FakeClassifier()) as srv:
        with urllib.request.urlopen(srv.url("/health"), timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["ok"] is True
            assert body["warm"] is True
            assert body["model"] == "fake-gemma"


def test_real_http_post_classify_returns_200_and_results():
    with _RunningServer(_FakeClassifier()) as srv:
        data = json.dumps({"tokens": ["x"]}).encode()
        req = urllib.request.Request(srv.url("/classify"), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["results"] == [{"token": "x", "verdict": "MOT"}]


def test_real_http_post_classify_raising_classifier_returns_500():
    # fail-toward-masking: caller treats any non-200 as "keep entries masked"
    with _RunningServer(_RaisingClassifier()) as srv:
        data = json.dumps({"tokens": ["x"]}).encode()
        req = urllib.request.Request(srv.url("/classify"), data=data, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTPError for 500 response"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            body = json.loads(e.read())
            assert body["error"] == "RuntimeError"


def test_real_http_get_unknown_path_returns_404():
    with _RunningServer(_FakeClassifier()) as srv:
        try:
            urllib.request.urlopen(srv.url("/nonexistent"), timeout=5)
            assert False, "expected HTTPError for 404 response"
        except urllib.error.HTTPError as e:
            assert e.code == 404
