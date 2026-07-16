"""
test_guard_560_host_relax.py — #560: relax the Cowork-SANDBOX-specific #553-C
guard gates on a POSITIVELY-CONFIRMED host, keep them strict everywhere else.

STRATEGIC BASIS (Joris 2026-07-16): Shield's future is HOST Claude Code, outside
Cowork. The #553-C gates (opaque-eval, unresolvable-cd + relative-read) defend a
sandbox-mount-escape threat that CANNOT exist on the host (no /sessions/*/mnt/).
Probe-verified: real protected-file reads still DENY on the host via the marker
walk-up WITHOUT these gates — so relaxing them on a confirmed host loses zero
protection and removes pure friction.

FAIL-SAFE (critical): relax ONLY when POSITIVELY host. Uncertain / any Cowork
signal → stay strict. So a Cowork session whose env is unexpectedly sparse still
gets the strict gates (default-deny direction preserved).

CONTRACT under test:
  confirmed host (HOME=/Users/…, no cowork env, no /sessions/):
    - opaque eval "$(…)"                 → ALLOWED (gate relaxed)
    - unresolvable cd + relative read    → ALLOWED (gate relaxed)
    - BUT a real protected-file read     → STILL DENY (marker walk-up unaffected)
    - #553-B (literal mnt + hiding)      → still DENY (left strict, harmless)
  sandbox / uncertain:
    - opaque eval                        → DENY (strict)
    - unresolvable cd + relative read    → DENY (strict)

Runs the REAL guard.py as a subprocess. Synthetic values only.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard.py"

# Assemble trigger strings at runtime so THIS test file doesn't itself trip a
# host guard scanning the repo (the opaque-eval / mnt literals are the triggers).
_EVAL = "e" + "val"
_OPAQUE = _EVAL + ' "$(echo ls)"'
_UNRESOLV_CD_REL = 'cd "$DIR" && cat notes/todo.txt'
_MNT = "/sessions/" + "abc/mnt"


def _run(command, *, home, cwd="/work", extra_env=None, protected=None):
    with tempfile.TemporaryDirectory() as td:
        cfg_obj = {"protected_folders": [protected] if protected else []}
        cfg = Path(td) / "cfg.json"
        cfg.write_text(json.dumps(cfg_obj))
        env = dict(os.environ)
        # scrub any inherited cowork signals, then set the scenario's HOME
        for k in ("CLAUDE_CODE_IS_COWORK", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(k, None)
        env.update(BUBBLE_SHIELD_GUARD_CONFIG=str(cfg), HOME=home,
                   CLAUDE_PROJECT_DIR=td, BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN="1")
        if extra_env:
            env.update(extra_env)
        ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(ev),
                           capture_output=True, text=True, env=env)
    out = p.stdout.strip()
    if not out:
        return "allow-noop"
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return "parse-error:" + out[:100]


HOST_HOME = "/Users/someone"          # a real host HOME
SANDBOX_HOME = "/sessions/xyz"        # Cowork VM HOME


# ── confirmed host: the two #553-C gates RELAX ───────────────────────────────

def test_host_opaque_eval_allowed():
    assert _run(_OPAQUE, home=HOST_HOME) in ("allow", "allow-noop")


def test_host_unresolvable_cd_relative_allowed():
    assert _run(_UNRESOLV_CD_REL, home=HOST_HOME,
                extra_env={"DIR": "/tmp/x"}) in ("allow", "allow-noop")


# ── confirmed host: REAL protection is UNAFFECTED ────────────────────────────

def test_host_protected_read_still_denies():
    with tempfile.TemporaryDirectory() as td:
        prot = Path(td) / "ClientFolder"
        prot.mkdir()
        (prot / ".bubble-shield.json").write_text("{}")
        got = _run(f'cat {prot}/dossier.pdf', home=HOST_HOME, protected=str(prot))
        assert got == "deny"


def test_host_553b_literal_still_denies():
    # #553-B (literal mnt + hiding construct) left strict even on host — harmless,
    # and defends the (rare) case a host command really references the mnt path.
    assert _run(f'({_EVAL} echo hi; cat {_MNT}/Dropbox/x.pdf)', home=HOST_HOME) == "deny"


# ── sandbox / uncertain: gates STAY strict ───────────────────────────────────

def test_sandbox_opaque_eval_denies():
    assert _run(_OPAQUE, home=SANDBOX_HOME) == "deny"


def test_sandbox_unresolvable_cd_relative_denies():
    assert _run(_UNRESOLV_CD_REL, home=SANDBOX_HOME,
                extra_env={"DIR": "/tmp/x"}) == "deny"


def test_cowork_env_var_keeps_strict_even_with_host_home():
    # HOME looks host-y but a Cowork env signal is present → NOT confirmed host →
    # stay strict. Fail-safe: any cowork signal wins.
    assert _run(_OPAQUE, home=HOST_HOME,
                extra_env={"CLAUDE_CODE_IS_COWORK": "1"}) == "deny"


def test_entrypoint_local_agent_keeps_strict():
    assert _run(_OPAQUE, home=HOST_HOME,
                extra_env={"CLAUDE_CODE_ENTRYPOINT": "local-agent"}) == "deny"


def test_unknown_home_keeps_strict():
    # An unrecognised HOME (not /Users, not /root) → can't confirm host → strict.
    assert _run(_OPAQUE, home="/opt/weird") == "deny"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
