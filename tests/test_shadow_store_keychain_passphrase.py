"""Bug #759 — `_passphrase()` must fall back to the macOS System keychain when
BUBBLE_SHIELD_STORE_PASSPHRASE is unset, so launchd-started consumers (minid,
sweep, MCP server) that lack the env var can still decrypt an encrypted shadow
store. Env var still wins for back-compat + testability; any failure of the
keychain read is fail-safe (returns None → plaintext fallback, never raises)."""
import subprocess
import pytest
from bubble_shield import shadow_store


def _fake_completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_env_wins_keychain_not_consulted(monkeypatch):
    """T1: env set → return env value, and subprocess.run is never called."""
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "envval")

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must NOT be called when env is set")

    monkeypatch.setattr(shadow_store.subprocess, "run", _boom)
    assert shadow_store._passphrase() == "envval"


def test_keychain_fallback_when_env_unset(monkeypatch):
    """T2: env unset → keychain read returns a value → that value is used."""
    monkeypatch.delenv("BUBBLE_SHIELD_STORE_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        shadow_store.subprocess, "run",
        lambda *a, **k: _fake_completed(returncode=0, stdout="kcpass\n"),
    )
    assert shadow_store._passphrase() == "kcpass"


def test_keychain_miss_returns_none(monkeypatch):
    """T3: env unset → keychain item not found (rc=44, empty stdout) → None."""
    monkeypatch.delenv("BUBBLE_SHIELD_STORE_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        shadow_store.subprocess, "run",
        lambda *a, **k: _fake_completed(returncode=44, stdout=""),
    )
    assert shadow_store._passphrase() is None


def test_fail_safe_when_security_missing(monkeypatch):
    """T4: env unset → no /usr/bin/security (FileNotFoundError) → None, no raise."""
    monkeypatch.delenv("BUBBLE_SHIELD_STORE_PASSPHRASE", raising=False)

    def _raise(*a, **k):
        raise FileNotFoundError("/usr/bin/security")

    monkeypatch.setattr(shadow_store.subprocess, "run", _raise)
    assert shadow_store._passphrase() is None


def test_empty_env_falls_through_to_keychain(monkeypatch):
    """T5: empty env var must NOT count as set → falls through to keychain."""
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "")
    monkeypatch.setattr(
        shadow_store.subprocess, "run",
        lambda *a, **k: _fake_completed(returncode=0, stdout="kcpass\n"),
    )
    assert shadow_store._passphrase() == "kcpass"
