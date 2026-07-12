"""
tests/test_phase2_launcher.py — Phase 2 launcher tests.

Covers:
1. find_free_port returns the preferred port when it is free.
2. find_free_port falls back when the preferred port is taken.
3. BubbleShieldServer starts uvicorn; GET / → 200 (full integration).
4. Clean shutdown: after server.stop() no process with that PID is alive
   (no orphan).
5. Port-conflict path: if preferred port is busy, the server starts on
   another port and GET /health-noauth → 200.
6. Server failure path: RuntimeError with FR message when the command fails.
7. webapp AUDIT_LOG path respects BUBBLE_SHIELD_HOME env var (Unit 2 fix).
8. Dashboard route works when served by the launcher against a tmp config dir.

All synthetic data — no real PII anywhere. The pre-commit pii-guard hook
would block any real name.
"""
from __future__ import annotations

import os
import socket
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

# We import from the launcher package directly — no pywebview dependency in
# tests (headless CI).
from launcher._server import (
    BubbleShieldServer,
    _port_is_free,
    find_free_port,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _occupy_port() -> tuple[socket.socket, int]:
    """Bind a socket to an OS-assigned port on 127.0.0.1 and return
    (sock, port). The caller must close the socket to free the port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _get_json(url: str, timeout: float = 5.0) -> int:
    """Return the HTTP status code for a GET request."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


# ── Unit 1: port helpers ──────────────────────────────────────────────────────

class TestFindFreePort:
    def test_prefers_free_preferred_port(self):
        """When preferred port is free, find_free_port returns it."""
        # Find a free port by asking the OS, then verify find_free_port agrees.
        sock, port = _occupy_port()
        sock.close()
        # Now the port is free
        result = find_free_port(preferred=port)
        assert result == port

    def test_falls_back_when_preferred_is_taken(self):
        """When the preferred port is occupied, a different free port is returned."""
        sock, occupied_port = _occupy_port()
        try:
            result = find_free_port(preferred=occupied_port)
            assert result != occupied_port, "Should pick a different port"
            assert _port_is_free(result), "Returned port must itself be free"
        finally:
            sock.close()

    def test_port_is_free_detects_occupied(self):
        """_port_is_free returns False for a port we know is occupied."""
        sock, port = _occupy_port()
        try:
            assert not _port_is_free(port)
        finally:
            sock.close()

    def test_port_is_free_detects_free(self):
        """_port_is_free returns True for a port that is not bound."""
        sock, port = _occupy_port()
        sock.close()
        # Small race window but fine for a unit test
        assert _port_is_free(port)


# ── Unit 1: server lifecycle ──────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.integration
class TestBubbleShieldServer:
    """These tests actually start uvicorn. They are marked 'integration' so
    they can be skipped in environments without the full dep stack."""

    def test_server_starts_and_responds(self, tmp_path):
        """Server starts, GET /health-noauth returns 200, then shuts down."""
        env_patch = {"BUBBLE_SHIELD_HOME": str(tmp_path)}
        with patch.dict(os.environ, env_patch):
            server = BubbleShieldServer(repo_root=_REPO_ROOT, ready_timeout=60.0)
            url = server.start()
            try:
                assert url.startswith("http://127.0.0.1:")
                status = _get_json(f"{url}/health-noauth")
                assert status == 200, f"Expected 200, got {status}"
                # Root should also respond
                status_root = _get_json(url + "/")
                assert status_root == 200
            finally:
                server.stop()

    def test_clean_shutdown_no_orphan(self, tmp_path):
        """After server.stop(), the uvicorn process must not be alive."""
        import subprocess
        import signal

        env_patch = {"BUBBLE_SHIELD_HOME": str(tmp_path)}
        with patch.dict(os.environ, env_patch):
            server = BubbleShieldServer(repo_root=_REPO_ROOT, ready_timeout=60.0)
            server.start()
            pid = server._proc.pid if server._proc else None
            assert pid is not None
            server.stop()

        # After stop(), the process should not be running.
        if pid is not None:
            # Give it a moment to fully exit (it already waited in _kill_proc).
            time.sleep(0.5)
            try:
                os.kill(pid, 0)  # signal 0 = just check existence
                # If we get here, the process still exists — check if it's a zombie.
                # A zombie is OK (it exited but wasn't reaped by its parent yet).
                # Read /proc or use waitpid — on macOS, os.kill(pid, 0) succeeds
                # for zombies too. Use subprocess.Popen.poll() instead: we already
                # called wait() inside _kill_proc so the proc should be reaped.
                # The safest check: try to connect to the port.
                time.sleep(1)
                port = server._port
                if port:
                    assert _port_is_free(port), (
                        f"Port {port} is still occupied after server.stop() — "
                        "orphan uvicorn process suspected"
                    )
            except ProcessLookupError:
                pass  # Process is gone — exactly what we want

    def test_port_conflict_fallback(self, tmp_path):
        """When the preferred port is taken, the server picks another and starts."""
        # Occupy the default port.
        blocker_sock, blocked_port = _occupy_port()
        env_patch = {"BUBBLE_SHIELD_HOME": str(tmp_path)}
        try:
            with patch.dict(os.environ, env_patch):
                server = BubbleShieldServer(
                    preferred_port=blocked_port,
                    repo_root=_REPO_ROOT,
                    ready_timeout=60.0,
                )
                url = server.start()
                try:
                    assert server.port != blocked_port, (
                        "Server should have picked a different port"
                    )
                    status = _get_json(f"{url}/health-noauth")
                    assert status == 200
                finally:
                    server.stop()
        finally:
            blocker_sock.close()

    def test_server_failure_raises_runtime_error_fr(self):
        """A broken command yields a RuntimeError with a FR message."""
        server = BubbleShieldServer(repo_root=_REPO_ROOT, ready_timeout=5.0)
        # Patch _build_cmd to return a command that immediately exits with error.
        with patch.object(server, "_build_cmd", return_value=["false"]):
            with pytest.raises(RuntimeError) as exc_info:
                server.start()
        msg = str(exc_info.value)
        # Message must be in French and mention the server.
        assert "serveur" in msg.lower() or "démarrer" in msg.lower(), (
            f"Error message should be FR and mention server: {msg!r}"
        )


# ── Unit 2: audit log path (host-native fix) ──────────────────────────────────

class TestAuditLogPath:
    def test_audit_log_uses_bubble_shield_home(self, tmp_path, monkeypatch):
        """When BUBBLE_SHIELD_HOME is set, AUDIT_LOG points there."""
        monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
        monkeypatch.delenv("BUBBLE_SHIELD_AUDIT_LOG", raising=False)

        # Re-run the resolution function (not the module-level cached value).
        import importlib
        import webapp.app as _app
        # Call the private resolver directly.
        result = _app._resolve_audit_log()
        assert result == str(tmp_path / "audit.jsonl"), (
            f"Expected {tmp_path / 'audit.jsonl'}, got {result}"
        )

    def test_audit_log_explicit_env_wins(self, tmp_path, monkeypatch):
        """BUBBLE_SHIELD_AUDIT_LOG always wins over BUBBLE_SHIELD_HOME."""
        custom = str(tmp_path / "custom_audit.jsonl")
        monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG", custom)
        monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))

        import webapp.app as _app
        result = _app._resolve_audit_log()
        assert result == custom

    def test_audit_log_fallback_to_webapp_data(self, monkeypatch):
        """Without either env var, falls back to webapp/data/audit.jsonl."""
        monkeypatch.delenv("BUBBLE_SHIELD_AUDIT_LOG", raising=False)
        monkeypatch.delenv("BUBBLE_SHIELD_HOME", raising=False)

        import webapp.app as _app
        result = _app._resolve_audit_log()
        assert result.endswith(os.sep + "audit.jsonl")
        assert "webapp" in result


