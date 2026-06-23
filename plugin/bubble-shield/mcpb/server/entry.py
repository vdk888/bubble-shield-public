#!/usr/bin/env python3
"""MCPB entry point for the Bubble Shield stdio MCP server.

WHY THIS WRAPPER EXISTS
-----------------------
Claude Code plugins may NOT declare a bare local/stdio MCP server
(command: python3 …) — the platform rejects them with:
  "MCP server '…' is a local/stdio server. Plugins may only declare remote
   (http/sse/ws) or MCPB servers."
The sanctioned way to ship a LOCAL server in a plugin is to bundle it as an
MCPB (the host extracts it and launches manifest.server.mcp_config as stdio).

This wrapper is that launched command. It wires the bundled, pure-python
dependencies onto sys.path, points CLAUDE_PLUGIN_ROOT at the extracted bundle
(so the unchanged bubble_shield_mcp.py resolves its own vendor/ + scripts/),
then hands off to its main() loop. No server logic lives here — the wrapper is
deliberately thin so the engine stays the single source of truth.

The heavy ML accuracy pack (GLiNER / onnxruntime / numpy) is NOT bundled here.
It is downloaded on demand by bubble_shield_setup_ml into a SEPARATE runtime
venv and lazy-imported; this MCPB base server is pure-stdlib + the vendored
pure-python engine + pypdf.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # …/server inside the extracted bundle
_VENDOR = _HERE / "vendor"
_SCRIPTS = _HERE / "scripts"

# Make the vendored engine (bubble_shield, pypdf) and the sibling scripts
# importable, exactly as the plugin layout expects.
for p in (str(_VENDOR), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# bubble_shield_mcp.py resolves vendor/ and scripts/ relative to
# CLAUDE_PLUGIN_ROOT (parent of scripts/). Point it at THIS bundled server dir
# so it finds the bundled copies and not some other install on the machine.
os.environ.setdefault("CLAUDE_PLUGIN_ROOT", str(_HERE))


def main() -> int:
    import bubble_shield_mcp
    return bubble_shield_mcp.main()


if __name__ == "__main__":
    sys.exit(main())
