"""
test_568_gemmad_classify.py — Task 3 (#568): /classify contract lock.

AMENDMENT NOTE: Task 2's fix already added real-HTTP (_RunningServer,
ThreadingHTTPServer + urllib) tests for POST /classify in
tests/test_568_gemmad_health.py, covering:
  - 200 + results for a single-token request
  - 500 fail-toward-masking on a raising classifier
  - GET /health 200, GET /nonexistent 404

That coverage uses the real do_POST path already — no brittle fake-socket
mock needed (the plan brief's Step 1 example is superseded; see amendment).

The one gap: no test asserts the exact /classify response SHAPE
{"results": [{"token": ..., "verdict": "NOM"|"MOT"}, ...]} for a
MULTI-token request with MIXED verdicts, over real HTTP. This file adds
only that, using the same _RunningServer pattern as test_568_gemmad_health.py.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from bubble_shield_gemmad import make_handler_class  # noqa: E402


class _MixedVerdictClassifier:
    """Returns NOM for capitalized-looking tokens, MOT otherwise — enough
    to exercise a mixed-verdict multi-token response without a real model."""

    warm = True
    model_id = "fake-gemma-mixed"

    def classify(self, tokens):
        return [
            {"token": t, "verdict": "NOM" if t[:1].isupper() else "MOT"}
            for t in tokens
        ]


class _RunningServer:
    """Start a real ThreadingHTTPServer on an OS-assigned ephemeral port.

    Mirrors tests/test_568_gemmad_health.py's helper exactly (kept local
    to this file rather than shared, to match that file's self-contained
    style).
    """

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


def test_real_http_post_classify_multi_token_shape_and_verdicts():
    """Locks the precise /classify response shape for a multi-token request:
    {"results": [{"token": ..., "verdict": "NOM"|"MOT"}, ...]}, one result
    per input token, order preserved, verdict constrained to NOM/MOT.
    """
    tokens = ["Déclarant", "Dupont", "le", "de"]
    with _RunningServer(_MixedVerdictClassifier()) as srv:
        data = json.dumps({"tokens": tokens}).encode()
        req = urllib.request.Request(srv.url("/classify"), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())

    assert set(body.keys()) == {"results"}
    results = body["results"]
    assert isinstance(results, list)
    assert len(results) == len(tokens)

    # Exact shape per result, order preserved, no extra keys.
    for entry, expected_token in zip(results, tokens):
        assert set(entry.keys()) == {"token", "verdict"}
        assert entry["token"] == expected_token
        assert entry["verdict"] in ("NOM", "MOT")

    # Mixed verdicts actually present (not a degenerate all-same-value case).
    verdicts = {r["verdict"] for r in results}
    assert verdicts == {"NOM", "MOT"}
    assert [r["verdict"] for r in results] == ["NOM", "NOM", "MOT", "MOT"]
