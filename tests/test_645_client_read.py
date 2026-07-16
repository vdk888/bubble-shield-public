"""
test_645_client_read.py — #645 mini tier: the CLIENT read branch.

Joris-decided degraded mode: the client must NEVER be blocked by the mini —
mini down / MISS / any error → serve raw local extract (accepted leak) and keep
working. No raw document content ever travels to the mini (hash + rel_path only).

Behavior matrix under test (spec 2026-07-16-645-mini-http-api-design.md):
  local HIT                 → serve local shadow (mini not contacted)
  local MISS + remote HIT   → serve remote shadow + cache it locally
  local MISS + remote MISS  → fire index_request (non-blocking) + serve raw
  local MISS + mini DOWN    → serve raw + local mark_pending
  no mini_url configured    → exact pre-#645 behavior (raw + mark_pending)

Synthetic values only. The "mini" is an in-thread stub HTTP server.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "scripts"))
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))

import pytest

import bubble_shield_mcp as mcp


class _StubMini:
    """Canned mini: records requests, serves a configurable shadow map."""
    def __init__(self):
        self.shadows = {}
        self.index_requests = []
        self.auth_seen = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.auth_seen.append(self.headers.get("Authorization", ""))
                h = self.path.rsplit("/", 1)[-1]
                if h in stub.shadows:
                    self._send(200, {"clean_text": stub.shadows[h]})
                else:
                    self._send(404, {"status": "miss"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                stub.index_requests.append(json.loads(self.rfile.read(n).decode()))
                self._send(202, {"status": "queued"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    docs = tmp_path / "docs"
    docs.mkdir()
    f = docs / "a.txt"
    f.write_text("contenu du dossier client", encoding="utf-8")
    return {"home": tmp_path, "file": f, "docs": docs}


@pytest.fixture()
def mini(env, monkeypatch):
    stub = _StubMini()
    monkeypatch.setattr(mcp, "_mini_config", lambda: {
        "mini_url": stub.url, "mini_token": "tok645",
        "protected_roots": [str(env["docs"])]})
    yield stub
    stub.stop()


def _hash(p):
    from bubble_shield import shadow_store
    return shadow_store.content_hash(p)


def test_no_mini_config_is_pre645_behavior(env, monkeypatch):
    monkeypatch.setattr(mcp, "_mini_config", lambda: {})
    from bubble_shield import shadow_store as ss
    out = mcp._read_with_shadow(str(env["file"]))
    assert out == "contenu du dossier client"        # raw (B1)
    assert str(env["file"].resolve()) in ss.pending_files()


def test_local_hit_short_circuits_mini(env, mini):
    from bubble_shield import shadow_store as ss
    ss.put_shadow(_hash(env["file"]), "⟦NOM_0001⟧ masqué", src_path=str(env["file"]))
    out = mcp._read_with_shadow(str(env["file"]))
    assert out == "⟦NOM_0001⟧ masqué"
    assert mini.auth_seen == []                      # mini never contacted


def test_remote_hit_is_served_and_cached(env, mini):
    from bubble_shield import shadow_store as ss
    h = _hash(env["file"])
    mini.shadows[h] = "⟦NOM_0002⟧ depuis le mini"
    out = mcp._read_with_shadow(str(env["file"]))
    assert out == "⟦NOM_0002⟧ depuis le mini"
    assert ss.get_shadow(h) == "⟦NOM_0002⟧ depuis le mini"   # cached locally
    assert mini.auth_seen and mini.auth_seen[0] == "Bearer tok645"


def test_remote_miss_serves_raw_and_fires_index_request(env, mini):
    out = mcp._read_with_shadow(str(env["file"]))
    assert out == "contenu du dossier client"        # accepted leak (Joris)
    assert len(mini.index_requests) == 1
    req = mini.index_requests[0]
    assert req["content_hash"] == _hash(env["file"])
    assert req["rel_path"] == "a.txt"                # relative to protected root
    # no raw content in the request
    assert "contenu" not in json.dumps(req)


def test_mini_down_serves_raw_and_marks_pending(env, monkeypatch):
    from bubble_shield import shadow_store as ss
    monkeypatch.setattr(mcp, "_mini_config", lambda: {
        "mini_url": "http://127.0.0.1:1", "mini_token": "tok645",   # nothing listens
        "protected_roots": [str(env["docs"])]})
    out = mcp._read_with_shadow(str(env["file"]))
    assert out == "contenu du dossier client"        # never blocked
    assert str(env["file"].resolve()) in ss.pending_files()
