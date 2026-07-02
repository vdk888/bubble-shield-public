"""
launcher/__main__.py — Bubble Shield native launcher entry point.

Run (dev mode, from the repo root):
    .venv312/bin/python -m launcher

What it does:
1. Find a free 127.0.0.1 port (default 8765, fallback to 9000-9100 range).
2. Start the existing FastAPI webapp via uvicorn in a subprocess.
3. Open a native pywebview window pointed at http://127.0.0.1:<port>/.
4. When the window closes → stop the uvicorn server cleanly (no orphan process).

If the server fails to start → show a native dialog with a FR error message,
not a blank screen or a raw traceback.

Window shell choice: pywebview
- Pure Python, wraps the platform's native WebKit (macOS: WKWebView via PyObjC).
- No Rust toolchain, no Tauri, no Electron bloat.
- Packages into a real .app via PyInstaller (see Bubble_Shield.spec).
- The window looks exactly like any Mac app: title bar, native close/minimise/
  maximise buttons, NO terminal behind it, NO "open localhost" shown to the client.
- Tauri would give a slightly smaller binary but requires a full Rust toolchain and
  a separate JS build for the frontend — a pointless overhead when the frontend is
  already a FastAPI/Jinja2 app that a WebKit view renders identically.

HEADLESS / CI mode:
  Set BUBBLE_SHIELD_HEADLESS=1 to skip the pywebview window.
  The server is still started and its URL is printed to stdout.
  Used by the test suite and by the dev smoke-test below.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path

# Defensive against any multiprocessing-based re-exec in the frozen .app.
# Must run before any other multiprocessing use. No-op in dev mode.
multiprocessing.freeze_support()

# Ensure repo root is importable when run as `python -m launcher`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from launcher._server import BubbleShieldServer


# ── constants ─────────────────────────────────────────────────────────────────

APP_TITLE = "Bubble Shield"
_HEADLESS = os.environ.get("BUBBLE_SHIELD_HEADLESS", "") == "1"


# ── helpers ───────────────────────────────────────────────────────────────────


def _show_error_dialog(message: str) -> None:
    """Show a native FR error dialog. Falls back to stderr if pywebview is
    not usable (e.g. headless CI, no display)."""
    try:
        import webview  # pywebview

        error_html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: -apple-system, sans-serif;
    padding: 40px;
    background: #fff8f8;
    color: #b00020;
  }}
  h2 {{ margin-bottom: 12px; }}
  pre {{
    background: #f0f0f0;
    padding: 12px;
    border-radius: 6px;
    font-size: 12px;
    color: #333;
    white-space: pre-wrap;
    word-break: break-word;
  }}
</style>
</head>
<body>
<h2>Bubble Shield — Erreur de démarrage</h2>
<p>{message.replace(chr(10), "<br>")}</p>
<p>Veuillez contacter votre administrateur ou relancer l'application.</p>
</body>
</html>"""
        w = webview.create_window(
            f"{APP_TITLE} — Erreur",
            html=error_html,
            width=600,
            height=400,
            resizable=False,
        )
        webview.start()
    except Exception:
        print(f"[Bubble Shield] Erreur fatale : {message}", file=sys.stderr)


def _write_ready_url(url: str) -> None:
    """Write the ready URL to ~/.bubble_shield/launcher.url.

    PyInstaller windowed (.app) builds can swallow stdout, so the print() in
    headless mode is not always observable. This file makes the ready URL
    verifiable regardless. Best-effort: never raise."""
    try:
        home = Path(os.environ.get(
            "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")
        ))
        home.mkdir(parents=True, exist_ok=True)
        (home / "launcher.url").write_text(url + "\n", encoding="utf-8")
    except Exception:
        pass


def _run_headless(server: BubbleShieldServer) -> None:
    """Headless mode: just print the URL and keep the server alive until
    Ctrl-C or the process receives SIGTERM (so the `finally: server.stop()`
    in main() runs for a clean shutdown)."""
    import signal
    import time

    url = server.url
    _write_ready_url(url)
    print(f"[bubble-shield] Server ready: {url}", flush=True)

    # Translate SIGTERM into KeyboardInterrupt so the loop unwinds normally and
    # main()'s finally-block calls server.stop(). Without this, SIGTERM would
    # kill the process before stop() runs (the daemon thread dies with us, so
    # the port is still freed — this just makes the clean path explicit).
    def _on_sigterm(_signum, _frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass  # not on main thread / unsupported — fall through to daemon-thread cleanup

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def _run_webview(server: BubbleShieldServer) -> None:
    """Open the pywebview native window and block until it is closed."""
    import webview  # pywebview — imported lazily so headless path has no dep

    url = server.url
    # Field-debuggability: record the bound URL so a CGP's misbehaving app can be
    # diagnosed (did the server bind?) without a terminal. Best-effort, never raises.
    _write_ready_url(url)

    def _on_loaded():
        # Nothing needed; hook is here in case future phases want JS injection.
        pass

    window = webview.create_window(
        APP_TITLE,
        url=url,
        width=1200,
        height=820,
        min_size=(900, 600),
    )
    # Register the loaded hook in a version-robust way. pywebview 4.x exposes
    # events under a `window.events` namespace (window.events.loaded); 3.x (the
    # version we vendor as an offline wheel, pywebview-3.4) puts them directly on
    # the window (window.loaded). Using the 4.x-only path crashed the app on
    # launch against the vendored 3.4 wheel (AttributeError: 'Window' object has
    # no attribute 'events'). Resolve whichever exists so the app works on both.
    _loaded_event = getattr(getattr(window, "events", None), "loaded", None)
    if _loaded_event is None:
        _loaded_event = getattr(window, "loaded", None)
    if _loaded_event is not None:
        _loaded_event += _on_loaded

    # webview.start() blocks until the window is closed.
    # gui="cocoa" is the native macOS backend; pywebview picks it automatically
    # on macOS so we don't specify it here (stays cross-platform for future Linux).
    webview.start()


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    server = BubbleShieldServer()
    try:
        server.start()
    except RuntimeError as exc:
        _show_error_dialog(str(exc))
        sys.exit(1)
    except Exception as exc:
        _show_error_dialog(
            f"Erreur inattendue au démarrage :\n{traceback.format_exc()}"
        )
        sys.exit(1)

    try:
        if _HEADLESS:
            _run_headless(server)
        else:
            _run_webview(server)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
