"""Shared mock NER daemon for test scripts.

Provides start_mock_daemon() which launches a minimal HTTP server that
responds to /health (200) and /detect (empty matches). Tests that need a
"daemon UP" environment import this and pass the returned port to rpc_calls.

Usage:
    from _test_mock_daemon import start_mock_daemon
    port, srv = start_mock_daemon()
    # ... run tests with nerd_port=port ...
    srv.shutdown()
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _MockNERHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/detect":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # drain body
            # Return empty matches — structured_ext does the work in these tests
            resp = json.dumps({"matches": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()


def start_mock_daemon():
    """Start mock NER daemon on a random port. Returns (port, server).
    Call server.shutdown() when done."""
    srv = HTTPServer(("127.0.0.1", 0), _MockNERHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return port, srv
