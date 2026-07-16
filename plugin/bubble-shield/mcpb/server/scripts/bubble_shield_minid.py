#!/usr/bin/env python3
"""Bubble Shield — mini-tier read-distribution daemon (bubble-shield-minid). #645

The Mac mini is the SOLE indexer of the shared vault; client Macs read masked
shadows over the tailnet instead of running models locally. This daemon is the
distribution mechanism (Joris-decided 2026-07-16: live HTTP API over Tailscale;
degraded mode on the CLIENT = serve raw, accepted leak — so this server never
needs a fail-closed mode: it either has the shadow or says "miss").

SECURITY PROPERTIES
-------------------
- Serves ONLY masked shadows + metadata. No endpoint accepts or returns raw
  document content — the mini gets documents via Dropbox sync, never via HTTP.
- Binds the given host ONLY (the mini's Tailscale IP in production — never
  0.0.0.0). Tailscale provides WireGuard encryption + device auth underneath.
- Bearer token on every endpoint except /health (constant-time compare). The
  token is auto-generated at $BUBBLE_SHIELD_HOME/mini_token (chmod 600) on
  first run; the operator copies it into each client's config once.
- /index_request resolves rel_path STRICTLY under --root (traversal → 400).

  GET  /health                 -> {"ok": true, "version": ..., "shadow_count": N}
  GET  /shadow/<content_hash>  -> {"clean_text": ...} | 404 {"status": "miss"}
  POST /index_request          -> {"content_hash": ..., "rel_path": ...} -> 202

Run (mini):  python3 bubble_shield_minid.py --host <tailscale-ip> --root <vault> [--port 8377]
"""
from __future__ import annotations
import argparse
import hmac
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8377   # avoids 8723 (nerd) and 8724 (gemmad)
VERSION = "1"         # API version, not the plugin version


def _vendor() -> Path:
    return Path(__file__).resolve().parent.parent / "vendor"


def _token_path() -> Path:
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
    return home / "mini_token"


def load_or_create_token() -> str:
    """Auto-generate the shared bearer token on first run (chmod 600)."""
    p = _token_path()
    if p.is_file():
        tok = p.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p.write_text(tok + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return tok


def _shadow_count() -> int:
    from bubble_shield import shadow_store
    conn = shadow_store.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM shadows").fetchone()[0]
    finally:
        conn.close()
        shadow_store._drop_working_copy()


def _serve_shadow(content_hash: str):
    """HIT → masked text with the gazetteer exact-string net applied server-side
    (the gazetteer lives on the mini — clients may not have it). Mirrors the
    local read path's belt-and-suspenders net. MISS → None."""
    from bubble_shield import shadow_store
    cached = shadow_store.get_shadow(content_hash)
    if cached is None:
        return None
    try:
        from bubble_shield import known_pii_store
        for name in known_pii_store.load_gazetteer().values():
            if not name or len(name) < 3:
                continue   # over-masking guard, same as the local read path
            cached = cached.replace(name, "⟦NOM_∎⟧")
    except Exception:
        pass  # net is additive; a gazetteer failure must never break the read
    return cached


def _queue_index_request(root: Path, rel_path: str) -> Path | None:
    """Resolve rel_path STRICTLY under root and mark it pending on the MINI's
    store (this is the cross-machine mark_pending the client can't do itself).
    Returns the resolved path, or None if the path escapes root (traversal)."""
    if not rel_path or rel_path.startswith(("/", "\\")):
        return None
    candidate = (root / rel_path).resolve()
    root_r = root.resolve()
    if not str(candidate).startswith(str(root_r) + os.sep) and candidate != root_r:
        return None   # traversal (.., absolute, symlink escape) → reject
    from bubble_shield import shadow_store
    # The file may not exist YET (Dropbox lag) — queue anyway; the sweep walks
    # the folder and picks it up once it syncs; the pending row waits meanwhile.
    shadow_store.mark_pending(str(candidate))
    return candidate


def make_server(*, host: str, port: int, token: str, root: str) -> ThreadingHTTPServer:
    sys.path.insert(0, str(_vendor()))
    root_p = Path(os.path.expanduser(root))

    class Handler(BaseHTTPRequestHandler):
        server_version = "bubble-shield-minid/" + VERSION

        def log_message(self, fmt, *args):  # quiet; launchd captures stderr anyway
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            expect = "Bearer " + token
            return hmac.compare_digest(got.encode(), expect.encode())

        def do_GET(self):
            if self.path == "/health":
                try:
                    n = _shadow_count()
                except Exception:
                    n = -1
                self._send(200, {"ok": True, "version": VERSION, "shadow_count": n})
                return
            if self.path.startswith("/shadow/"):
                if not self._authed():
                    self._send(401, {"error": "unauthorized"})
                    return
                h = self.path[len("/shadow/"):].strip("/")
                if not h or not all(c in "0123456789abcdef" for c in h.lower()):
                    self._send(400, {"error": "bad content_hash"})
                    return
                body = _serve_shadow(h)
                if body is None:
                    self._send(404, {"status": "miss"})
                else:
                    self._send(200, {"clean_text": body})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/index_request":
                self._send(404, {"error": "not found"})
                return
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                rel = str(payload.get("rel_path", ""))
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            resolved = _queue_index_request(root_p, rel)
            if resolved is None:
                self._send(400, {"error": "rel_path escapes root"})
                return
            self._send(202, {"status": "queued"})

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True,
                    help="bind address — the mini's Tailscale IP (never 0.0.0.0)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", required=True, help="protected vault root (index_request scope)")
    args = ap.parse_args()
    if args.host in ("0.0.0.0", "::"):
        print("refusing to bind all interfaces — pass the Tailscale IP", file=sys.stderr)
        sys.exit(2)
    token = load_or_create_token()
    # SINGLETON via port-bind (same pattern as nerd/gemmad): a duplicate exits
    # cleanly on EADDRINUSE before doing any work.
    try:
        srv = make_server(host=args.host, port=args.port, token=token, root=args.root)
    except OSError as e:
        print(f"minid already running or bind failed: {e}", file=sys.stderr)
        sys.exit(0)
    print(f"bubble-shield-minid listening on {args.host}:{args.port} "
          f"(token at {_token_path()})", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
