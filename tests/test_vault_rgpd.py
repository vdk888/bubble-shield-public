"""
test_vault_rgpd.py — RGPD-compliance features on the Vault.

The vault concentrates ALL the cleartext PII in one place, so it is the
highest-value target and the focal point of our RGPD obligations:
  - art. 32  → security of processing  → encryption at rest (Feature A)
  - art. 17  → right to erasure        → forget / forget_subject (Feature B)
  - art. 16  → right to rectification  → rectify (Feature B)
  - art. 5-1-e → storage limitation    → TTL / purge_expired (Feature C)

All fixtures use SYNTHETIC data only.
"""
from __future__ import annotations

import json

import pytest

from bubble_shield.vault import Vault, purge_expired


# ── Feature A — encryption at rest (art. 32) ───────────────────────────────

def test_encrypted_file_has_no_plaintext_pii(tmp_path):
    """The whole point: a stolen vault file must reveal nothing."""
    v = Vault(mission="dossier-x")
    v.token_for("Jean Dupont", "NOM")
    v.token_for("jean@x.fr", "EMAIL")
    p = tmp_path / "vault.enc"
    v.save_encrypted(p, passphrase="correct horse battery")

    raw = p.read_bytes()
    assert b"Jean Dupont" not in raw
    assert "Jean Dupont".encode("utf-8") not in raw
    assert b"jean@x.fr" not in raw
    # mission name / json structure must not leak either
    assert b"dossier-x" not in raw
    assert b"to_token" not in raw


def test_encrypted_roundtrip(tmp_path):
    v = Vault(mission="dossier-x")
    tok = v.token_for("Jean Dupont", "NOM")
    p = tmp_path / "vault.enc"
    v.save_encrypted(p, passphrase="s3cret")

    loaded = Vault.load_encrypted(p, passphrase="s3cret")
    assert loaded.mission == "dossier-x"
    assert loaded.value_for(tok) == "Jean Dupont"
    assert loaded.to_token == v.to_token
    assert loaded.to_value == v.to_value


def test_wrong_passphrase_raises(tmp_path):
    v = Vault()
    v.token_for("Marie Durand", "NOM")
    p = tmp_path / "vault.enc"
    v.save_encrypted(p, passphrase="right")

    with pytest.raises(Exception):
        Vault.load_encrypted(p, passphrase="wrong")


def test_encrypted_file_is_chmod_600(tmp_path):
    import os
    import stat
    v = Vault()
    v.token_for("Jean Dupont", "NOM")
    p = tmp_path / "vault.enc"
    v.save_encrypted(p, passphrase="x")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    # owner-only (no group/other bits). Skip the assert on platforms that
    # don't honour chmod cleanly, but on POSIX it should be 0o600.
    if os.name == "posix":
        assert mode == 0o600


# ── Feature B — erasure / rectification (art. 17 & 16) ─────────────────────

def _sample_vault() -> Vault:
    v = Vault(mission="erasure")
    v.token_for("Jean Dupont", "NOM")
    v.token_for("Marie Durand", "NOM")
    v.token_for("jean@x.fr", "EMAIL")
    return v


def test_forget_removes_both_directions():
    v = _sample_vault()
    tok = v.to_token["jean@x.fr"]
    assert v.forget("jean@x.fr") is True
    assert "jean@x.fr" not in v.to_token
    assert tok not in v.to_value
    assert v.value_for(tok) is None


def test_forget_unknown_value_returns_false():
    v = _sample_vault()
    before = v.size
    assert v.forget("nobody@x.fr") is False
    assert v.size == before


def test_forget_subject_removes_all_matching_entries():
    v = Vault(mission="erasure")
    v.token_for("Jean Dupont", "NOM")          # Dupont
    v.token_for("M. Dupont", "NOM")            # Dupont variant
    v.token_for("dupont@x.fr", "EMAIL")        # Dupont email (lowercase d)
    v.token_for("Marie Durand", "NOM")         # NOT Dupont

    removed = v.forget_subject("Dupont")       # case-insensitive
    assert removed == 3
    assert "Marie Durand" in v.to_token
    # nothing Dupont-related left in either direction
    assert not any("dupont" in k.lower() for k in v.to_token)
    assert not any("dupont" in val.lower() for val in v.to_value.values())


def test_forget_subject_no_match_returns_zero():
    v = _sample_vault()
    assert v.forget_subject("Martin") == 0


def test_rectify_keeps_same_token():
    v = Vault(mission="rectify")
    tok = v.token_for("Jean Dupon", "NOM")     # typo in stored value
    assert v.rectify("Jean Dupon", "Jean Dupont") is True

    # same token, new real value, old value gone
    assert v.to_token.get("Jean Dupont") == tok
    assert "Jean Dupon" not in v.to_token
    assert v.value_for(tok) == "Jean Dupont"
    assert v.restore(f"client {tok}") == "client Jean Dupont"


def test_rectify_unknown_value_returns_false():
    v = _sample_vault()
    assert v.rectify("ghost", "phantom") is False


# ── Feature C — TTL / auto-expiry (art. 5-1-e) ─────────────────────────────

def test_created_at_is_set_and_persisted(tmp_path):
    v = Vault()
    assert v.created_at  # ISO string set at construction
    d = v.to_dict()
    assert d["created_at"] == v.created_at
    restored = Vault.from_dict(d)
    assert restored.created_at == v.created_at


def test_is_expired():
    from datetime import datetime, timedelta, timezone
    v = Vault()
    # fresh vault is not expired
    assert v.is_expired(ttl_days=30) is False
    # backdate creation by 40 days
    v.created_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    assert v.is_expired(ttl_days=30) is True
    assert v.is_expired(ttl_days=90) is False


def test_purge_expired_deletes_only_old_vault_files(tmp_path):
    from datetime import datetime, timedelta, timezone

    old = Vault(mission="old")
    old.token_for("Jean Dupont", "NOM")
    old.created_at = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    old.save(tmp_path / "old.json")

    fresh = Vault(mission="fresh")
    fresh.token_for("Marie Durand", "NOM")
    fresh.save(tmp_path / "fresh.json")

    # an unrelated json file that is NOT a vault — must be left alone
    (tmp_path / "notes.json").write_text(json.dumps({"hello": "world"}))

    purged = purge_expired(tmp_path, ttl_days=30)

    purged_names = {p.name for p in purged}
    assert "old.json" in purged_names
    assert "fresh.json" not in purged_names
    assert "notes.json" not in purged_names

    assert not (tmp_path / "old.json").exists()
    assert (tmp_path / "fresh.json").exists()
    assert (tmp_path / "notes.json").exists()


def test_purge_expired_ignores_non_vault_json(tmp_path):
    # a json file missing the expected vault keys should never be deleted
    (tmp_path / "config.json").write_text(json.dumps({"foo": 1, "bar": 2}))
    purged = purge_expired(tmp_path, ttl_days=0)  # ttl 0 = expire everything
    assert purged == []
    assert (tmp_path / "config.json").exists()
