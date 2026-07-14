"""
tests/test_guard_transient_retry.py — guard resilience fixes (2026-07-14).

Symptom (Joris): the PreToolUse guard intermittently fail-CLOSED with the generic
"erreur interne du guard" on legit ops (git commit/add, Telegram send) and CLEARED
on identical retry — a false block under concurrent-hook I/O pressure, not a policy
hit. Three fixes:

  FIX 1 — log the real traceback (redacted: tool name only, never tool_input/PII)
          to ~/.bubble_shield/guard_errors.log before failing closed.
  FIX 2 — retry the (idempotent, read-only) decision a few times on a TRANSIENT
          OSError before failing closed; a non-transient error still fails closed
          immediately (no retry). Never retries into an ALLOW.
  FIX 3 — run guard as an imported MODULE (import guard; guard.main()) so its .pyc
          caches instead of recompiling the 88KB source every fire.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import guard as guardmod  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _run_main(monkeypatch, stdin: str = '{"tool_name":"Bash"}'):
    """Run guard.main() with `stdin`, capturing stdout and the SystemExit code.
    Returns (exit_code, stdout_text)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = None
    try:
        guardmod.main()
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def _last_decision(stdout: str) -> dict:
    """Parse the last JSON line the guard printed (the deny/allow decision)."""
    lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
    return json.loads(lines[-1]) if lines else {}


# ── FIX 2: transient-OSError retry ───────────────────────────────────────────

