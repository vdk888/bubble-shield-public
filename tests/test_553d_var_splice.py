#!/usr/bin/env python3
"""P1 SECURITY #553 (D) — SIMPLE LITERAL variable-assignment splice of a mount path.

The residual after #553-C
-------------------------
#553-C fail-closes on an UNRESOLVABLE `cd` (env var / `$(…)` / backtick / `cd -` /
glob) paired with a relative read. But a leak can be assembled with NO `cd` at all
and with the `/sessions/*/mnt/` literal NEVER appearing in the command text — by
stashing the mount prefix in a SIMPLE literal shell variable, then splicing it into
the read path:

    S=/sessions/foo; cat "$S/mnt/Dropbox/clients/f.pdf"   → ALLOW (LEAK)
    DIR="/sessions/foo/mnt"; cat "$DIR/Dropbox/x.pdf"      → ALLOW (LEAK)

There is NO `cd` (so #553-C's unresolvable-cd gate never fires), and the literal
`/sessions/*/mnt/` substring is spliced out of `$S`, so `_mentions_session_mnt`
returns False and the #553-B class gate misses it too. Both gates miss it and the
mnt-token classifier never sees a token to classify → ALLOW → PII leaks in clear.

The fix (a variable RESOLVER, not another pattern)
--------------------------------------------------
A pre-pass resolves SIMPLE, LITERAL assignments made earlier IN THE SAME command
(`VAR=<literal>`, bare or quoted, no `$`/`$(…)`/backtick/glob in the RHS), then
substitutes `$VAR`/`${VAR}` occurrences into the rest of the command BEFORE the
existing classification runs. The resolved command makes the hidden literal visible
so the EXISTING literal-mnt gates deny it. Substitution is for DETECTION ONLY — the
original command is what runs; the resolved form is only what the guard inspects.

Scope: only LITERAL RHS values are resolved. `VAR=$(cmd)`, `VAR=$OTHER`,
`VAR=`glob` are LEFT AS-IS (not recursively evaluated) — they remain covered by the
#553-C unresolvable-cd/opaque gates, or are a separate deeper residual.

Multi-var chains (`A=/sessions; B=foo; cat "$A/$B/mnt/…"`) ARE resolved (each var is
a simple literal), so that shape denies too.

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

ROOT = "/sessions/foo"


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


class VarSpliceResidualTest(unittest.TestCase):

    def _deny(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "deny", f"{g.name}: {label} must DENY")

    def _allow(self, cmd: str, label: str):
        for g in GUARDS:
            with self.subTest(guard=g.name, case=label):
                self.assertEqual(d(g, cmd), "allow", f"{g.name}: {label} must ALLOW")

    # --- (1) the residual LEAKS (ALLOW pre-fix, must DENY post-fix) -------------
    def test_var_splice_mnt_read_denied(self):
        self._deny('S=/sessions/foo; cat "$S/mnt/Dropbox/clients/f.pdf"',
                   "S=lit ; cat $S/mnt/…")

    # --- (2) quoted RHS + ${VAR} form -----------------------------------------
    def test_quoted_rhs_var_denied(self):
        self._deny('DIR="/sessions/foo/mnt"; cat "$DIR/Dropbox/x.pdf"',
                   'DIR="…/mnt" ; cat $DIR/…')

    def test_braced_var_form_denied(self):
        self._deny('DIR="/sessions/foo/mnt"; cat "${DIR}/Dropbox/x.pdf"',
                   'DIR="…/mnt" ; cat ${DIR}/…')

    # --- (3) other read verb ---------------------------------------------------
    def test_other_read_verb_denied(self):
        self._deny('S=/sessions/foo; base64 "$S/mnt/Dropbox/x"',
                   "base64 $S/mnt/…")

    # --- (4) multi-var chain (in scope: each var is a simple literal) ----------
    def test_multi_var_chain_denied(self):
        self._deny('A=/sessions; B=foo; cat "$A/$B/mnt/Dropbox/x"',
                   "A=/sessions ; B=foo ; cat $A/$B/mnt/…")

    # --- (5) NO over-block -----------------------------------------------------
    def test_benign_tmp_var_allowed(self):
        self._allow('S=/tmp; cat "$S/x"', "S=/tmp ; cat $S/x (benign)")

    def test_infra_var_read_allowed(self):
        # resolves to /sessions/foo/mnt/outputs/r.txt — a direct infra read → ALLOW.
        self._allow('D=/sessions/foo/mnt/outputs; cat "$D/r.txt"',
                    "D=…/mnt/outputs ; cat $D/r.txt (infra)")

    def test_unresolved_cmdsubst_rhs_allowed(self):
        # VAR=$(pwd) is NOT a literal → left as-is; no cd, no relative read of a
        # protected token → no existing gate catches it → ALLOW.
        self._allow('VAR=$(pwd); cat "$VAR/x"',
                    "VAR=$(pwd) ; cat $VAR/x (unresolved RHS)")

    def test_no_assignment_unchanged_allowed(self):
        self._allow('cat /tmp/whatever.txt', "no assignment, benign absolute read")

    def test_var_indirection_rhs_left_as_is_allowed(self):
        # VAR=$OTHER indirection is NOT resolved (documented out-of-scope). With no
        # cd and no literal protected token it falls through the existing gates.
        self._allow('OTHER=/tmp; VAR=$OTHER; cat "$VAR/x"',
                    "VAR=$OTHER indirection (not resolved) → ALLOW")

    # --- direct (non-spliced) reads still behave as before ---------------------
    def test_direct_alias_read_still_denied(self):
        self._deny('cat /sessions/foo/mnt/Dropbox/clients/f.pdf',
                   "direct alias read (no var)")

    def test_direct_infra_read_still_allowed(self):
        self._allow('cat /sessions/foo/mnt/outputs/r.txt',
                    "direct infra read (no var)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
