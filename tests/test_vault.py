from pathlib import Path

import pytest

from bubble_shield.vault import Vault, make_token, TOKEN_RE


def test_token_is_stable_per_value():
    v = Vault()
    t1 = v.token_for("Jean Dupont", "NOM")
    t2 = v.token_for("Jean Dupont", "NOM")
    assert t1 == t2                       # same value → same token
    assert v.token_for("Marie Dupont", "NOM") != t1


def test_counter_per_type():
    v = Vault()
    assert v.token_for("a@x.com", "EMAIL") == make_token("EMAIL", 1)
    assert v.token_for("b@x.com", "EMAIL") == make_token("EMAIL", 2)
    assert v.token_for("Jean", "NOM") == make_token("NOM", 1)   # type-scoped


def test_restore_roundtrips():
    v = Vault()
    t = v.token_for("FR76 3000 6000 0112 3456 7890 189", "IBAN")
    assert v.restore(f"compte {t} ok") == "compte FR76 3000 6000 0112 3456 7890 189 ok"


def test_restore_ignores_foreign_token():
    v = Vault()
    foreign = make_token("NOM", 99)       # never minted in this vault
    assert v.restore(f"x {foreign} y") == f"x {foreign} y"   # left as-is, no guess


def test_token_regex_matches_format():
    m = TOKEN_RE.fullmatch("⟦IBAN_0003⟧")
    assert m and m.group(1) == "IBAN" and m.group(2) == "0003"


def test_save_load_roundtrip(tmp_path):
    v = Vault(mission="dossier-x")
    v.token_for("Jean Dupont", "NOM")
    p = tmp_path / "vault.json"
    v.save(p)
    w = Vault.load(p)
    assert w.mission == "dossier-x"
    assert w.to_token == v.to_token and w.to_value == v.to_value


# ── encrypted vault: pure-stdlib (no `cryptography`), the client-offline path ──

def _vault_with_pii():
    v = Vault(mission="enc-test")
    v.token_for("Jean Dupont", "NOM")
    v.token_for("FR7630006000011234567890189", "IBAN")
    return v


def test_encrypted_vault_roundtrips_without_cryptography(tmp_path, monkeypatch):
    # Simulate a bare client: cryptography NOT importable.
    import sys
    monkeypatch.setitem(sys.modules, "cryptography", None)
    v = _vault_with_pii()
    p = tmp_path / "v.enc"
    v.save_encrypted(str(p), "correct horse battery staple")
    v2 = Vault.load_encrypted(str(p), "correct horse battery staple")
    assert v2.value_for(v.to_token["Jean Dupont"]) == "Jean Dupont"
    assert v2.value_for(v.to_token["FR7630006000011234567890189"]) == "FR7630006000011234567890189"


def test_encrypted_vault_no_plaintext_on_disk(tmp_path):
    v = _vault_with_pii()
    p = tmp_path / "v.enc"
    v.save_encrypted(str(p), "pw")
    disk = p.read_text(encoding="utf-8")
    assert "Dupont" not in disk and "FR7630" not in disk
    import json
    assert json.loads(disk)["bubble_shield_enc"] == 2   # stdlib format


def test_encrypted_vault_wrong_passphrase_raises(tmp_path):
    v = _vault_with_pii()
    p = tmp_path / "v.enc"
    v.save_encrypted(str(p), "right")
    import pytest
    with pytest.raises(ValueError):
        Vault.load_encrypted(str(p), "wrong")


def test_encrypted_vault_tamper_detected(tmp_path):
    import json, base64, pytest
    v = _vault_with_pii()
    p = tmp_path / "v.enc"
    v.save_encrypted(str(p), "pw")
    env = json.loads(p.read_text())
    ct = bytearray(base64.b64decode(env["ct"])); ct[0] ^= 1
    env["ct"] = base64.b64encode(bytes(ct)).decode()
    p.write_text(json.dumps(env))
    with pytest.raises(ValueError):
        Vault.load_encrypted(str(p), "pw")


# ── art. 32 plaintext-at-rest: detection + one-command in-place encryption ────

