"""
launcher/_server.py — uvicorn server lifecycle management for the native launcher.

Two run modes, picked automatically:

- DEV mode (`python -m launcher`): start uvicorn in a SUBPROCESS so we get a
  real PID to kill. `sys.executable` is the venv Python, so `-m uvicorn` works.

- FROZEN mode (the PyInstaller .app): `sys.executable` is the frozen app binary
  itself, so shelling out to `sys.executable -m uvicorn` would re-launch the
  whole app recursively (fork bomb) and never bind a port. Instead we run
  uvicorn IN-PROCESS on a background thread via the programmatic API
  (`uvicorn.Config` + `uvicorn.Server`).

Both modes share the same public contract:
- Find a free 127.0.0.1 port (default 8765; fallback to any open port).
- Poll until the server is ready (GET /health-noauth → 200) or time out.
- Shut down cleanly on request — no orphan process/thread left behind.

This module has NO pywebview dependency so it can be unit-tested headlessly.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import urllib.request
import urllib.error


def _is_frozen() -> bool:
    """True when running inside a PyInstaller-frozen bundle (the .app).

    In that case `sys.executable` is the app binary, not a Python interpreter,
    so the subprocess `-m uvicorn` approach must NOT be used.
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


# ── port helpers ──────────────────────────────────────────────────────────────

_DEFAULT_PORT = 8765
_FALLBACK_RANGE_START = 9000
_FALLBACK_RANGE_END = 9100


def find_free_port(preferred: int = _DEFAULT_PORT) -> int:
    """Return *preferred* if it is free on 127.0.0.1, else pick one from the
    fallback range, else let the OS assign one (bind port 0).

    Raises RuntimeError only if the OS itself cannot find any free port (should
    never happen in practice).
    """
    if _port_is_free(preferred):
        return preferred
    for p in range(_FALLBACK_RANGE_START, _FALLBACK_RANGE_END):
        if _port_is_free(p):
            return p
    # Last resort: ask the OS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    """True when nothing is listening on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ── in-process server (frozen mode) ───────────────────────────────────────────


