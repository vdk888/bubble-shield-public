"""
test_568_daemon_classify.py — Task 6 (#568): prod classify_fn, HTTP call to
the daemon, fail-toward-masking on any error.

Wedge fix (2026-07-11): daemon_classify now CHUNKS the token batch into
DEPOLLUTE_CHUNK_SIZE-token requests (default 8) so a single request can never
grind the single MLX worker for minutes and wedge the daemon. Each chunk is an
independent /classify_extract POST; the `results` lists are concatenated in
order. Per-chunk fail-toward-masking: if ONE chunk request errors / times out /
returns non-200, that chunk contributes NO verdicts (its tokens are simply
absent → depollute keeps them masked) and the remaining chunks still run.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import json

import bubble_shield.depollute as dp
from bubble_shield.depollute import DEPOLLUTE_CHUNK_SIZE, daemon_classify


def test_daemon_down_returns_empty_keeps_masked():
    # nothing listening on this port → must return [] (fail-toward-masking)
    assert daemon_classify(["Déclarant"], port=8, timeout=1) == []


def test_chunk_size_constant_is_eight():
    assert DEPOLLUTE_CHUNK_SIZE == 8


class _FakeResponse:
    """Minimal stand-in for the urlopen context-manager response."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _install_fake_urlopen(monkeypatch, handler):
    """Patch urllib.request.urlopen so daemon_classify never hits the network.

    `handler(tokens)` receives the token list decoded from each request body
    and returns either a `_FakeResponse` or raises — mimicking one chunk's HTTP
    round-trip. Records every request's token list in `seen`.
    """
    seen: list[list[str]] = []
    import urllib.request

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        tokens = body["tokens"]
        seen.append(tokens)
        return handler(tokens)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# --- Test 1: chunking splits ≤8 and concatenates in order ------------------
def test_daemon_classify_chunks_batch_into_le_8_and_concatenates(monkeypatch):
    # 30 synthetic tokens → 8 + 8 + 8 + 6 = exactly 4 chunk requests, each ≤ 8.
    tokens = [f"tok{i}" for i in range(30)]

    def handler(chunk_tokens):
        # Echo every token as MOT so we can verify order + completeness.
        return _FakeResponse(
            200, {"results": [{"token": t, "verdict": "MOT"} for t in chunk_tokens]}
        )

    seen = _install_fake_urlopen(monkeypatch, handler)
    results = daemon_classify(tokens, port=9999, timeout=5)

    # Exactly 4 requests, each carrying at most DEPOLLUTE_CHUNK_SIZE tokens.
    assert len(seen) == 4
    assert [len(chunk) for chunk in seen] == [8, 8, 8, 6]
    for chunk in seen:
        assert len(chunk) <= DEPOLLUTE_CHUNK_SIZE
    # The chunks partition the input in order (no reorder, no drop, no dup).
    assert seen[0] + seen[1] + seen[2] + seen[3] == tokens

    # All 30 verdicts returned, concatenated in original order.
    assert results == [{"token": t, "verdict": "MOT"} for t in tokens]


# --- Test 2: chunk fail-toward-masking (failed chunk absent, never MOT) -----
def test_daemon_classify_failed_chunk_keeps_tokens_masked(monkeypatch):
    # 30 tokens. A middle band of tokens is marked to fail (non-200): ANY chunk
    # request that contains one of them fails wholesale, so all tokens in a
    # touched chunk must be ABSENT from results (→ stay masked). This holds for
    # any DEPOLLUTE_CHUNK_SIZE — we compute the expected survivors from the same
    # chunking the code uses rather than hard-coding chunk boundaries.
    tokens = [f"tok{i}" for i in range(30)]
    fail_marked = set(tokens[12:24])

    def handler(chunk_tokens):
        if set(chunk_tokens) & fail_marked:
            # non-200 → fail-toward-masking for THIS chunk only
            return _FakeResponse(500, {"error": "boom"})
        return _FakeResponse(
            200, {"results": [{"token": t, "verdict": "MOT"} for t in chunk_tokens]}
        )

    _install_fake_urlopen(monkeypatch, handler)
    results = daemon_classify(tokens, port=9999, timeout=5)

    returned_tokens = {r["token"] for r in results}
    mot_tokens = {r["token"] for r in results if r["verdict"] == "MOT"}

    # Expected survivors = tokens in chunks that contain NO fail-marked token.
    survivors = set()
    for i in range(0, len(tokens), DEPOLLUTE_CHUNK_SIZE):
        chunk = tokens[i : i + DEPOLLUTE_CHUNK_SIZE]
        if not (set(chunk) & fail_marked):
            survivors |= set(chunk)

    # THE fail-toward-masking assertion: no fail-marked token un-masks, and every
    # token sharing a failed chunk is dropped too (whole-chunk fail).
    for t in fail_marked:
        assert t not in returned_tokens
        assert t not in mot_tokens
    assert returned_tokens == survivors
    assert mot_tokens == survivors


def test_daemon_classify_chunk_raising_keeps_tokens_masked(monkeypatch):
    # Same guarantee when a chunk's request RAISES (e.g. timeout / connection
    # reset) rather than returning non-200 — the failed chunk contributes no
    # verdicts, the rest still run.
    tokens = [f"tok{i}" for i in range(20)]
    fail_marked = set(tokens[12:20])

    def handler(chunk_tokens):
        if set(chunk_tokens) & fail_marked:
            raise TimeoutError("chunk timed out")
        return _FakeResponse(
            200, {"results": [{"token": t, "verdict": "MOT"} for t in chunk_tokens]}
        )

    _install_fake_urlopen(monkeypatch, handler)
    results = daemon_classify(tokens, port=9999, timeout=5)

    # Survivors = tokens in chunks with no fail-marked token (chunk-size agnostic).
    survivors = set()
    for i in range(0, len(tokens), DEPOLLUTE_CHUNK_SIZE):
        chunk = tokens[i : i + DEPOLLUTE_CHUNK_SIZE]
        if not (set(chunk) & fail_marked):
            survivors |= set(chunk)

    returned = {r["token"] for r in results}
    assert returned == survivors
    for t in fail_marked:
        assert t not in returned  # failed chunk stays masked


def test_daemon_classify_empty_tokens_makes_no_request(monkeypatch):
    seen = _install_fake_urlopen(monkeypatch, lambda t: _FakeResponse(200, {"results": []}))
    assert daemon_classify([], port=9999, timeout=5) == []
    assert seen == []


def test_daemon_classify_single_chunk_when_le_12(monkeypatch):
    tokens = [f"tok{i}" for i in range(5)]
    seen = _install_fake_urlopen(
        monkeypatch,
        lambda ct: _FakeResponse(
            200, {"results": [{"token": t, "verdict": "NOM"} for t in ct]}
        ),
    )
    results = daemon_classify(tokens, port=9999, timeout=5)
    assert len(seen) == 1  # single request, no needless chunking
    assert results == [{"token": t, "verdict": "NOM"} for t in tokens]
