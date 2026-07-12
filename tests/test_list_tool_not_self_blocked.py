"""Regression: the sanctioned `bubble_shield_list` tool must NOT self-block.

`bubble_shield_list` (added v1.20.2) takes a `folder` argument pointing at a
protected folder — its whole job is to list a protected folder's (masked)
filenames so the agent can discover files. But it was NOT in the guard's
own-tool allow-list (`_OWN_MCP_TOOL_SUFFIXES`), so the guard's generic mcp__*
path-scan denied it on every protected path → the sanctioned discovery tool was
unusable, forcing the operator to paste paths by hand (v1.20.5 fix).

This test drives the guard hook and asserts `bubble_shield_list` on a protected
folder is ALLOWED, while a NON-sanctioned mcp file tool on the same path is still
DENIED (the exemption must stay narrow — no over-exempt).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "plugin" / "bubble-shield" / "scripts" / "guard.py"


def _decide(tool_name, tool_input, cfg):
    ev = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "cwd": "/tmp"})
    out = subprocess.run(
        [sys.executable, str(GUARD)],
        input=ev, capture_output=True, text=True,
        env={**os.environ, "BUBBLE_SHIELD_GUARD_CONFIG": cfg},
    ).stdout
    return "deny" if "deny" in out else "allow"


def _protected_folder():
    d = tempfile.mkdtemp()
    prot = os.path.join(d, "client")
    os.makedirs(prot)
    open(os.path.join(prot, ".bubble-shield.json"), "w").write("{}")
    cfg = os.path.join(d, "cfg.json")
    json.dump({"protected_folders": [prot]}, open(cfg, "w"))
    return prot, cfg


def test_bubble_shield_list_is_allowed_on_protected_folder():
    prot, cfg = _protected_folder()
    # opaque-prefixed form (how Cowork names it) must be exempt by suffix
    assert _decide(
        "mcp__plugin_bubble-shield_bubble_shield__bubble_shield_list",
        {"folder": prot}, cfg,
    ) == "allow"
    # bare name too
    assert _decide("bubble_shield_list", {"folder": prot}, cfg) == "allow"


def test_read_still_allowed_and_generic_mcp_still_denied():
    prot, cfg = _protected_folder()
    # sanctioned read still works
    assert _decide("mcp__x__bubble_shield_read", {"path": prot + "/f.pdf"}, cfg) == "allow"
    # a NON-sanctioned mcp file tool must STILL be denied — the exemption is narrow
    assert _decide("mcp__filesystem__read_file", {"path": prot + "/f.pdf"}, cfg) == "deny"
    # a look-alike that merely ends differently must not be exempted
    assert _decide("mcp__evil__bubble_shield_list_and_dump", {"path": prot + "/f.pdf"}, cfg) == "deny"
