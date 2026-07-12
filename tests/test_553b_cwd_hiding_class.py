#!/usr/bin/env python3
"""P1 SECURITY #553 (B) — fail-close on the cwd-HIDING construct CLASS + input robustness.

PART 1 — the cwd-hiding class (the leak)
----------------------------------------
The primary #553 fix (a4e18fb) parses a leading/chained ``cd X && …`` to compute
the EFFECTIVE cwd, so ``cd /sessions/<s>/mnt && cat Dropbox/x`` is correctly
DENIED. But the ``cd`` can be HIDDEN from that lexical parse inside a construct
the guard cannot cleanly resolve — a subshell, ``bash -c "…"``, ``pushd``,
``eval``, ``$(…)`` — so the effective-cwd walk never sees it and the mnt-alias
fail-closed is evaded:

    (cd /sessions/<s>/mnt && cat "Dropbox/clients/f.pdf")          → LEAK
    bash -c "cd /sessions/<s>/mnt && cat Dropbox/clients/f.pdf"    → LEAK
    pushd /sessions/<s>/mnt && cat "Dropbox/clients/f.pdf"         → LEAK
    eval "cd /sessions/<s>/mnt && cat Dropbox/clients/f.pdf"       → LEAK

DECISION (Joris approved, option B): do NOT chase each construct with the parser.
FAIL-CLOSE on the CLASS. Rule:
    If a Bash command contains a cwd-HIDING or cwd-CHANGING construct the guard
    cannot cleanly resolve, AND the command ALSO contains ANY ``/sessions/*/mnt/``
    token → DENY the whole command.
A cwd-hiding construct with NO ``/sessions/*/mnt/`` token at all → ALLOW (do not
over-block ``bash -c "echo hi"`` or ``(ls /tmp)``).

PART 2 — input robustness (stop the false "erreur interne du guard")
--------------------------------------------------------------------
The guard's blanket-except fail-closed with a scary "🔒 erreur interne du guard"
whenever the tool event had an unexpected SHAPE the harness legitimately passes:
  - ``tool_input`` as a LIST (not dict)
  - ``cwd`` as an INT (not str)
  - ``command`` as a LIST (not str)
These reach a NORMAL decision now (coerce-then-decide, never coerce-to-allow):
a benign list-command ALLOWs, a protected-path list-command still DENYs.

All names SYNTHETIC (e.g. "Marc DURAND"). No real client name appears.
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

ROOT = "/sessions/cool-bold-volta"
ERREUR_INTERNE = "erreur interne du guard"


def run_guard(guard: Path, event: dict) -> dict:
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
        return {"permissionDecision": "allow", "_implicit": True,
                "permissionDecisionReason": ""}
    return json.loads(out)["hookSpecificOutput"]


def d(guard: Path, command, cwd=ROOT, tool_input=None) -> str:
    if tool_input is None:
        tool_input = {"command": command}
    ev = {"tool_name": "mcp__workspace__bash", "tool_input": tool_input, "cwd": cwd}
    return run_guard(guard, ev)["permissionDecision"]


def reason(guard: Path, tool_input, cwd=ROOT) -> str:
    ev = {"tool_name": "mcp__workspace__bash", "tool_input": tool_input, "cwd": cwd}
    return run_guard(guard, ev).get("permissionDecisionReason", "")


# ---------------------------------------------------------------------------
# PART 1 — cwd-hiding construct + mnt token → DENY
# ---------------------------------------------------------------------------
class CwdHidingClassTest(unittest.TestCase):

    def _deny(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "deny", f"{g.name}: {label} must DENY")

    def _allow(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "allow", f"{g.name}: {label} must ALLOW")

    # --- the three same-class LEAKS the primary fix still let through ----------
    def test_subshell_cd_relative_denied(self):
        self._deny(f'(cd {ROOT}/mnt && cat "Dropbox/clients/f.pdf")',
                   "subshell (cd mnt && cat Dropbox/..)")

    def test_bash_c_cd_relative_denied(self):
        self._deny(f'bash -c "cd {ROOT}/mnt && cat Dropbox/clients/f.pdf"',
                   'bash -c "cd mnt && cat Dropbox/.."')

    def test_pushd_cd_relative_denied(self):
        self._deny(f'pushd {ROOT}/mnt && cat "Dropbox/clients/f.pdf"',
                   "pushd mnt && cat Dropbox/..")

    # --- eval + command-substitution variants ---------------------------------
    def test_eval_absolute_alias_denied(self):
        # already denied pre-change (literal absolute alias), must STAY denied.
        self._deny(f'eval "cat {ROOT}/mnt/Dropbox/clients/f.pdf"',
                   "eval absolute alias")

    def test_eval_cd_relative_denied(self):
        # LEAK pre-change: the cd + relative token is hidden inside eval.
        self._deny(f'eval "cd {ROOT}/mnt && cat Dropbox/clients/f.pdf"',
                   "eval cd+relative")

    def test_cmd_subst_cd_relative_denied(self):
        self._deny(f'echo $(cd {ROOT}/mnt && cat Dropbox/clients/f.pdf)',
                   "$() cd+relative")

    def test_backtick_cd_relative_denied(self):
        self._deny(f'echo `cd {ROOT}/mnt && cat Dropbox/clients/f.pdf`',
                   "backtick cd+relative")

    # --- other hiding/changing constructs + an mnt token → DENY ---------------
    def test_sh_c_denied(self):
        self._deny(f'sh -c "cat {ROOT}/mnt/outputs/x"', "sh -c + mnt token")

    def test_zsh_c_denied(self):
        self._deny(f'zsh -c "cat {ROOT}/mnt/uploads/x"', "zsh -c + mnt token")

    def test_cd_env_var_target_with_mnt_denied(self):
        # cd $VAR is unresolvable; an mnt token elsewhere → DENY.
        self._deny(f'cd $TARGET && cat {ROOT}/mnt/outputs/x', "cd $VAR + mnt token")

    def test_cd_dash_with_mnt_denied(self):
        self._deny(f'cd - && cat {ROOT}/mnt/outputs/x', "cd - + mnt token")

    def test_cd_cmdsubst_target_with_mnt_denied(self):
        self._deny(f'cd $(pwd) && cat {ROOT}/mnt/outputs/x', "cd $() target + mnt token")

    def test_cd_glob_target_with_mnt_denied(self):
        self._deny(f'cd {ROOT}/mn* && cat {ROOT}/mnt/outputs/x', "cd glob target + mnt token")

    def test_popd_with_mnt_denied(self):
        self._deny(f'popd && cat {ROOT}/mnt/outputs/x', "popd + mnt token")

    # --- ACCEPTED over-block: hiding construct + even an INFRA mnt token → DENY
    def test_subshell_infra_mnt_token_denied(self):
        # A subshell could `cd` elsewhere; we cannot prove mnt/outputs stays in
        # outputs, so ANY /sessions/*/mnt/ token inside a hiding construct → DENY.
        self._deny(f'(cd {ROOT}/mnt/outputs && cat rt.txt)',
                   "subshell + infra mnt token (accepted over-block)")

    def test_bash_c_infra_mnt_token_denied(self):
        self._deny(f'bash -c "cat {ROOT}/mnt/outputs/rt.txt"',
                   "bash -c + infra mnt token (accepted over-block)")

    # --- NO over-block: hiding construct with NO mnt token → ALLOW -------------
    def test_bash_c_echo_allowed(self):
        self._allow('bash -c "echo hi"', 'bash -c "echo hi"')

    def test_subshell_ls_tmp_allowed(self):
        self._allow('(ls /tmp)', "(ls /tmp)")

    def test_pushd_tmp_allowed(self):
        self._allow('pushd /tmp && ls', "pushd /tmp && ls")

    def test_eval_benign_allowed(self):
        self._allow('eval "echo hello world"', "eval benign")

    def test_sh_c_benign_allowed(self):
        self._allow('sh -c "date"', "sh -c benign")

    def test_cmd_subst_benign_allowed(self):
        self._allow('echo $(date)', "$() benign")

    # --- primary fix still intact (resolvable, non-hidden) --------------------
    def test_plain_cd_outputs_read_still_allowed(self):
        # resolvable cd (not hidden) into infra → stays ALLOW (primary fix behavior)
        self._allow(f'cd {ROOT}/mnt/outputs && cat rt.txt',
                    "plain resolvable cd into outputs")

    def test_plain_cd_mnt_dropbox_relative_still_denied(self):
        # resolvable cd into a protected mount → still DENY (primary fix)
        self._deny(f'cd {ROOT}/mnt && cat "Dropbox/clients/f.pdf"',
                   "plain cd into mnt + Dropbox read")

    def test_direct_alias_read_still_denied(self):
        self._deny(f'cat {ROOT}/mnt/Dropbox/clients/f.pdf', "direct alias read")

    def test_cd_tmp_allowed(self):
        self._allow('cd /tmp && cat x', "cd /tmp && cat x")


# ---------------------------------------------------------------------------
# PART 2 — input robustness (no false "erreur interne")
# ---------------------------------------------------------------------------
class InputRobustnessTest(unittest.TestCase):

    def test_list_command_benign_allowed(self):
        # command passed as a LIST — benign → ALLOW, NOT "erreur interne".
        for g in GUARDS:
            with self.subTest(guard=g.name):
                r = reason(g, {"command": ["echo", "hi"]})
                self.assertNotIn(ERREUR_INTERNE, r,
                                 f"{g.name}: list benign must not emit erreur interne")
                self.assertEqual(d(g, None, tool_input={"command": ["echo", "hi"]}),
                                 "allow", f"{g.name}: list benign must ALLOW")

    def test_list_command_protected_denied_no_bypass(self):
        # command as a LIST touching a protected alias path → still DENY.
        cmd = ["cat", f"{ROOT}/mnt/Dropbox/clients/f.pdf"]
        for g in GUARDS:
            with self.subTest(guard=g.name):
                self.assertEqual(d(g, None, tool_input={"command": cmd}), "deny",
                                 f"{g.name}: protected path via list must DENY (no bypass)")

    def test_cwd_int_tool_input_list_no_erreur_interne(self):
        # cwd as INT + tool_input as LIST → reach a normal decision, no crash.
        for g in GUARDS:
            with self.subTest(guard=g.name):
                r = reason(g, ["x"], cwd=12345)
                self.assertNotIn(ERREUR_INTERNE, r,
                                 f"{g.name}: cwd-int/tool_input-list must not crash")

    def test_cwd_int_with_command_dict(self):
        # cwd int but tool_input is a normal dict with a benign command → ALLOW.
        for g in GUARDS:
            with self.subTest(guard=g.name):
                r = reason(g, {"command": "echo hi"}, cwd=99)
                self.assertNotIn(ERREUR_INTERNE, r)
                self.assertEqual(d(g, "echo hi", cwd=99), "allow")

    def test_tool_input_none_no_erreur_interne(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                ev = {"tool_name": "mcp__workspace__bash", "tool_input": None, "cwd": ROOT}
                r = run_guard(g, ev).get("permissionDecisionReason", "")
                self.assertNotIn(ERREUR_INTERNE, r)

    def test_command_none_allowed(self):
        for g in GUARDS:
            with self.subTest(guard=g.name):
                r = reason(g, {"command": None})
                self.assertNotIn(ERREUR_INTERNE, r)
                self.assertEqual(d(g, None, tool_input={"command": None}), "allow")


if __name__ == "__main__":
    unittest.main()
