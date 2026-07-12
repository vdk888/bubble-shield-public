#!/usr/bin/env python3
"""Regression tests for two live red-team Bash guard bypasses (Findings #19+#20).

FINDING #20 — bare-name symlink (or cwd-relative bare name) into a protected folder
------------------------------------------------------------------------------------
A relative symlink with NO slash bypassed the guard entirely::

    ln -s <protected>/secret.txt link      # 'link' created in a non-protected cwd
    cat link                               # ALLOWED → leaks the protected file

Root cause: ``_extract_command_paths`` skipped any token without a ``/``, so the
bare token ``link`` was never extracted, resolved, or checked — even though it is a
symlink pointing INTO a protected folder. Fix: the Bash branch now ALSO considers
bare (slash-free) words, but NARROWLY — it ``os.path.realpath``-resolves each
(following symlinks) and DENIES only when the resolved real path EXISTS, lands
inside a protected/marked folder, AND cwd is NOT itself inside that protected root.
That last clause preserves the DELIBERATE in-folder residual (``cd protected &&
cat avis.txt`` stays ALLOWED) and the loose extraction never over-blocks benign
bare words (``ls``, ``git status``, ``cat readme``, a symlink to ``/tmp``).

FINDING #19 — `..`-traversal into a Cowork sandbox mount alias
--------------------------------------------------------------
With a session cwd of ``/sessions/<name>/mnt/outputs`` (an infra mount),
``cat ../Dropbox/x`` resolves to ``/sessions/<name>/mnt/Dropbox/x`` — a mnt-alias
path into a NON-infra (user) mount. The v1.20.1 Fix-C fail-closed keys on tokens
matching ``^/sessions/[^/]+/mnt/…`` but the ``..``-bearing RELATIVE token never
matched (it wasn't absolute). Fix: ``_iter_session_mnt_tokens`` now joins relative
tokens to cwd and ``os.path.normpath``'s them (collapsing ``..``) BEFORE the
``/sessions/*/mnt/`` classification, so the traversal re-enters Fix C and DENIES —
while an infra traversal (``../outputs/y``) still resolves to an infra mount and
ALLOWS.

All names here are SYNTHETIC (e.g. "Marc DURAND"). No real client name appears —
the repo's pii-guard denylist would block the commit. Both guard copies (primary
+ mcpb mirror) are tested identically.
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


def run_guard(guard: Path, event: dict, config_path: str | None) -> str:
    env = dict(os.environ)
    if config_path is None:
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
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def decide(guard: Path, command: str, cwd: str, config_path: str | None) -> str:
    ev = {
        "tool_name": "mcp__workspace__bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    return run_guard(guard, ev, config_path)


class BareNameSymlinkExfilTest(unittest.TestCase):
    """FINDING #20 — a bare (slash-free) symlink into a protected folder."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        # Protected folder via an in-folder marker (Cowork-native).
        cls.protected = base / "Dropbox" / "clients" / "Marc_DURAND"
        cls.protected.mkdir(parents=True, exist_ok=True)
        (cls.protected / ".bubble-shield.json").write_text("{}")
        (cls.protected / "avis.txt").write_text("Marc DURAND, IBAN FR76 ...\n")
        # Also a symlink INSIDE the protected folder (for the residual test).
        cls.inside_link = cls.protected / "inlink"
        os.symlink(str(cls.protected / "avis.txt"), str(cls.inside_link))

        # An UNRELATED (non-protected) cwd where the attacker plants the symlink.
        cls.work = base / "work"
        cls.work.mkdir()
        # 'link' (no slash) → INTO the protected folder.
        cls.link = cls.work / "link"
        os.symlink(str(cls.protected / "avis.txt"), str(cls.link))
        # 'link2' (no slash) → a benign target OUTSIDE any protected folder.
        cls.benign = base / "benign.txt"
        cls.benign.write_text("nothing secret\n")
        os.symlink(str(cls.benign), str(cls.work / "link2"))
        # a plain (non-symlink) bare file in the unrelated cwd.
        (cls.work / "somefile.txt").write_text("plain content\n")

        # Two regimes: (a) empty global config (marker-only, prod-like) and
        # (b) a global protected_folders config. Both must DENY the leak.
        cls.cfg_empty = str(base / "empty.json")
        Path(cls.cfg_empty).write_text(json.dumps({"protected_folders": [], "block_bash": True}))
        cls.cfg_global = str(base / "global.json")
        Path(cls.cfg_global).write_text(json.dumps({
            "protected_folders": [str(cls.protected)],
            "block_bash": True,
        }))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # --- LEAK PROOF + POST-FIX DENY: bare symlink into protected ---
    def test_cat_bare_symlink_denied_marker_regime(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat link", str(self.work), None), "deny",
                    f"{g.name}: bare symlink into protected (marker regime) must DENY")

    def test_cat_bare_symlink_denied_global_config(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat link", str(self.work), self.cfg_global), "deny")

    def test_base64_bare_symlink_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(decide(g, "base64 link", str(self.work), None), "deny")

    def test_strings_bare_symlink_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(decide(g, "strings link", str(self.work), None), "deny")

    def test_quoted_bare_symlink_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(decide(g, 'cat "link"', str(self.work), None), "deny")

    # --- NO OVER-BLOCK: benign bare words stay allowed ---
    def test_benign_symlink_to_outside_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat link2", str(self.work), None), "allow",
                    f"{g.name}: symlink to a benign (non-protected) target must ALLOW")

    def test_plain_bare_file_unrelated_cwd_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat somefile.txt", str(self.work), None), "allow")

    def test_nonexistent_bare_word_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat nonexistent.txt", str(self.work), None), "allow")

    def test_benign_commands_allowed(self):
        for g in GUARDS:
            for cmd in ("ls", "git status", "make build", "cat readme"):
                with self.subTest(guard=g.name, cmd=cmd):
                    self.assertEqual(decide(g, cmd, str(self.work), None), "allow")

    # --- DELIBERATE RESIDUAL PRESERVED: bare file with cwd INSIDE protected ---
    def test_in_folder_residual_bare_file_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat avis.txt", str(self.protected), None), "allow",
                    f"{g.name}: in-folder bare file (cwd inside protected) must stay ALLOWED")

    def test_in_folder_residual_bare_symlink_allowed(self):
        # A symlink INSIDE the protected folder, read from a cwd INSIDE the same
        # protected folder → still the deliberate residual → ALLOW.
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat inlink", str(self.protected), None), "allow")


