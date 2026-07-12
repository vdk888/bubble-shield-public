"""
test_589c_gemmad_classify_extract.py — daemon POST /classify_extract endpoint.

The extract-based de-pollution judge is served over HTTP by the same daemon,
mirroring /classify: POST {"tokens":[...]} -> {"results":[{"token","verdict"}]}
routed through the InferenceWorker (op="classify_via_extract"), and 500 +
fail-toward-masking on any error (caller keeps entries masked).

Uses a FAKE classifier — no real MLX model is loaded. Mirrors the
_RunningServer pattern in test_568_gemmad_health.py / test_568_gemmad_classify.py.

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

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from bubble_shield_gemmad import make_handler_class  # noqa: E402


class _ExtractJudgeClassifier:
    """Fake extract-judge: MOT for known labels, NOM otherwise — enough to
    exercise a mixed-verdict multi-token /classify_extract response without a
    real model."""

    warm = True
    model_id = "fake-gemma-extract"

    _MOT = {"cadre de notre activité de Conseil", "déclarant 1"}

    def classify_via_extract(self, tokens):
        return [
            {"token": t, "verdict": "MOT" if t in self._MOT else "NOM"}
            for t in tokens
        ]


class _RaisingExtractClassifier:
    warm = True
    model_id = "fake-gemma-extract-raising"

    def classify_via_extract(self, tokens):
        raise RuntimeError("boom")


class _RunningServer:
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


def test_classify_extract_multi_token_shape_and_verdicts():
    """Locks the /classify_extract response shape for a multi-token request:
    {"results": [{"token": ..., "verdict": "NOM"|"MOT"}, ...]}, one per input,
    order preserved."""
    tokens = ["cadre de notre activité de Conseil", "MARTINVILLE", "déclarant 1"]
    with _RunningServer(_ExtractJudgeClassifier()) as srv:
        data = json.dumps({"tokens": tokens}).encode()
        req = urllib.request.Request(
            srv.url("/classify_extract"), data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())

    assert set(body.keys()) == {"results"}
    results = body["results"]
    assert len(results) == len(tokens)
    for entry, expected_token in zip(results, tokens):
        assert set(entry.keys()) == {"token", "verdict"}
        assert entry["token"] == expected_token
        assert entry["verdict"] in ("NOM", "MOT")
    assert [r["verdict"] for r in results] == ["MOT", "NOM", "MOT"]


def test_classify_extract_raising_classifier_returns_500():
    # fail-toward-masking: caller treats any non-200 as "keep entries masked"
    with _RunningServer(_RaisingExtractClassifier()) as srv:
        data = json.dumps({"tokens": ["x"]}).encode()
        req = urllib.request.Request(
            srv.url("/classify_extract"), data=data, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTPError for 500 response"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            body = json.loads(e.read())
            assert body["error"] == "RuntimeError"
