# -*- mode: python ; coding: utf-8 -*-
#
# Bubble_Shield.spec — PyInstaller packaging spec for the native Mac launcher.
#
# Produces: dist/Bubble Shield.app
#
# Usage (from the repo root, with the venv active):
#   pip install pyinstaller pywebview
#   pyinstaller Bubble_Shield.spec
#
# The resulting .app:
#   - Double-click to launch (no terminal, no "open localhost" shown to users).
#   - Reads/writes ~/.bubble_shield/ directly on the host.
#   - 100% local: no telemetry, no cloud calls (only localhost <-> itself).
#
# Code-signing (required for distribution to CGPs):
#   codesign --deep --force --options runtime \
#     --sign "Developer ID Application: <YOUR TEAM>" \
#     "dist/Bubble Shield.app"
#   xcrun stapler staple "dist/Bubble Shield.app"
# (Joris controls the Apple Developer account — sign after review.)
#
# NOTE: Heavy ML deps (GLiNER, onnxruntime, numpy) are NOT bundled.
# They live in the user's pip environment / the lazy-download path.
# The .app bundles only: FastAPI, uvicorn, Jinja2, pywebview, pypdf,
# and the bubble_shield package (pure-Python core + recognizers).

import os
from pathlib import Path

_ROOT = Path(SPECPATH)  # noqa: F821 — PyInstaller provides SPECPATH

block_cipher = None

a = Analysis(
    [str(_ROOT / "launcher" / "__main__.py")],
    pathex=[str(_ROOT)],
    binaries=[],
    datas=[
        # Webapp templates and static assets
        (str(_ROOT / "webapp" / "templates"), "webapp/templates"),
        (str(_ROOT / "webapp" / "static"), "webapp/static"),
        # bubble_shield package data (allowlist JSON, etc.)
        (str(_ROOT / "bubble_shield"), "bubble_shield"),
    ],
    hiddenimports=[
        # pywebview macOS backend
        "webview.platforms.cocoa",
        # FastAPI / uvicorn internals that PyInstaller misses
        "uvicorn.lifespan.off",
        "uvicorn.lifespan.on",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "fastapi",
        "fastapi.templating",
        "fastapi.staticfiles",
        # bubble_shield optional detectors (fail-open; include so import works)
        "bubble_shield.gliner_ext",
        "bubble_shield.structured_ext",
        "bubble_shield.profile_sweep",
        "bubble_shield.surrogate",
        # webapp sub-modules
        "webapp.app",
        "webapp.extract",
        "webapp.render",
        "webapp.dashboard",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy ML — not bundled; loaded lazily at runtime if installed.
        "torch",
        "gliner",
        "onnxruntime",
        "numpy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Bubble Shield",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,       # No terminal window
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,           # TODO: add icon.icns when brand asset is ready
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Bubble Shield",
)

app = BUNDLE(  # noqa: F821
    coll,
    name="Bubble Shield.app",
    icon=None,           # TODO: icon.icns
    bundle_identifier="invest.bubble.shield",
    info_plist={
        "CFBundleName": "Bubble Shield",
        "CFBundleDisplayName": "Bubble Shield",
        "CFBundleVersion": "2.0.0",
        "CFBundleShortVersionString": "2.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        # Privacy — no camera/mic/location needed
        "NSAppleEventsUsageDescription": "Bubble Shield uses Apple Events to open documents.",
    },
)