def test_transient_oserror_retries_then_succeeds(monkeypatch):
    """A transient OSError on the first attempt is retried; the retry succeeds →
    the guard reaches a NORMAL allow, NOT the fail-closed deny."""
    calls = {"n": 0}

    def flaky_main(raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(24, "Too many open files")  # EMFILE — transient
        guardmod._allow()  # 2nd attempt: normal allow

    monkeypatch.setattr(guardmod, "_main", flaky_main)
    monkeypatch.setattr(guardmod, "_log_guard_error", lambda *a, **k: None)
    code, out = _run_main(monkeypatch)
    assert calls["n"] == 2, "must have retried after the transient OSError"
    assert code == 0
    # _allow() with no reason prints nothing; the key assertion is NO deny emitted.
    assert "erreur interne" not in out


def test_transient_oserror_retry_can_still_deny(monkeypatch):
    """The retry re-runs the REAL decision — if the (now-succeeding) decision is a
    legit block, it still denies. Retry must not coerce toward allow."""
    calls = {"n": 0}

    def flaky_main(raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(4, "Interrupted system call")  # EINTR
        guardmod._deny("🔒 legit policy block")

    monkeypatch.setattr(guardmod, "_main", flaky_main)
    monkeypatch.setattr(guardmod, "_log_guard_error", lambda *a, **k: None)
    code, out = _run_main(monkeypatch)
    dec = _last_decision(out)
    assert dec["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "legit policy block" in dec["hookSpecificOutput"]["permissionDecisionReason"]


def test_persistent_transient_error_fails_closed_after_retries(monkeypatch):
    """If the transient OSError never clears, after _MAX_RETRIES the guard fails
    CLOSED (generic deny) — never falls through to allow."""
    calls = {"n": 0}

    def always_flaky(raw):
        calls["n"] += 1
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(guardmod, "_main", always_flaky)
    monkeypatch.setattr(guardmod, "_log_guard_error", lambda *a, **k: None)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    code, out = _run_main(monkeypatch)
    assert calls["n"] == guardmod._MAX_RETRIES, "exhausts all attempts"
    dec = _last_decision(out)
    assert dec["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "erreur interne" in dec["hookSpecificOutput"]["permissionDecisionReason"]


def test_non_transient_error_fails_closed_without_retry(monkeypatch):
    """A logic bug (TypeError) is NOT a transient I/O error → fail closed
    IMMEDIATELY, no retries (retrying a deterministic bug is pointless)."""
    calls = {"n": 0}

    def buggy_main(raw):
        calls["n"] += 1
        raise TypeError("cwd was an int")

    monkeypatch.setattr(guardmod, "_main", buggy_main)
    logged = {"final": None}
    monkeypatch.setattr(guardmod, "_log_guard_error",
                        lambda raw, attempt, final: logged.__setitem__("final", final))
    code, out = _run_main(monkeypatch)
    assert calls["n"] == 1, "non-transient error must NOT be retried"
    assert logged["final"] is True
    dec = _last_decision(out)
    assert dec["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_is_transient_oserror_classification():
    assert guardmod._is_transient_oserror(OSError(24, "EMFILE")) is True
    assert guardmod._is_transient_oserror(OSError(4, "EINTR")) is True
    assert guardmod._is_transient_oserror(FileNotFoundError()) is True   # bare OSError, errno None
    assert guardmod._is_transient_oserror(TypeError("nope")) is False
    assert guardmod._is_transient_oserror(KeyError("nope")) is False
    # A non-retryable errno (e.g. EACCES 13) is a real permission problem, still fail closed.
    assert guardmod._is_transient_oserror(OSError(13, "EACCES")) is False


def test_systemexit_from_main_propagates(monkeypatch):
    """_deny/_allow raise SystemExit(0) — main must let those through, not treat
    them as errors to retry/log."""
    def normal(raw):
        guardmod._allow("explicit allow reason")

    monkeypatch.setattr(guardmod, "_main", normal)
    code, out = _run_main(monkeypatch)
    assert code == 0
    dec = _last_decision(out)
    assert dec["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── FIX 1: traceback logging (redacted) ──────────────────────────────────────

def test_log_writes_traceback_without_pii(monkeypatch, tmp_path):
    """The error log records the tool name + traceback, but NEVER the tool_input
    (which can carry client paths/PII)."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    raw = json.dumps({"tool_name": "Bash",
                      "tool_input": {"command": "cat /Clients/Dupont/avis.pdf"}})
    try:
        raise OSError(24, "boom")
    except OSError:
        guardmod._log_guard_error(raw, attempt=3, final=True)
    log = tmp_path / ".bubble_shield" / "guard_errors.log"
    assert log.is_file()
    text = log.read_text()
    assert "Bash" in text, "tool name is logged"
    assert "OSError" in text, "the exception is logged"
    assert "Dupont" not in text, "tool_input / PII must NEVER be logged"
    assert "avis.pdf" not in text
    assert "FAIL-CLOSED" in text


def test_log_never_raises(monkeypatch):
    """A logging failure must not turn into another guard failure."""
    def boom_home():
        raise OSError("no home")
    monkeypatch.setattr(Path, "home", staticmethod(boom_home))
    # Must simply return, not raise.
    guardmod._log_guard_error('{"tool_name":"Read"}', attempt=1, final=False)


# ── FIX 3: guard runs as an imported module (byte-cached) ─────────────────────

def test_wrapped_cmd_guard_uses_module_import():
    """The self-installer builds the guard hook as `import guard; guard.main()`
    (module → .pyc-cached), while other scripts stay `python3 <path>`."""
    import install_user_hooks as ih
    guard_cmd = ih._wrapped_cmd("guard.py")
    assert "import guard" in guard_cmd and "guard.main()" in guard_cmd
    assert "python3 -c" in guard_cmd
    # Preserves the safety net + marker so stale-entry replacement still matches.
    assert "|| exit 0" in guard_cmd
    assert f"{ih.MARKER}:guard.py" in guard_cmd
    assert "guard.py" in guard_cmd  # the [ -f '.../guard.py' ] existence check
    # A different script is unchanged (plain path invocation).
    trip_cmd = ih._wrapped_cmd("tripwire.py")
    assert "import guard" not in trip_cmd
    assert "'/tripwire.py' " in trip_cmd or "tripwire.py'" in trip_cmd


def test_hooks_json_guard_uses_module_import():
    """The plugin hooks.json guard command also imports the module."""
    hj = json.loads((REPO / "plugin" / "bubble-shield" / "hooks" / "hooks.json").read_text())
    pre = hj["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "import guard" in pre and "guard.main()" in pre
    assert "sys.path.insert" in pre
