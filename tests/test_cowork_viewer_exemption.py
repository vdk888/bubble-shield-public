#!/usr/bin/env python3
"""Guard exemption for Cowork's human-viewer tools (companion to Finding #40).

WHY THIS EXEMPTION EXISTS
-------------------------
The #40 fix forces `bubble_shield_write` to write the RESTORED real-PII file into
a GUARDED path (never the allow-listed `clean/`). The intended UX is then:

    1. agent works on TOKENS (`bubble_shield_read`);
    2. agent writes a doc/HTML containing ONLY tokens;
    3. `bubble_shield_write` restores real values to a GUARDED file on disk;
    4. the agent calls a Cowork VIEWER tool to render that restored file to the
       HUMAN's screen — the clear data appears on the human UI, the agent still
       only ever saw tokens.

Step 4 breaks if the guard blocks the viewer on a guarded path: the guard's
hooks.json matches `mcp__.*`, so it fires on Cowork's viewer tools and DENIES
them on a protected path (the generic-mcp candidate-path scan gates their
`file_path`/`html_path`).

WHY IT IS SAFE (the invariant these tests pin down)
---------------------------------------------------
Verified live what each viewer RETURNS TO THE AGENT (not what it shows the human):
  - mcp__cowork__present_files  → returns ONLY the path (no file body);
  - mcp__cowork__create_artifact → returns ONLY "Artifact \"…\" created." (no body);
  - mcp__cowork__update_artifact → returns ONLY "Artifact \"…\" updated." (no body).
So they are structurally like our OWN sanctioned bubble_shield_read/write tools:
the human sees the content, the AGENT does not. Exempting them cannot leak PII
into the agent's context — PROVIDED they keep returning only a path/confirmation.
If Cowork ever changes them to return the file body, this exemption becomes a
leak (documented in the guard comment beside the constant).

The exemption is NARROW: only these three named Cowork viewer tools. Every other
mcp__* file tool, and native Read/cat, stay BLOCKED on a protected path.

All names/fixtures here are SYNTHETIC ("Marc DURAND"); pii-guard blocks reals.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD_PRIMARY = REPO / "plugin" / "bubble-shield" / "scripts" / "guard.py"
GUARD_MIRROR = REPO / "plugin" / "bubble-shield" / "mcpb" / "server" / "scripts" / "guard.py"
GUARDS = [g for g in (GUARD_PRIMARY, GUARD_MIRROR) if g.is_file()]


def run_guard(guard: Path, event: dict) -> str:
    env = dict(os.environ)
    env.pop("BUBBLE_SHIELD_GUARD_CONFIG", None)
    env["CLAUDE_PROJECT_DIR"] = tempfile.gettempdir()
    proc = subprocess.run(
        [sys.executable, str(guard)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"guard exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


class CoworkViewerExemptionTest(unittest.TestCase):
    """The three named Cowork viewer tools are ALLOWED on a protected path;
    everything else on a protected path still DENIES."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        # Protected folder via an in-folder marker (Cowork-native).
        cls.protected = base / "Clients" / "Marc_DURAND"
        cls.protected.mkdir(parents=True, exist_ok=True)
        (cls.protected / ".bubble-shield.json").write_text("{}")
        # A RESTORED real-PII file at the marker root (a guarded path — exactly
        # where the #40 fix forces bubble_shield_write to land the file).
        cls.restored = cls.protected / "resultat-demo.txt"
        cls.restored.write_text("Cher Marc DURAND, votre dossier est prêt.\n")
        cls.cwd = str(base)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ── the three viewers → ALLOWED on a protected path (was DENY pre-change) ──
    def test_present_files_on_protected_is_allowed(self):
        ev = {
            "tool_name": "mcp__cowork__present_files",
            "tool_input": {"files": [{"file_path": str(self.restored)}]},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    run_guard(g, ev), "allow",
                    f"{g.name}: present_files must be ALLOWED on a protected path "
                    "(it renders to the human, returns only a path to the agent)")

    def test_create_artifact_on_protected_is_allowed(self):
        ev = {
            "tool_name": "mcp__cowork__create_artifact",
            "tool_input": {"html_path": str(self.restored)},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(run_guard(g, ev), "allow",
                                 f"{g.name}: create_artifact must be ALLOWED on a protected path")

    def test_update_artifact_on_protected_is_allowed(self):
        ev = {
            "tool_name": "mcp__cowork__update_artifact",
            "tool_input": {"html_path": str(self.restored)},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(run_guard(g, ev), "allow",
                                 f"{g.name}: update_artifact must be ALLOWED on a protected path")

    # ── opaque-prefixed form → ALSO ALLOWED (suffix match, like _OWN_MCP) ──────
    def test_opaque_prefixed_present_files_is_allowed(self):
        ev = {
            "tool_name": "mcp__plugin_cowork_cowork__present_files",
            "tool_input": {"files": [{"file_path": str(self.restored)}]},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    run_guard(g, ev), "allow",
                    f"{g.name}: opaque-prefixed viewer name must match by suffix")

    def test_opaque_prefixed_create_artifact_is_allowed(self):
        ev = {
            "tool_name": "mcp__plugin_cowork_cowork__create_artifact",
            "tool_input": {"html_path": str(self.restored)},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(run_guard(g, ev), "allow")

    # ── NO-REGRESSION: the exemption must be NARROW ────────────────────────────
    def test_generic_mcp_read_on_protected_still_denied(self):
        """A generic filesystem MCP tool on a protected path → still DENIED.
        This tool RETURNS the file body to the agent, so it stays blocked."""
        ev = {
            "tool_name": "mcp__filesystem__read_file",
            "tool_input": {"path": str(self.restored)},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    run_guard(g, ev), "deny",
                    f"{g.name}: a generic mcp file tool must still be DENIED "
                    "(the exemption is only for the named viewers)")

    def test_native_read_on_protected_still_denied(self):
        ev = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.restored)},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(run_guard(g, ev), "deny",
                                 f"{g.name}: native Read must still be DENIED on a protected path")

    def test_bash_cat_on_protected_still_denied(self):
        ev = {
            "tool_name": "mcp__workspace__bash",
            "tool_input": {"command": f"cat {self.restored}"},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(run_guard(g, ev), "deny",
                                 f"{g.name}: `cat` on a protected file must still be DENIED")

    def test_lookalike_non_viewer_mcp_still_denied(self):
        """A tool whose name merely CONTAINS a viewer substring but does NOT end
        with the exact suffix (e.g. a `present_files_raw` dumping the body) must
        NOT be exempted — suffix match, not substring."""
        ev = {
            "tool_name": "mcp__evil__present_files_and_dump_body",
            "tool_input": {"files": [{"file_path": str(self.restored)}]},
            "cwd": self.cwd,
        }
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    run_guard(g, ev), "deny",
                    f"{g.name}: a non-viewer tool that only contains the viewer name "
                    "as a substring must NOT be exempted")


if __name__ == "__main__":
    unittest.main()
