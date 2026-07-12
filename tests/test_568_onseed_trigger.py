import sys
from pathlib import Path

# the async on-seed trigger lives in the MCP daemon script
_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402


def test_fire_depollute_async_does_not_raise_and_is_nonblocking(monkeypatch):
    called = {}
    monkeypatch.setattr(mcp, "_run_depollute_pass", lambda: called.setdefault("ran", True))
    t = mcp._fire_depollute_async()   # returns the thread
    t.join(timeout=5)
    assert called.get("ran") is True
