"""
test_589c_depollute_extract_wiring.py — depollute wired to the extract judge.

Two seams:

1. daemon_classify now POSTs to /classify_extract (not /classify). Proven by a
   tiny real HTTP server that ONLY answers /classify_extract — if daemon_classify
   still hit /classify it would 404 → [] and the assertion would fail.

2. depollute_gazetteer, given a fake extract-judge classify_fn, un-masks entries
   the judge returns "MOT" for and keeps "NOM" entries masked. The orchestration
   contract is unchanged — this just proves the new judge shape plugs in.

All PII in this file is SYNTHETIC. No real client names anywhere.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bubble_shield import known_pii_store as kps
from bubble_shield.depollute import daemon_classify, depollute_gazetteer


class _ExtractOnlyHandler(BaseHTTPRequestHandler):
    """Answers ONLY /classify_extract with an echo verdict; 404 for anything
    else (so hitting the old /classify path would fail the test)."""

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        if self.path != "/classify_extract":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        tokens = req.get("tokens", [])
        # "conseil" → MOT (un-mask), everything else → NOM (keep masked)
        results = [
            {"token": t, "verdict": "MOT" if t == "conseil" else "NOM"}
            for t in tokens
        ]
        body = json.dumps({"results": results}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_daemon_classify_targets_classify_extract_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ExtractOnlyHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        results = daemon_classify(["conseil", "MARTINVILLE"], port=port, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)

    assert results == [
        {"token": "conseil", "verdict": "MOT"},
        {"token": "MARTINVILLE", "verdict": "NOM"},
    ]


def _seed_gaz(tmp_path, values):
    p = tmp_path / "gaz.json"
    for v in values:
        kps.add_confirmed_pii(v, "NOM", path=p)
    return p


def test_extract_judge_fake_wired_through_depollute_gazetteer(tmp_path):
    # A capitalized multi-word boilerplate FP that the OLD single-token judge
    # would keep masked. The extract-judge fake returns zero-span→MOT for it,
    # so it un-masks; a real-name entry returns a span→NOM and stays masked.
    gaz = _seed_gaz(tmp_path, ["Cadre De Notre Activite De Conseil", "Martinville"])
    q = tmp_path / "queue.json"

    def fake_extract_judge(tokens):
        # mirrors classify_via_extract's output shape
        span = {"Martinville"}  # only Martinville yields a PII span
        return [
            {"token": t, "verdict": "NOM" if t in span else "MOT"}
            for t in tokens
        ]

    res = depollute_gazetteer(fake_extract_judge, gaz_path=gaz, queue_path=q)

    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "Cadre De Notre Activite De Conseil" not in remaining  # un-masked
    assert "Martinville" in remaining  # PII span → stays masked

    assert res["unmasked"] == ["Cadre De Notre Activite De Conseil"]
    assert res["kept"] == ["Martinville"]