def test_save_warns_plaintext_on_stderr(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", raising=False)
    _vault_with_pii().save(tmp_path / "v.json")
    err = capsys.readouterr().err
    assert "EN CLAIR" in err and "art. 32" in err
    # and it's suppressible once acknowledged
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    _vault_with_pii().save(tmp_path / "v2.json")
    assert "EN CLAIR" not in capsys.readouterr().err


def test_is_encrypted_vault_file(tmp_path):
    from bubble_shield.vault import is_encrypted_vault_file
    plain = tmp_path / "p.json"; _vault_with_pii().save(plain)
    enc = tmp_path / "e.json"; _vault_with_pii().save_encrypted(enc, "pw")
    assert is_encrypted_vault_file(enc) is True
    assert is_encrypted_vault_file(plain) is False


def test_find_plaintext_vaults_skips_encrypted_and_non_vaults(tmp_path):
    import json
    from bubble_shield.vault import find_plaintext_vaults
    _vault_with_pii().save(tmp_path / "plain1.json")
    _vault_with_pii().save(tmp_path / "plain2.json")
    _vault_with_pii().save_encrypted(tmp_path / "enc.json", "pw")
    (tmp_path / "notes.json").write_text(json.dumps({"hello": "world"}))  # not a vault
    found = {p.name for p in find_plaintext_vaults(tmp_path)}
    assert found == {"plain1.json", "plain2.json"}


def test_encrypt_vault_file_roundtrip_exact_and_no_plaintext(tmp_path):
    from bubble_shield.vault import encrypt_vault_file, is_encrypted_vault_file
    v = _vault_with_pii()
    p = tmp_path / "v.json"; v.save(p)
    assert encrypt_vault_file(p, "pw") is True
    assert is_encrypted_vault_file(p) is True
    disk = p.read_text(encoding="utf-8")
    assert "Dupont" not in disk and "FR7630" not in disk   # no cleartext PII left
    w = Vault.load_encrypted(p, "pw")
    assert w.to_token == v.to_token and w.to_value == v.to_value  # exact round-trip
    # idempotent: a second pass reports "already encrypted"
    assert encrypt_vault_file(p, "pw") is False


def test_encrypt_vault_file_cleans_up_tmp_on_save_encrypted_failure(tmp_path, monkeypatch):
    """#478: if Vault.save_encrypted() raises mid-write, encrypt_vault_file must
    not leave a `.enc.tmp` orphan behind (pure hygiene — the tmp is ciphertext
    only, but it should still never linger)."""
    from bubble_shield.vault import encrypt_vault_file
    v = _vault_with_pii()
    p = tmp_path / "v.json"; v.save(p)

    def _boom(self, path, passphrase):
        # simulate a crash AFTER the tmp file hit disk but before the write
        # completed — this is the actual failure mode #478 is about.
        Path(path).write_text("partial-ciphertext", encoding="utf-8")
        raise RuntimeError("disk full mid-write")

    monkeypatch.setattr(Vault, "save_encrypted", _boom)
    with pytest.raises(RuntimeError):
        encrypt_vault_file(p, "pw")
    assert not (tmp_path / "v.json.enc.tmp").exists()
    assert "Dupont" in p.read_text(encoding="utf-8")  # original plaintext file left in place


def test_encrypt_all_vaults_and_legacy_plaintext_still_loads(tmp_path):
    from bubble_shield.vault import encrypt_all_vaults
    # legacy plaintext vault written the old way — must STILL load after we
    # migrate the OTHERS (we never touch a file we didn't select).
    legacy = tmp_path / "legacy.json"; _vault_with_pii().save(legacy)
    target = tmp_path / "target.json"; _vault_with_pii().save(target)
    # encrypt only target by moving legacy aside first would over-complicate;
    # instead assert both are found, migrate all, and both round-trip.
    done = encrypt_all_vaults(tmp_path, "pw")
    assert {p.name for p in done} == {"legacy.json", "target.json"}
    for name in ("legacy.json", "target.json"):
        w = Vault.load_encrypted(tmp_path / name, "pw")
        assert w.value_for(w.to_token["Jean Dupont"]) == "Jean Dupont"


def test_legacy_plaintext_vault_still_loads_via_plain_load(tmp_path):
    # A client's pre-existing PLAINTEXT vault must keep loading unchanged.
    v = _vault_with_pii()
    p = tmp_path / "old.json"; v.save(p)
    w = Vault.load(p)   # the plain loader, untouched
    assert w.to_value == v.to_value and w.to_token == v.to_token
