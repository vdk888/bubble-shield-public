#!/usr/bin/env python3
"""Regression tests for the Cowork sandbox-mount-alias Bash exfil (P0).

ROOT CAUSE
----------
The guard runs as a PreToolUse hook on the HOST (Mac) and matches command
strings against the REAL Mac protected paths. But Cowork mounts a marked folder
into the sandbox VM under a DYNAMIC session alias:

    /sessions/<random-session-name>/mnt/<subpath>

e.g. ``/sessions/pensive-dreamy-goldberg/mnt/clients/note.txt``. The host guard
walks that alias path UP looking for a ``.bubble-shield.json`` marker, but
``/sessions/...`` does not exist on the Mac, so the walk-up finds nothing, and
the legacy needle-scan only carries Mac-path needles (``/Users/.../clients``),
which never match the alias prefix. Result: the command is ALLOWED and PII leaks
in clear. This is DISTINCT from the June 30 "bash cwd-exfil" fix (that fixed
cwd-independent path EXTRACTION; this is a mount-namespace MISMATCH).

These tests drive the ACTUAL guard.py contract: PreToolUse event JSON on stdin,
a JSON decision on stdout. We test BOTH guard copies (primary + mcpb mirror).

All names here are SYNTHETIC (e.g. "Marc DURAND"). No real client name appears
anywhere in this file — the repo's pii-guard denylist would block the commit.
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

# Both copies must exist and get tested identically.
GUARDS = [g for g in (GUARD_PRIMARY, GUARD_MIRROR) if g.is_file()]


def run_guard(guard: Path, event: dict, config_path: str | None) -> dict:
    """Invoke a guard.py copy with an event on stdin; return the decision dict.

    Points the guard's config at ``config_path`` via BUBBLE_SHIELD_GUARD_CONFIG
    so we exercise the ``protected_folders`` (global config) code path. Pass
    ``config_path=None`` to run in the REAL Cowork PROD REGIME: NO global config
    at all (env var scrubbed) — no ``protected_folders``, no discoverable marker.
    That is the regime where the pre-hardening Fix C stayed inert and LEAKED.
    """
    env = dict(os.environ)
    if config_path is None:
        # Prod regime: ensure NO global config is discoverable. Scrub the explicit
        # override AND point CLAUDE_PROJECT_DIR at an empty dir so the project-dir
        # config location can't accidentally pick up a stray file.
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
        # No JSON emitted == the "normal permission flow proceeds" allow path.
        return {"permissionDecision": "allow", "_implicit": True}
    return json.loads(out)["hookSpecificOutput"]


def decision(guard: Path, command: str, config_path: str | None, cwd: str = "/sessions/testsession") -> str:
    ev = {
        "tool_name": "mcp__workspace__bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    return run_guard(guard, ev, config_path)["permissionDecision"]


class CoworkMntAliasExfilTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A synthetic protected folder whose BASENAME is "clients". The real Mac
        # path here is a temp dir; what matters is that its basename == "clients"
        # (the folder the user connected, exposed under /sessions/*/mnt/clients).
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.protected = base / "Dropbox" / "clients"
        cls.protected.mkdir(parents=True, exist_ok=True)
        (cls.protected / "secret.txt").write_text("Marc DURAND, IBAN FR76...\n")
        # A drop-in guard config that protects it via protected_folders + block_bash.
        cls.config_path = str(base / "bubble-shield.json")
        Path(cls.config_path).write_text(json.dumps({
            "protected_folders": [str(cls.protected)],
            "block_bash": True,
        }))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # --- (1) LEAK PROOF + POST-FIX DENY: the alias namespace must be blocked ---
    def test_alias_cat_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny", f"{g.name}: alias cat must DENY (leak)")

    def test_alias_base64_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "base64 /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_strings_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "strings /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_grep_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "grep IBAN /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_head_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "head -5 /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_tail_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "tail /sessions/testsession/mnt/clients/secret.txt", self.config_path)
                self.assertEqual(d, "deny")

    # --- (2) FIX B positive: nested path under the protected basename ---
    def test_alias_nested_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/clients/sub/deep/f.txt", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_dir_itself_denied(self):
        # `mnt/clients` exactly (the protected dir itself, e.g. `ls`).
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "ls -la /sessions/x/mnt/clients", self.config_path)
                self.assertEqual(d, "deny")

    def test_alias_quoted_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, 'cat "/sessions/x/mnt/clients/secret.txt"', self.config_path)
                self.assertEqual(d, "deny")

    # --- (3) FIX C backstop: unknown mnt subfolder (not infra, not a known
    #        protected basename) → fail closed ---
    def test_alias_unknown_mnt_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/unknownfolder/f.txt", self.config_path)
                self.assertEqual(d, "deny", f"{g.name}: unknown mnt subtree must fail closed")

    # --- (4) Infra allow (no over-block): known infra mounts must still work ---
    def test_alias_outputs_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/outputs/result.txt", self.config_path)
                self.assertEqual(d, "allow", f"{g.name}: mnt/outputs is infra, must ALLOW")

    def test_alias_uploads_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/uploads/in.txt", self.config_path)
                self.assertEqual(d, "allow")

    def test_alias_claude_infra_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/.claude/settings.json", self.config_path)
                self.assertEqual(d, "allow")

    def test_alias_remote_plugins_infra_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/mnt/.remote-plugins/plugin.js", self.config_path)
                self.assertEqual(d, "allow")

    def test_non_mnt_session_path_allowed(self):
        # /sessions/<name>/outputs/... (NOT under mnt/) must still be allowed —
        # the fail-closed backstop is scoped strictly to the mnt/ mount subtree.
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /sessions/x/outputs/log.txt", self.config_path)
                self.assertEqual(d, "allow")

    # --- (5) No regression on the REAL Mac path + benign commands ---
    def test_real_mac_path_still_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, f"cat {self.protected}/secret.txt", self.config_path)
                self.assertEqual(d, "deny", f"{g.name}: real Mac protected path must still DENY")

    def test_benign_tmp_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "cat /tmp/x.txt", self.config_path)
                self.assertEqual(d, "allow")

    def test_bare_ls_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = decision(g, "ls", self.config_path)
                self.assertEqual(d, "allow")

    # --- HARDENING (2026-07-05): Fix C is now UNCONDITIONAL on the mnt/ namespace.
    #     Even with an EMPTY global config (no protected_folders), a non-infra
    #     `mnt/<X>` must fail closed. This REPLACES the pre-hardening assertion
    #     (which allowed it, and was the residual leak). The host cannot see a
    #     marker on the sandbox-FS inode, so the only safe posture is DENY. ---
    def test_alias_unknown_denied_even_when_empty_config(self):
        with tempfile.TemporaryDirectory() as td:
            empty_cfg = str(Path(td) / "cfg.json")
            Path(empty_cfg).write_text(json.dumps({"protected_folders": [], "block_bash": True}))
            for g in GUARDS:
                with self.subTest(guard=g.name):
                    d = decision(g, "cat /sessions/x/mnt/unknownfolder/f.txt", empty_cfg)
                    self.assertEqual(
                        d, "deny",
                        f"{g.name}: empty config MUST still fail closed on non-infra mnt/")

    # Even with an empty config, infra mounts and non-mnt paths must STILL allow —
    # the unconditional Fix C must not brick the agent's own workspace.
    def test_infra_and_nonmnt_allowed_even_with_empty_config(self):
        with tempfile.TemporaryDirectory() as td:
            empty_cfg = str(Path(td) / "cfg.json")
            Path(empty_cfg).write_text(json.dumps({"protected_folders": [], "block_bash": True}))
            for g in GUARDS:
                with self.subTest(guard=g.name):
                    self.assertEqual(decision(g, "cat /sessions/x/mnt/outputs/r.txt", empty_cfg), "allow")
                    self.assertEqual(decision(g, "cat /sessions/x/mnt/uploads/i.txt", empty_cfg), "allow")
                    self.assertEqual(decision(g, "cat /sessions/x/outputs/log.txt", empty_cfg), "allow")
                    self.assertEqual(decision(g, "cat /tmp/x.txt", empty_cfg), "allow")


class CoworkProdRegimeExfilTest(unittest.TestCase):
    """The EXACT real-Cowork PROD REGIME the pre-hardening fix missed.

    NO global config is passed (env scrubbed), and the session cwd is a HOST
    outputs-style path — NOT inside any marked folder, NOT `/sessions/...`. In
    this regime the pre-hardening guard had an EMPTY basename set AND
    `_discover_marker_roots(cwd)` found nothing, so `any_protection` was FALSE and
    Fix C stayed inert → `cat /sessions/foo/mnt/clients/secret.txt` was ALLOWED
    (leak). Post-hardening, unconditional Fix C DENIES it. Cwd is set to a
    host-outputs path to mirror prod precisely (also proves cwd doesn't matter).
    """
    PROD_CWD = "/private/var/folders/aa/local_deadbeef/outputs"

    def _decide(self, guard: Path, command: str) -> str:
        # config_path=None → prod regime: NO global config discoverable.
        return decision(guard, command, None, cwd=self.PROD_CWD)

    def test_prod_regime_cat_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                d = self._decide(g, "cat /sessions/foo/mnt/clients/secret.txt")
                self.assertEqual(d, "deny", f"{g.name}: PROD-REGIME leak must now DENY")

    def test_prod_regime_base64_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "base64 /sessions/foo/mnt/clients/secret.txt"), "deny")

    def test_prod_regime_strings_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "strings /sessions/foo/mnt/clients/secret.txt"), "deny")

    def test_prod_regime_grep_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "grep IBAN /sessions/foo/mnt/clients/secret.txt"), "deny")

    def test_prod_regime_quoted_denied(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, 'cat "/sessions/foo/mnt/clients/secret.txt"'), "deny")

    # PROD REGIME must NOT over-block: infra mounts, non-mnt session paths, /tmp
    # and benign commands must STILL allow with no config at all.
    def test_prod_regime_infra_outputs_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /sessions/foo/mnt/outputs/r.txt"), "allow")

    def test_prod_regime_infra_uploads_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /sessions/foo/mnt/uploads/i.txt"), "allow")

    def test_prod_regime_infra_claude_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /sessions/foo/mnt/.claude/settings.json"), "allow")

    def test_prod_regime_infra_remote_plugins_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /sessions/foo/mnt/.remote-plugins/p.js"), "allow")

    def test_prod_regime_non_mnt_session_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /sessions/foo/outputs/log.txt"), "allow")

    def test_prod_regime_tmp_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "cat /tmp/x.txt"), "allow")

    def test_prod_regime_bare_ls_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(self._decide(g, "ls"), "allow")

    # block_bash:false GLOBAL config → whole Bash branch skipped upstream (the
    # operator's deliberate opt-out); Fix C never runs. Confirm the gating.
    def test_global_block_bash_false_skips_fixc(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = str(Path(td) / "cfg.json")
            Path(cfg).write_text(json.dumps({"protected_folders": [], "block_bash": False}))
            for g in GUARDS:
                with self.subTest(guard=g.name):
                    d = decision(g, "cat /sessions/foo/mnt/clients/secret.txt", cfg, cwd=self.PROD_CWD)
                    self.assertEqual(
                        d, "allow",
                        f"{g.name}: global block_bash:false is the operator opt-out → allow")


if __name__ == "__main__":
    unittest.main()
