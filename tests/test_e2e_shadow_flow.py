"""Task 13 — end-to-end integration proof for the shadow-index redesign.

Proves success criteria 1-4 of the spec against the REAL modules
(shadow_index.run_sweep, shadow_store, and bubble_shield_mcp._read_with_shadow):

  1. A background sweep pre-anonymises a folder and caches the shadow keyed by
     content hash.
  2. A subsequent read of an indexed doc serves the CACHED masked text — the
     real name is gone, the ⟦NOM_0001⟧ token is present.
  3. That read is MODEL-FREE: _anonymise_file is sabotaged to raise if called,
     so any secret model call at read time crashes the test.
  4. Rename-stability: because the store is keyed by content hash (not path),
     renaming a file (same bytes → same hash) still serves the cached clean
     text on the new path, again with zero models.

Only the anonymize_fn is faked (a lambda standing in for GLiNER+Gemma) so no
real models are needed; every other line is the real production code path,
including the encrypted-at-rest store (BUBBLE_SHIELD_STORE_PASSPHRASE set).
"""

import sys
from pathlib import Path

from bubble_shield import shadow_index, shadow_store

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "plugin/bubble-shield/scripts"))
sys.path.insert(0, str(HERE.parent / "plugin/bubble-shield/vendor"))
import bubble_shield_mcp as M


def test_sweep_then_read_is_cached_and_model_free(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "e2e-test-pass")
    root = tmp_path / "protected"; root.mkdir()
    doc = root / "kyc.txt"; doc.write_text("Client: Jean Dupont, IBAN FR76...")
    # background sweep uses a fake full-anonymizer (stands in for GLiNER+Gemma)
    shadow_index.run_sweep(str(root), anonymize_fn=lambda p: "Client: ⟦NOM_0001⟧, IBAN ⟦IBAN_0001⟧")
    # now a read must serve the cached shadow with NO model call
    monkeypatch.setattr(M, "_anonymise_file", lambda p: (_ for _ in ()).throw(AssertionError("models on read")))
    out = M._read_with_shadow(str(doc))
    assert "Jean Dupont" not in out and "⟦NOM_0001⟧" in out


def test_rename_still_served_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "e2e-test-pass")
    root = tmp_path / "p"; root.mkdir()
    doc = root / "old.txt"; doc.write_text("Dupont file")
    shadow_index.run_sweep(str(root), anonymize_fn=lambda p: "⟦NOM_0001⟧ file")
    renamed = root / "new.txt"; doc.rename(renamed)     # same bytes → same hash
    monkeypatch.setattr(M, "_anonymise_file", lambda p: (_ for _ in ()).throw(AssertionError("models")))
    assert M._read_with_shadow(str(renamed)) == "⟦NOM_0001⟧ file"