# ── Unit 2: dashboard works host-native ──────────────────────────────────────

class TestDashboardHostNative:
    """Dashboard route renders using a tmp BUBBLE_SHIELD_HOME — no Cowork path."""

    def test_dashboard_renders_with_tmp_home(self, tmp_path, monkeypatch):
        """GET /dashboard returns 200 when served with a tmp BUBBLE_SHIELD_HOME."""
        monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
        monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG",
                           str(tmp_path / "audit.jsonl"))

        from httpx import AsyncClient, ASGITransport
        import asyncio
        import webapp.app as _app

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=_app.app),
                base_url="http://test",
            ) as client:
                r = await client.get("/dashboard")
                return r.status_code

        status = asyncio.run(_run())
        assert status == 200, f"Dashboard returned {status}"

    def test_policy_route_renders_with_tmp_home(self, tmp_path, monkeypatch):
        """POST /dashboard/policy returns 200 (policy save path is writable)."""
        monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
        monkeypatch.setenv("BUBBLE_SHIELD_AUDIT_LOG",
                           str(tmp_path / "audit.jsonl"))

        from httpx import AsyncClient, ASGITransport
        import asyncio
        import webapp.app as _app

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=_app.app),
                base_url="http://test",
            ) as client:
                r = await client.post("/dashboard/policy", data={})
                return r.status_code

        status = asyncio.run(_run())
        assert status == 200, f"Policy route returned {status}"