class TraversalIntoMountAliasTest(unittest.TestCase):
    """FINDING #19 — `..`-traversal from an infra mount into a user mount."""

    def test_dotdot_into_user_mount_denied_prod_regime(self):
        # cwd is an infra mount; ../Dropbox escapes into a non-infra user mount.
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat ../Dropbox/x", "/sessions/foo/mnt/outputs", None), "deny",
                    f"{g.name}: ..-traversal into a non-infra mount must DENY (Fix C)")

    def test_dotdot_into_user_mount_denied_nested(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat ../clients/sub/secret.txt", "/sessions/foo/mnt/outputs", None),
                    "deny")

    def test_dotdot_into_user_mount_quoted_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, 'cat "../Dropbox/x"', "/sessions/foo/mnt/uploads", None), "deny")

    # embedded `..` in an ABSOLUTE mnt token must also be collapsed and caught.
    def test_embedded_dotdot_absolute_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat /sessions/foo/mnt/outputs/../clients/f.txt",
                           "/sessions/foo/mnt/outputs", None), "deny")

    # --- NO OVER-BLOCK: traversal that lands back in an INFRA mount stays allowed ---
    def test_dotdot_into_infra_mount_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat ../outputs/y", "/sessions/foo/mnt/uploads", None), "allow",
                    f"{g.name}: ..-traversal landing in an infra mount must ALLOW")

    def test_dotdot_within_same_infra_mount_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(
                    decide(g, "cat ../uploads/in.txt", "/sessions/foo/mnt/uploads/sub", None),
                    "allow")


if __name__ == "__main__":
    unittest.main()
