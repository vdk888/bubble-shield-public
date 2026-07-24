#!/usr/bin/env python3
"""bubble_shield_setup_mini.py — set up the Mac-mini read-distribution TIER.

Run ONCE on the client's Mac mini (the sole indexer). It:
  1. Copies bubble_shield_minid.py + the engine to a STABLE path (~/.bubble_shield/
     daemon/) so a plugin update / Cowork GC can't strand the launchd job.
  2. Writes + loads a LaunchAgent (com.bubbleshield.minid) that runs minid at login
     and KEEPS IT ALIVE (KeepAlive=true — a read daemon must ALWAYS be up; a mini
     reboot / macOS update must not silently kill every employee's read access).
  3. Verifies minid is actually serving (/health) before declaring success.

CRITICAL DIFFERENCE from nerd/gemmad launchd jobs:
  - KeepAlive is UNCONDITIONAL (true), not {SuccessfulExit:false}. nerd/gemmad
    idle-shut-down (exit 0) ON PURPOSE to free RAM; minid must NEVER be down.
  - minid runs on STOCK macOS python3 (3.9) — it's stdlib + the pure-python engine
    (shadow_store/known_pii_store), NO ML deps. So it does NOT need the 3.12 venv.
  - --host / --root / --port are per-install → passed as args, baked into the plist.

Usage (on the mini):
  python3 bubble_shield_setup_mini.py --host <mini-tailscale-ip> \
      --root <path-to-protected-vault> [--port 8377]
  # --host: the mini's Tailscale IP (tailscale ip -4). NEVER 0.0.0.0.
  # --root: the protected folder minid resolves index_request rel_paths under
  #         (the Dropbox clients folder).
  # --uninstall: remove the LaunchAgent + stop minid.
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME",
                                         Path.home() / ".bubble_shield"))
MINID_LABEL = "com.bubbleshield.minid"
MINID_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MINID_LABEL}.plist"
DAEMON_DIR = BUBBLE_SHIELD_HOME / "daemon" / "scripts"
# minid + the pure-python engine it imports (shadow_store, known_pii_store, +deps).
# We copy the WHOLE vendored bubble_shield package to be safe — it's small and
# pure-python, and this is the stable copy launchd points at.
VENDOR_SRC = Path(__file__).resolve().parent.parent / "vendor" / "bubble_shield"


def log(msg: str) -> None:
    print(msg, flush=True)


def _install_to_stable_path() -> Path:
    """Copy minid + the engine into ~/.bubble_shield/daemon so launchd never points
    at the ephemeral per-session plugin cache (Cowork GCs it on every update)."""
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    src_minid = Path(__file__).resolve().parent / "bubble_shield_minid.py"
    dst_minid = DAEMON_DIR / "bubble_shield_minid.py"
    shutil.copy2(src_minid, dst_minid)
    # vendor engine next to the daemon so `from bubble_shield import ...` resolves
    dst_vendor = DAEMON_DIR / "bubble_shield"
    if dst_vendor.exists():
        shutil.rmtree(dst_vendor)
    shutil.copytree(VENDOR_SRC, dst_vendor,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "deployment_allowlist.json"))
    log(f"✓ minid + engine installed to stable path: {dst_minid}")
    return dst_minid


def _write_and_load_plist(py: str, minid: Path, host: str, root: str,
                          port: int) -> None:
    logf = BUBBLE_SHIELD_HOME / "minid.log"
    # KeepAlive UNCONDITIONAL — a read daemon must always be up. If minid exits for
    # ANY reason (crash, OOM, reboot), launchd restarts it. This is the whole point
    # of the mini tier's availability (contrast nerd/gemmad which idle-exit on
    # purpose to free RAM). PYTHONPATH points at the stable daemon dir so
    # `from bubble_shield import shadow_store` resolves against the vendored copy.
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{MINID_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{minid}</string>
    <string>--host</string><string>{host}</string>
    <string>--root</string><string>{root}</string>
    <string>--port</string><string>{port}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BUBBLE_SHIELD_HOME</key><string>{BUBBLE_SHIELD_HOME}</string>
    <key>PYTHONPATH</key><string>{DAEMON_DIR}</string>
    <key>BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{logf}</string>
  <key>StandardErrorPath</key><string>{logf}</string>
</dict>
</plist>
"""
    MINID_PLIST.parent.mkdir(parents=True, exist_ok=True)
    MINID_PLIST.write_text(plist, encoding="utf-8")
    # unload-then-load so a re-run picks up new host/root/port
    subprocess.run(["launchctl", "unload", str(MINID_PLIST)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(MINID_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"⚠️ launchctl load returned {r.returncode}: {r.stderr.strip()}")
    else:
        log(f"✓ LaunchAgent loaded ({MINID_LABEL}) — minid starts at login, "
            "restarts on any exit")


def _verify_serving(host: str, port: int, timeout_s: int = 30) -> bool:
    """Poll /health until minid answers (it starts fast — no model load)."""
    deadline = time.monotonic() + timeout_s
    url = f"http://{host}:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                import json
                d = json.loads(r.read().decode())
                if d.get("ok"):
                    log(f"✓ minid serving on {host}:{port} "
                        f"(shadows: {d.get('shadow_count', '?')})")
                    return True
        except Exception:
            time.sleep(1)
    log(f"✗ minid did NOT answer /health on {host}:{port} within {timeout_s}s — "
        f"check {BUBBLE_SHIELD_HOME / 'minid.log'}")
    return False


def uninstall() -> int:
    subprocess.run(["launchctl", "unload", str(MINID_PLIST)], capture_output=True)
    if MINID_PLIST.exists():
        MINID_PLIST.unlink()
    log(f"✓ minid LaunchAgent removed ({MINID_LABEL})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="the mini's Tailscale IP (tailscale ip -4)")
    ap.add_argument("--root", help="protected vault root (Dropbox clients folder)")
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if args.uninstall:
        return uninstall()
    if not args.host or not args.root:
        ap.error("--host and --root are required (unless --uninstall)")
    if args.host in ("0.0.0.0", "::"):
        log("✗ refuse to bind all interfaces — pass the Tailscale IP")
        return 2
    root = str(Path(os.path.expanduser(args.root)).resolve())
    if not Path(root).is_dir():
        log(f"✗ --root is not a directory: {root}")
        return 2

    log("Bubble Shield — Mac-mini read-distribution tier setup")
    log(f"  home: {BUBBLE_SHIELD_HOME}")
    log(f"  host: {args.host}  port: {args.port}")
    log(f"  root: {root}")

    py = sys.executable  # stock python3 is fine — minid is stdlib + pure-python engine
    minid = _install_to_stable_path()
    _write_and_load_plist(py, minid, args.host, root, args.port)
    ok = _verify_serving(args.host, args.port)

    if ok:
        tok = (BUBBLE_SHIELD_HOME / "mini_token")
        log("\n✅ Mini tier ready. Next:")
        log(f"  • Bearer token (paste into each employee's config): "
            f"{'see ' + str(tok) if tok.is_file() else '(generated on first request)'}")
        log(f"  • Each employee sets mini_url=http://{args.host}:{args.port} + token")
        log(f"  • minid auto-starts at login + restarts on any exit (KeepAlive).")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
