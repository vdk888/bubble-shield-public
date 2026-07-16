"""
test_645_minid.py — #645 mini tier: the read-distribution HTTP daemon.

Joris-decided design (2026-07-16): live HTTP API on the mini over Tailscale;
degraded mode = clients serve raw (accepted leak) — so the daemon itself only
ever serves MASKED shadows or metadata. No raw document content in or out.

Endpoints under test (spec: docs/superpowers/specs/2026-07-16-645-mini-http-api-design.md):
  GET  /health                → open (no token), {ok, version, shadow_count}
  GET  /shadow/<content_hash> → Bearer token required; HIT → {clean_text} with
                                the gazetteer exact-string net applied
                                server-side; MISS → 404 {"status":"miss"}
  POST /index_request         → Bearer token required; {content_hash, rel_path}
                                resolved under --root; queues mark_pending on
                                the MINI's store; 202. Traversal → 400.

Synthetic values only. Server runs in-thread on 127.0.0.1 with a tmp store.
"""
import json
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "scripts"))

import pytest

TOKEN = "test-token-645"


@pytest.fixture()
def mini(tmp_path, monkeypatch):
    """A running minid on an ephemeral port with an isolated store."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    root = tmp_path / "vault-root"
    root.mkdir()
    import bubble_shield_minid as minid
    srv = minid.make_server(host="127.0.0.1", port=0, token=TOKEN, root=str(root))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    yield {"port": port, "root": root, "minid": minid}
    srv.shutdown()


def _req(port, path, *, method="GET", body=None, token=TOKEN):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def test_health_is_open_and_reports_count(mini):
    from bubble_shield import shadow_store as ss
    ss.put_shadow("h1", "s1", src_path="/x/a.txt")
    code, body = _req(mini["port"], "/health", token=None)
    assert code == 200
    assert body["ok"] is True
    assert body["shadow_count"] == 1


def test_shadow_requires_token(mini):
    code, _ = _req(mini["port"], "/shadow/deadbeef", token=None)
    assert code == 401
    code, _ = _req(mini["port"], "/shadow/deadbeef", token="wrong")
    assert code == 401


def test_shadow_hit_serves_masked_text(mini):
    from bubble_shield import shadow_store as ss
    ss.put_shadow("cafe01", "dossier de ⟦NOM_0001⟧", src_path="/x/a.txt")
    code, body = _req(mini["port"], "/shadow/cafe01")
    assert code == 200
    assert body["clean_text"] == "dossier de ⟦NOM_0001⟧"


def test_shadow_hit_applies_gazetteer_net(mini):
    # A gazetteer-confirmed name sitting IN CLEAR in a stored shadow must be
    # masked before it leaves the mini (same belt-and-suspenders as local reads).
    from bubble_shield import shadow_store as ss
    from bubble_shield.known_pii_store import add_confirmed_pii
    ss.put_shadow("cafe02", "le client Vaugirond a signé", src_path="/x/b.txt")
    add_confirmed_pii("Vaugirond", "NOM")
    code, body = _req(mini["port"], "/shadow/cafe02")
    assert code == 200
    assert "Vaugirond" not in body["clean_text"]


def test_shadow_miss_is_404(mini):
    code, body = _req(mini["port"], "/shadow/" + "0" * 64)
    assert code == 404
    assert body["status"] == "miss"


def test_index_request_queues_pending_under_root(mini):
    from bubble_shield import shadow_store as ss
    (mini["root"] / "sub").mkdir()
    f = mini["root"] / "sub" / "doc.txt"
    f.write_text("x", encoding="utf-8")
    code, body = _req(mini["port"], "/index_request", method="POST",
                      body={"content_hash": "ab" * 32, "rel_path": "sub/doc.txt"})
    assert code == 202
    assert str(f.resolve()) in ss.pending_files()


def test_index_request_not_yet_synced_file_still_queues(mini):
    # Dropbox lag: the rel_path doesn't exist on the mini yet → still 202,
    # queued; the sweep picks it up once the file lands.
    from bubble_shield import shadow_store as ss
    code, _ = _req(mini["port"], "/index_request", method="POST",
                   body={"content_hash": "cd" * 32, "rel_path": "later/doc.pdf"})
    assert code == 202
    assert any(p.endswith("later/doc.pdf") for p in ss.pending_files())


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "a/../../x"])
def test_index_request_rejects_traversal(mini, bad):
    code, _ = _req(mini["port"], "/index_request", method="POST",
                   body={"content_hash": "ef" * 32, "rel_path": bad})
    assert code == 400


def test_index_request_requires_token(mini):
    code, _ = _req(mini["port"], "/index_request", method="POST",
                   body={"content_hash": "ab" * 32, "rel_path": "x.txt"}, token=None)
    assert code == 401


def test_no_raw_content_endpoints_exist(mini):
    # The API surface must never accept or return raw document content.
    code, _ = _req(mini["port"], "/extract", method="POST", body={"text": "x"})
    assert code == 404
