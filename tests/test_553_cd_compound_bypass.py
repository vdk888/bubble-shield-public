#!/usr/bin/env python3
"""P1 SECURITY REGRESSION #553 — `cd`-compound Bash bypass of the mnt-alias fail-closed.

ROOT CAUSE
----------
The #19 fix (June/July 2026) resolves a RELATIVE token (`../Dropbox/x`) into the
mnt-alias namespace by joining it against ``event.cwd`` and collapsing ``..``
lexically (``_iter_session_mnt_tokens``). That is CORRECT only when ``event.cwd``
is already the mount dir. But the REAL live attack does the ``cd`` ITSELF inside a
compound command:

    cwd = /sessions/<s>                                  (the session ROOT)
    cmd = cd /sessions/<s>/mnt/outputs && cat "../Dropbox/clients/f.pdf"

The guard never parses ``cd``, so it still joins ``../Dropbox/...`` against the
UNTRACKED session root ``/sessions/<s>`` → ``/sessions/Dropbox/...`` → which does
NOT match ``_SESSION_MNT_RE`` (no ``/mnt/`` segment) → the token is ignored →
Fix-C never fires → ALLOW → the protected file leaks in clear.

The #19 fix + its review tested the WRONG shape: they set ``cwd`` = the mount dir
(``/sessions/<s>/mnt/outputs``) and a bare ``cat "../Dropbox/..."`` — which the
guard correctly DENIES — so the leak was never observed. THIS file reproduces the
EXACT live shape: the ``cd`` is in the command, ``cwd`` is the session root.

THE FIX
-------
Parse a leading ``cd X`` / ``cd X && cd Y && …`` / ``cd X; …`` chain to compute
the EFFECTIVE cwd, then resolve relative tokens (and the mnt-classification /
marker walk-up) against THAT effective cwd instead of ``event.cwd``.

All names here are SYNTHETIC (e.g. "Marc DURAND"). No real client name appears.
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


def run_guard(guard: Path, event: dict, config_path: str | None) -> dict:
    env = dict(os.environ)
    if config_path is None:
        # Prod regime: NO global config discoverable (marker-only).
        env.pop("BUBBLE_SHIELD_GUARD_CONFIG", None)
        env["CLAUDE_PROJECT_DIR"] = tempfile.gettempdir()
    else:
        env["BUBBLE_SHIELD_GUARD_CONFIG"] = config_path
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
        return {"permissionDecision": "allow", "_implicit": True}
    return json.loads(out)["hookSpecificOutput"]


def decision(guard: Path, command: str, config_path: str | None, cwd: str) -> str:
    ev = {
        "tool_name": "mcp__workspace__bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    return run_guard(guard, ev, config_path)["permissionDecision"]


class Cd553CompoundBypassTest(unittest.TestCase):
    """The EXACT live #553 leak shapes: `cd` INSIDE a compound command, with
    ``cwd`` = the session ROOT (not the mount dir)."""

    # Session root — the guard's event.cwd in the real leak. NOT the mount dir.
    ROOT = "/sessions/cool-bold-volta"

    def _d(self, guard: Path, command: str, cfg: str | None = None) -> str:
        return decision(guard, command, cfg, cwd=self.ROOT)

    # --- (1) THE LEAK, exact live shapes → must DENY post-fix -------------------
    def test_cd_into_outputs_then_dotdot_dropbox_denied(self):
        # `cd .../mnt/outputs && cat "../Dropbox/clients/f.pdf"` — the `..` climbs
        # out of the infra mount into the sibling protected mount. cwd=session root.
        cmd = f'cd {self.ROOT}/mnt/outputs && cat "../Dropbox/clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: cd-into-outputs + ../Dropbox read must DENY")

    def test_cd_into_mnt_then_bare_relative_denied(self):
        # `cd .../mnt && cat "Dropbox/clients/f.pdf"` — bare relative from the mount
        # root. cwd=session root.
        cmd = f'cd {self.ROOT}/mnt && cat "Dropbox/clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: cd-into-mnt + bare Dropbox read must DENY")

    # --- (2) Variants: chained cd, `;` separator, quoted target ----------------
    def test_multiple_cd_chain_denied(self):
        cmd = f'cd {self.ROOT}/mnt && cd Dropbox && cat "clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: chained cd into Dropbox must DENY")

    def test_cd_semicolon_separator_denied(self):
        cmd = f'cd {self.ROOT}/mnt/outputs ; cat "../Dropbox/clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: cd with `;` separator must DENY")

    def test_cd_quoted_target_denied(self):
        cmd = f'cd "{self.ROOT}/mnt/outputs" && cat "../Dropbox/clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: quoted cd target must DENY")

    def test_cd_into_dropbox_directly_denied(self):
        # `cd .../mnt/Dropbox && cat "clients/f.pdf"` — cd directly into the
        # protected mount, then a bare relative read.
        cmd = f'cd {self.ROOT}/mnt/Dropbox && cat "clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: cd directly into Dropbox mount must DENY")

    # --- (3) NO over-block: infra-mount cd stays ALLOWED -----------------------
    def test_cd_outputs_read_infra_file_allowed(self):
        # cd into outputs, read a file that STAYS in outputs (does NOT escape).
        cmd = f'cd {self.ROOT}/mnt/outputs && cat rt-probe.txt'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "allow",
                                 f"{g.name}: reading an outputs file after cd must ALLOW")

    def test_cd_outputs_write_then_read_infra_allowed(self):
        cmd = f'cd {self.ROOT}/mnt/outputs && echo hi > y.txt && cat y.txt'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "allow",
                                 f"{g.name}: write+read within outputs must ALLOW")

    def test_cd_tmp_allowed(self):
        cmd = 'cd /tmp && cat x.txt'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "allow",
                                 f"{g.name}: cd /tmp read must ALLOW")

    def test_plain_ls_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, "ls", None), "allow")

    def test_cd_outputs_subdir_relative_read_allowed(self):
        # cd into outputs, read a relative file in a subdir of outputs — stays infra.
        cmd = f'cd {self.ROOT}/mnt/outputs && cat sub/result.txt'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "allow",
                                 f"{g.name}: relative read staying inside outputs must ALLOW")

    # --- (4) NO regression: pre-positioned cwd + direct alias still DENY -------
    def test_prepositioned_cwd_still_denied(self):
        # The shape #19 tested: cwd IS the mount dir, bare `cat ../Dropbox/...`.
        cmd = 'cat "../Dropbox/clients/f.pdf"'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, cmd, None, cwd=f"{self.ROOT}/mnt/outputs")
                self.assertEqual(d, "deny",
                                 f"{g.name}: pre-positioned-cwd ../Dropbox must STILL DENY")

    def test_direct_alias_read_still_denied(self):
        cmd = f'cat {self.ROOT}/mnt/Dropbox/clients/f.pdf'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: direct alias read must STILL DENY")

    # --- (5) cd to a NON-mount path then absolute-alias read → existing logic --
    def test_cd_nonmount_then_absolute_alias_read_denied(self):
        # cd /tmp does NOT change mnt-classification of an ABSOLUTE alias token.
        cmd = f'cd /tmp && cat {self.ROOT}/mnt/Dropbox/clients/f.pdf'
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._d(g, cmd), "deny",
                                 f"{g.name}: absolute alias read after cd /tmp must DENY")


if __name__ == "__main__":
    unittest.main()
