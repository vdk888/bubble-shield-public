import os, json, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent

def _list_tools(env):
    # tools/list JSON-RPC round-trip against the MCP server
    proc = subprocess.run([sys.executable, str(HERE/"bubble_shield_mcp.py")],
        input=json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list"})+"\n",
        capture_output=True, text=True, env={**os.environ, **env}, timeout=30)
    return proc.stdout

def test_mail_tools_absent_by_default():
    out = _list_tools({})   # no BUBBLE_SHIELD_ENABLE_MAIL
    assert "bubble_shield_mail_read" not in out
    assert "bubble_shield_mail_apply" not in out

def test_mail_tools_present_when_flag_on():
    out = _list_tools({"BUBBLE_SHIELD_ENABLE_MAIL": "1"})
    assert "bubble_shield_mail_read" in out
