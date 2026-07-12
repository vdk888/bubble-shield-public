"""Encryption-at-rest for the shadow store (Task 4).

The shadow store's `shadows` table holds CLEAN (unmasked) text — every client's
real names. At rest it must be encrypted with the vault's own crypto (PBKDF2 +
HMAC-CTR, encrypt-then-MAC), never plaintext SQLite. A wrong passphrase or a
tampered file must fail LOUD (raise), never silently return an empty store —
an empty store would look like "no known names" and re-leak PII downstream.
"""
import pytest
from pathlib import Path
from bubble_shield import shadow_store


def test_ondisk_artifact_is_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pass")
    shadow_store.put_shadow("h1", "le client Jean Dupont habite Paris")
    # the plaintext name must not appear in any on-disk byte of the store
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"Dupont" not in f.read_bytes(), f"leak in {f}"


def test_roundtrip_through_encryption(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pass")
    shadow_store.put_shadow("h1", "value-A")
    assert shadow_store.get_shadow("h1") == "value-A"


def test_wrong_passphrase_fails_loud(tmp_path, monkeypatch):
    """Fail-toward-masking: a wrong passphrase must RAISE, not silently return
    an empty/garbage store (which downstream reads as 'no known names' → leak)."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "right-pass")
    shadow_store.put_shadow("h1", "le client Jean Dupont")
    # Reopen with the WRONG passphrase — must not silently succeed.
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "wrong-pass")
    with pytest.raises(Exception):
        shadow_store.get_shadow("h1")


def test_tampered_file_fails_loud(tmp_path, monkeypatch):
    """A tampered encrypted artifact must RAISE (MAC verify fails), never
    decrypt to garbage or an empty store."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pass")
    shadow_store.put_shadow("h1", "le client Jean Dupont")
    enc = tmp_path / "shield.db.enc"
    assert enc.is_file(), "expected encrypted artifact on disk"
    import json
    envelope = json.loads(enc.read_text(encoding="utf-8"))
    # Flip a byte in the ciphertext (MAC must catch it).
    import base64
    ct = bytearray(base64.b64decode(envelope["ct"]))
    ct[0] ^= 0xFF
    envelope["ct"] = base64.b64encode(bytes(ct)).decode("ascii")
    enc.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(Exception):
        shadow_store.get_shadow("h1")


def test_mid_write_exception_leaves_no_plaintext(tmp_path, monkeypatch):
    """Finding 1 (review): if the write inside put_shadow raises mid-flight,
    the plaintext SQLite working copy (shield.db) must NOT survive on disk.
    Before the fix, _seal() sat AFTER the try/finally, so an exception during
    conn.execute/commit skipped it entirely, stranding a plaintext shield.db
    containing every prior client's clean name. Only shield.db.enc may remain."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pass")
    # Seed a prior write so there is an existing encrypted store to decrypt
    # into a working copy on the next connect() (mirrors the real leak: prior
    # client names sitting decrypted on disk during the failing write).
    shadow_store.put_shadow("h0", "le client Jean Dupont")

    import sqlite3

    real_connect = shadow_store.sqlite3.connect

    class _BoomingConnection:
        """Wraps a real sqlite3.Connection but raises on execute(), simulating
        a mid-write failure (e.g. disk full, constraint violation) after
        connect() has already produced a plaintext working copy on disk."""

        def __init__(self, real_conn):
            self._real = real_conn

        def executescript(self, *a, **kw):
            return self._real.executescript(*a, **kw)

        def commit(self, *a, **kw):
            return self._real.commit(*a, **kw)

        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("forced failure for test")

        def close(self, *a, **kw):
            return self._real.close(*a, **kw)

    def _fake_connect(path, *a, **kw):
        return _BoomingConnection(real_connect(path, *a, **kw))

    monkeypatch.setattr(shadow_store.sqlite3, "connect", _fake_connect)

    with pytest.raises(sqlite3.OperationalError):
        shadow_store.put_shadow("h1", "le client Marie Curie")

    plaintext_path = shadow_store.store_path()
    assert not plaintext_path.exists(), (
        "plaintext shield.db must not survive a mid-write exception"
    )
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"Dupont" not in f.read_bytes(), f"plaintext leak in {f}"
    assert (tmp_path / "shield.db.enc").is_file()


def test_seal_failure_still_drops_plaintext(tmp_path, monkeypatch):
    """Finding 2 (review): if _encrypt_bytes (or the envelope write) raises
    inside _seal(), the plaintext working copy must still be unlinked — the
    finally around the unlink must run even when sealing itself fails. We
    cannot produce a valid shield.db.enc in that case (fail loud, the
    exception propagates), but the plaintext must never strand on disk."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pass")

    def _boom_encrypt(plaintext, passphrase):
        raise RuntimeError("forced encryption failure for test")

    monkeypatch.setattr(shadow_store, "_encrypt_bytes", _boom_encrypt)

    with pytest.raises(RuntimeError):
        shadow_store.put_shadow("h1", "le client Marie Curie")

    plaintext_path = shadow_store.store_path()
    assert not plaintext_path.exists(), (
        "plaintext shield.db must be dropped even when _seal()'s encryption fails"
    )


def test_plaintext_fallback_warns_once_on_stderr(tmp_path, monkeypatch, capsys):
    """Finding 3 (review): with no passphrase set, the store falls back to
    plaintext at rest. That must not stay silent — a loud stderr warning
    (mirroring vault._warn_plaintext_at_rest) fires, once per process."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.delenv("BUBBLE_SHIELD_STORE_PASSPHRASE", raising=False)
    monkeypatch.delenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", raising=False)
    monkeypatch.setattr(shadow_store, "_warned_plaintext_fallback", False)

    shadow_store.put_shadow("h1", "value-A")
    err1 = capsys.readouterr().err
    assert "EN CLAIR" in err1 or "plaintext" in err1.lower()

    # Second call in the same process must NOT warn again (once-per-process).
    shadow_store.put_shadow("h2", "value-B")
    err2 = capsys.readouterr().err
    assert err2 == ""


def test_plaintext_fallback_warning_suppressible(tmp_path, monkeypatch, capsys):
    """The plaintext-fallback warning honors the same silence env var as the
    vault (BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1)."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.delenv("BUBBLE_SHIELD_STORE_PASSPHRASE", raising=False)
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    monkeypatch.setattr(shadow_store, "_warned_plaintext_fallback", False)

    shadow_store.put_shadow("h1", "value-A")
    err = capsys.readouterr().err
    assert err == ""
