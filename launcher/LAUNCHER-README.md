# Bubble Shield — Native Launcher (Phase 2)

The native launcher wraps the existing FastAPI webapp in a pywebview Mac window:
double-click → a real app appears, no terminal, no "open localhost" shown to the client.

## Window shell choice: pywebview (not Tauri)

**Why pywebview:**
- Pure Python — same venv as the rest of the project, no Rust toolchain, no npm/webpack.
- Wraps the platform's native WebKit (macOS: `WKWebView` via PyObjC) — identical rendering
  to Safari, zero extra dependencies at runtime on macOS.
- Packages into a real `.app` via PyInstaller in a single command.
- The frontend is already a FastAPI/Jinja2 app — a WebKit view renders it identically
  to a browser; there is no reason to add a Tauri/Electron layer.

**Why not Tauri:**
Tauri requires a full Rust toolchain + a `cargo build` + a JS bundler for the frontend.
That is ~3 GB of toolchain overhead for zero benefit when the UI is already server-rendered
HTML that pywebview renders identically.

## Dev mode (no packaging required)

```bash
# From the repo root, with the venv active:
cd /path/to/bubble-shield

# Normal (opens a native window):
.venv312/bin/python -m launcher

# Headless / CI (server only, no window):
BUBBLE_SHIELD_HEADLESS=1 .venv312/bin/python -m launcher
```

The launcher:
1. Finds a free 127.0.0.1 port (default 8765; fallback: 9000-9100 range).
2. Starts uvicorn in a subprocess.
3. Opens a pywebview window pointed at http://127.0.0.1:<port>/.
4. On window close → sends SIGTERM to the uvicorn process group, waits up to 5s,
   then SIGKILL if needed. No orphan process is ever left.

## Build Bubble Shield.app

### Prerequisites

```bash
# Install build tools (one-time)
.venv312/bin/pip install pyinstaller pywebview
```

### Build

```bash
# From the repo root:
.venv312/bin/pyinstaller Bubble_Shield.spec
# Output: dist/Bubble Shield.app
```

### Test the .app

```bash
open "dist/Bubble Shield.app"
```

### Code-sign for distribution (Joris, Apple Developer account required)

```bash
codesign --deep --force --options runtime \
  --sign "Developer ID Application: <YOUR TEAM ID>" \
  "dist/Bubble Shield.app"

# Notarise (required for macOS Gatekeeper on non-developer Macs):
xcrun notarytool submit "dist/Bubble Shield.app" \
  --apple-id <your@email.com> \
  --team-id <TEAM_ID> \
  --password <app-specific-password> \
  --wait

xcrun stapler staple "dist/Bubble Shield.app"
```

## Configuration paths (host-native)

When launched as a native app, Bubble Shield reads and writes:

| Store | Path |
|---|---|
| Policy (cloak/keep) | `~/.bubble_shield/policy.json` |
| Custom fields | `~/.config/bubble_shield/custom_fields.json` |
| Audit log | `~/.bubble_shield/audit.jsonl` |
| Known PII gazetteer | `~/.bubble_shield/gazetteer/known_pii.json` |
| Candidate sidecar | `~/.bubble_shield/candidates/<mission>.candidates.json` |

Override any path via environment variables (`BUBBLE_SHIELD_HOME`, `BUBBLE_SHIELD_AUDIT_LOG`,
`BUBBLE_SHIELD_CUSTOM_FIELDS`). The launcher sets `BUBBLE_SHIELD_HOME=~/.bubble_shield`
automatically if not already set.

## What is NOT in Phase 2

- Review queue store/feeder (Phase 1 — `bubble_shield/review_queue.py`)
- Review queue UI — Phase 3 (new pages in webapp/)
- Vault/token view/management — Tier 3
