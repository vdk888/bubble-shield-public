"""test_vault_atomic_save.py — Vault.save must be atomic (crash mid-write must
not destroy the existing vault). #345.

The vault is the ONLY store holding the reversible token<->cleartext-PII map;
a truncated/empty vault means documents already anonymised with it can never be
decloaked again. Vault.save must write to a temp file then os.replace() it into
place, so a crash between write and replace leaves the prior file intact.
"""
from __future__ import annotations

import json

from bubble_shield.vault import Vault


def test_vault_save_is_atomic_under_crash(tmp_path, monkeypatch):
    """If the write crashes mid-save, the PREVIOUS vault file must survive intact."""
    path = tmp_path / "m.vault.json"

    # 1. Save a good vault first.
    v1 = Vault(mission="m")
    v1.token_for("Original Value", "NOM")
    v1.save(path)
    assert path.exists()
    original_bytes = path.read_bytes()

    # 2. Mutate, then simulate a crash DURING the temp-file write.
    v2 = Vault.load(path)
    v2.token_for("Second Value", "NOM")

    real_write_text = type(path).write_text

    def _boom(self, *a, **kw):
        # Let the .tmp write begin, then blow up — emulating a crash mid-write.
        real_write_text(self, *a, **kw)
        raise OSError("simulated crash during write")

    monkeypatch.setattr(type(path), "write_text", _boom, raising=True)
    try:
        v2.save(path)
    except OSError:
        pass  # expected — the crash
    monkeypatch.undo()

    # 3. The ORIGINAL vault file must be intact (atomic save never half-wrote it).
    assert path.exists(), "vault file disappeared after a crashed save"
    assert path.read_bytes() == original_bytes, "vault was truncated/corrupted by a crashed save"
    # And it must still load + decloak the original token.
    restored = Vault.load(path)
    assert "Original Value" in restored.to_token


def test_vault_save_no_tmp_left_behind(tmp_path):
    """A successful save leaves no stray .tmp file."""
    path = tmp_path / "m.vault.json"
    v = Vault(mission="m")
    v.token_for("Some Value", "NOM")
    v.save(path)
    assert path.exists()
    assert not (tmp_path / "m.vault.tmp").exists()
    # round-trips fine
    assert json.loads(path.read_text())["mission"] == "m"
