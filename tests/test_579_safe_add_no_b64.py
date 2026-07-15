"""
tests/test_579_safe_add_no_b64.py — /safe/add drops the reversible-base64 param (#579).

/safe/add accepted `value_b64` (base64 of the raw value in a hidden DOM field) — the
SAME reversible-DOM pattern #346 removed from /gazetteer/remove. base64 is an ENCODING,
not encryption: a devtools atob() recovers the cleartext. The route was DORMANT (no
template posted the b64 field), so the b64 path was stripped. The route still works with
a raw `value` (a direct caller), and typed-confirm is unchanged.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _client():
    from fastapi.testclient import TestClient
    from webapp.app import app
    return TestClient(app)


def test_safe_add_signature_has_no_value_b64():
    """The b64 param must be GONE from the route signature (not just unused)."""
    from webapp.app import safe_add
    params = set(inspect.signature(safe_add).parameters)
    assert "value_b64" not in params, "the reversible-base64 param must be removed (#579)"
    assert "value" in params and "confirm" in params, "raw value + confirm still accepted"


def test_safe_add_source_has_no_b64_decode():
    """No base64 decode of a form value survives in the route body."""
    from webapp.app import safe_add
    src = inspect.getsource(safe_add)
    assert "urlsafe_b64decode" not in src, "no reversible base64 decode of a DOM value"
    assert "value_b64" not in src.replace("#579", "").replace("value_b64`` param", ""), \
        "value_b64 must not be referenced except in the removal note"


def test_safe_add_still_works_with_raw_value(monkeypatch, tmp_path):
    """Regression: the route still adds a raw value to the safe-list with confirm=SUR."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    added = {}
    import bubble_shield.safe_words as sw
    monkeypatch.setattr(sw, "add_safe", lambda v: added.__setitem__("v", v))
    r = _client().post("/safe/add", data={"value": "Patrimoine", "confirm": "SUR"},
                       follow_redirects=False)
    assert r.status_code == 303
    assert added.get("v") == "Patrimoine", "raw-value add path must still work"


def test_safe_add_requires_confirm(monkeypatch, tmp_path):
    """Typed-confirm gate unchanged: no confirm → no add."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    called = {"n": 0}
    import bubble_shield.safe_words as sw
    monkeypatch.setattr(sw, "add_safe", lambda v: called.__setitem__("n", called["n"] + 1))
    r = _client().post("/safe/add", data={"value": "X"}, follow_redirects=False)
    assert r.status_code == 303
    assert called["n"] == 0, "without confirm=SUR, nothing is added"