class _InProcessServer:
    """Runs uvicorn IN-PROCESS on a background thread.

    Used in the frozen .app where we cannot shell out to `sys.executable`.
    `webapp.app:app` is imported directly (PyInstaller bundles it). Shutdown
    is cooperative: set `server.should_exit = True` and join the thread.
    """

    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._server = None  # uvicorn.Server
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        import uvicorn
        # Import the ASGI app directly. BUBBLE_SHIELD_HOME / PYTHONPATH have
        # already been set in BubbleShieldServer.start() before we get here.
        from webapp.app import app as asgi_app

        config = uvicorn.Config(
            asgi_app,
            host=self._host,
            port=self._port,
            log_level="warning",
            # In-process: no signal handlers (we own shutdown via should_exit),
            # single worker, no reload.
            workers=1,
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers by default; that only works on the
        # main thread. Disable so running on a background thread is safe.
        self._server.install_signal_handlers = lambda: None

        self._thread = threading.Thread(
            target=self._server.run,
            name="bubble-shield-uvicorn",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal uvicorn to exit and join the thread. Idempotent."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._server = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ── server process ────────────────────────────────────────────────────────────


class BubbleShieldServer:
    """Manages the uvicorn subprocess for the Bubble Shield webapp.

    Usage::

        server = BubbleShieldServer()
        url = server.start()          # blocks until ready or raises
        # ... app runs ...
        server.stop()                 # clean shutdown, no orphan
    """

    def __init__(self, preferred_port: int = _DEFAULT_PORT,
                 ready_timeout: float = 30.0,
                 repo_root: Optional[Path] = None):
        self._preferred_port = preferred_port
        self._ready_timeout = ready_timeout
        self._repo_root = repo_root or Path(__file__).resolve().parent.parent
        self._port: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self._inproc: Optional[_InProcessServer] = None

    # public ──────────────────────────────────────────────────────────────────

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self._port}" if self._port else None

    def start(self) -> str:
        """Start uvicorn; return the base URL when the server is ready.

        Raises RuntimeError with a FR-language message on failure (the launcher
        shows this directly in the error dialog / window).
        """
        if self._proc is not None or self._inproc is not None:
            raise RuntimeError("Server already running")

        self._port = find_free_port(self._preferred_port)

        # Set env BEFORE importing the app (frozen path imports webapp.app in
        # this process). PYTHONPATH only matters for the subprocess path, but
        # BUBBLE_SHIELD_HOME matters for both.
        existing_pp = os.environ.get("PYTHONPATH", "")
        env_pythonpath = (
            str(self._repo_root)
            if not existing_pp
            else f"{self._repo_root}{os.pathsep}{existing_pp}"
        )
        if "BUBBLE_SHIELD_HOME" not in os.environ:
            os.environ["BUBBLE_SHIELD_HOME"] = str(Path.home() / ".bubble_shield")

        if _is_frozen():
            # Frozen .app: run uvicorn in-process on a thread. Shelling out to
            # sys.executable would re-launch the app binary (fork bomb).
            os.environ["PYTHONPATH"] = env_pythonpath
            self._inproc = _InProcessServer("127.0.0.1", self._port)
            try:
                self._inproc.start()
            except Exception as exc:
                self._inproc = None
                raise RuntimeError(
                    "Le serveur Bubble Shield n'a pas pu démarrer.\n\n"
                    f"Détails techniques :\n{exc}"
                ) from exc
            return self._await_ready()

        # Dev mode: subprocess path.
        cmd = self._build_cmd()
        env = dict(os.environ)
        env["PYTHONPATH"] = env_pythonpath

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self._repo_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Start in its own process group so a SIGTERM to the group
                # doesn't accidentally kill the parent GUI process.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Impossible de démarrer le serveur Bubble Shield.\n"
                f"Python introuvable : {exc}"
            ) from exc

        return self._await_ready()

    def _await_ready(self) -> str:
        """Poll until /health-noauth → 200 or timeout. Works for both the
        subprocess and the in-process thread path."""
        deadline = time.monotonic() + self._ready_timeout
        last_err: Optional[str] = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                # Subprocess exited early — server failed.
                out = self._proc.stdout.read().decode("utf-8", errors="replace") if self._proc.stdout else ""
                raise RuntimeError(
                    "Le serveur Bubble Shield n'a pas pu démarrer.\n\n"
                    f"Détails techniques :\n{out[-1000:]}"
                )
            if self._inproc is not None and not self._inproc.is_alive():
                # In-process thread died before binding — server failed.
                self._inproc = None
                raise RuntimeError(
                    "Le serveur Bubble Shield n'a pas pu démarrer "
                    "(thread interne arrêté avant l'ouverture du port)."
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/health-noauth", timeout=1
                ) as resp:
                    if resp.status == 200:
                        return self.url
            except Exception as exc:
                last_err = str(exc)
            time.sleep(0.2)

        # Timed out — shut down whichever path is running and surface the error.
        self.stop()
        raise RuntimeError(
            f"Le serveur Bubble Shield n'a pas répondu dans les {self._ready_timeout:.0f}s.\n"
            f"Dernière erreur : {last_err}"
        )

    def stop(self) -> None:
        """Shut down the uvicorn server cleanly. Idempotent.

        Handles both the subprocess (dev) and in-process thread (frozen) paths.
        """
        if self._inproc is not None:
            inproc = self._inproc
            self._inproc = None
            inproc.stop(timeout=5.0)
        if self._proc is not None:
            self._kill_proc()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # private ─────────────────────────────────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        """Build the uvicorn command. Uses the same Python that runs the launcher."""
        python = sys.executable
        return [
            python, "-m", "uvicorn",
            "webapp.app:app",
            "--host", "127.0.0.1",
            "--port", str(self._port),
            "--log-level", "warning",
        ]

    def _kill_proc(self) -> None:
        """SIGTERM → wait 5s → SIGKILL. No orphan left."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform != "win32":
                # Kill the entire process group (uvicorn may spawn workers).
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGKILL as last resort.
            try:
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
            except Exception:
                pass
            proc.wait(timeout=3)
