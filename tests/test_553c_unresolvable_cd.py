#!/usr/bin/env python3
"""P1 SECURITY #553 (C) — env-var / command-substitution / opaque-eval obfuscation
of the cwd-hiding fail-close.

The residual after #553-B
-------------------------
The #553-B CLASS gate fires only when a hiding construct is paired with a LITERAL
``/sessions/*/mnt/`` substring (``_mentions_session_mnt``). When the mount path is
BUILT so that literal never appears in the command text, the gate lets it through:

    cd "$SESS/mnt" && cat "Dropbox/clients/f.pdf"          → ALLOW (LEAK)
    p=$(printf /x/y); cd $p && cat "Dropbox/clients/f.pdf" → ALLOW (LEAK)
    cd $(echo /whatever) && cat relative/f.pdf             → ALLOW (LEAK)
    eval "$(echo <b64> | base64 -d)"                       → ALLOW (LEAK)

Root cause: an UNRESOLVABLE ``cd`` target (env var ``$VAR``/``${VAR}``, command
substitution ``$(…)``/backticks, or otherwise-unresolvable) is ALREADY detected as
a hiding construct — but the CLASS gate additionally required the literal mnt token,
which is absent when the path is obfuscated. In a Cowork session the guard cannot
know where an unresolvable ``cd`` lands; if it might land in a mounted protected
folder, a subsequent RELATIVE read would then hit protected content. The only safe
posture is DENY.

The hardening (Joris approved, option 2)
----------------------------------------
In a bash command, if there is an UNRESOLVABLE ``cd`` target AND the command also
performs a file read/access via a RELATIVE path (or any path we can't prove is
outside a mount) → FAIL-CLOSE (DENY), EVEN WITHOUT a literal ``/sessions/*/mnt/``
token.

Read-detection heuristic (documented): a read is any of a known content-reading
verb (cat/head/tail/less/more/base64/xxd/od/strings/grep/awk/sed/python-open/…)
applied to a RELATIVE, slash-free-or-slashed non-absolute path, OR any relative
slash-bearing path token in the command. An ABSOLUTE read after an unresolvable cd
is SAFE (an absolute path is cwd-independent) → ALLOW.

Opaque-eval decision (documented): ``eval "$(...)"`` — an eval of a command
substitution whose decoded content the guard cannot see — is treated as
unresolvable AND read-opaque → FAIL-CLOSE, since the hidden text could read
anything relative.

No over-block: an unresolvable ``cd`` with NO subsequent risky read
(``cd "$DIR" && echo done``, ``cd "$DIR" && ls``) → ALLOW. An unresolvable ``cd``
followed by an ABSOLUTE read (``cd "$DIR" && cat /tmp/x``) → ALLOW.

All names SYNTHETIC. No real client name appears.
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


class UnresolvableCdResidualTest(unittest.TestCase):

    def _deny(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "deny", f"{g.name}: {label} must DENY")

    def _allow(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "allow", f"{g.name}: {label} must ALLOW")

    # --- the residual LEAKS (ALLOW pre-fix, must DENY post-fix) ----------------
    def test_envvar_cd_relative_read_denied(self):
        # cd built from env var + relative read → the obfuscation shape.
        self._deny('cd "$SESS/mnt" && cat "Dropbox/clients/f.pdf"',
                   "cd $SESS/mnt + relative read")

    def test_cmdsubst_assigned_var_cd_relative_denied(self):
        self._deny('p=$(printf /x/y); cd $p && cat "Dropbox/clients/f.pdf"',
                   "p=$(...) ; cd $p + relative read")

    def test_cmdsubst_cd_relative_read_denied(self):
        self._deny('cd $(echo /whatever) && cat relative/f.pdf',
                   "cd $(...) + relative read")

    def test_opaque_eval_denied(self):
        # eval "$(...)" — decoded content invisible → fail-close.
        self._deny('eval "$(echo Y2QgL3g= | base64 -d)"',
                   "opaque eval $(...)")

    def test_bare_var_cd_relative_denied(self):
        self._deny('cd ${SESS} && head Dropbox/x.pdf',
                   "cd ${VAR} + relative read")

    def test_backtick_cd_target_relative_denied(self):
        self._deny('cd `pwd`/sub && cat notes/secret.txt',
                   "cd `...` target + relative read")

    def test_unresolvable_cd_then_relative_grep_denied(self):
        self._deny('cd "$D" && grep secret client/notes.txt',
                   "unresolvable cd + relative grep")

    # --- NO over-block --------------------------------------------------------
    def test_unresolvable_cd_no_read_allowed(self):
        self._allow('cd "$DIR" && echo done', "unresolvable cd, no read")

    def test_unresolvable_cd_absolute_read_allowed(self):
        # absolute read is cwd-independent → the unknown cd doesn't endanger it.
        self._allow('cd "$DIR" && cat /tmp/x', "unresolvable cd + ABSOLUTE read")

    def test_unresolvable_cd_ls_allowed(self):
        self._allow('cd "$DIR" && ls', "unresolvable cd + ls (no protected arg)")

    def test_unresolvable_cd_pwd_allowed(self):
        self._allow('cd "$DIR" && pwd', "unresolvable cd + pwd")

    def test_var_cd_echo_var_allowed(self):
        self._allow('cd ${HOME} && echo "$PATH"', "cd ${HOME} + echo var")

    def test_plain_absolute_read_no_cd_allowed(self):
        # no unresolvable cd at all; benign absolute read.
        self._allow('cat /tmp/whatever.txt', "plain absolute read, no cd")

    def test_relative_read_no_cd_allowed(self):
        # relative read WITHOUT any unresolvable cd → not the obfuscation shape.
        self._allow('cat notes/todo.txt', "relative read, no unresolvable cd")


# ---------------------------------------------------------------------------
# #553-B regressions must all still hold after the #553-C hardening.
# ---------------------------------------------------------------------------
class B_Regression_StillHolds(unittest.TestCase):

    def _deny(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "deny", f"{g.name}: {label} must DENY")

    def _allow(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "allow", f"{g.name}: {label} must ALLOW")

    def test_literal_mnt_subshell_still_denied(self):
        self._deny(f'(cd {ROOT}/mnt && cat "Dropbox/clients/f.pdf")',
                   "subshell literal mnt")

    def test_bash_c_literal_mnt_still_denied(self):
        self._deny(f'bash -c "cd {ROOT}/mnt && cat Dropbox/clients/f.pdf"',
                   "bash -c literal mnt")

    def test_benign_bash_c_echo_still_allowed(self):
        self._allow('bash -c "echo hi"', 'bash -c "echo hi"')

    def test_resolvable_cd_outputs_read_still_allowed(self):
        self._allow(f'cd {ROOT}/mnt/outputs && cat rt.txt',
                    "resolvable cd into outputs")

    def test_direct_alias_read_still_denied(self):
        self._deny(f'cat {ROOT}/mnt/Dropbox/clients/f.pdf', "direct alias read")

    def test_cd_tmp_read_allowed(self):
        self._allow('cd /tmp && cat x', "cd /tmp (resolvable) + read")


if __name__ == "__main__":
    unittest.main()
