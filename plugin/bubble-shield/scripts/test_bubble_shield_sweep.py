"""Task 9 — sweep CLI entrypoint: refuse-plaintext guard, singleton-lock no-op,
and real-anonymize_fn wiring.

These prove the LOCK + GUARD logic without running Gemma/GLiNER: the refuse test
short-circuits before any model, the lock no-op returns before run_sweep, and
the wiring test drives run_sweep over a tiny root with the real _anonymise_file
STUBBED (we only assert the sweep injected _that specific callable_, not that
the model ran).
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vendor"))

import bubble_shield_sweep as SW
import bubble_shield_mcp as M
from bubble_shield import shadow_store, shadow_index

_CLI = HERE / "bubble_shield_sweep.py"


# ---- PART C: refuse-plaintext prod guard -----------------------------------

def test_cli_refuses_when_passphrase_unset_writes_nothing(tmp_path, monkeypatch):
    """The hard Task-4 guard: with BUBBLE_SHIELD_STORE_PASSPHRASE unset, the CLI
    must REFUSE (nonzero exit) and write NOTHING to the store — a plaintext
    shadow store of the whole document base's real names must never be created.
    Driven as a real subprocess to exercise the actual CLI, not just main()."""
    home = tmp_path / "home"
    home.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "client.txt").write_text("Jean Dupont, IBAN FR76...")

    env = dict(os.environ)
    env["BUBBLE_SHIELD_HOME"] = str(home)
    env.pop("BUBBLE_SHIELD_STORE_PASSPHRASE", None)  # UNSET → must refuse

    proc = subprocess.run(
        [sys.executable, str(_CLI), "--root", str(docs)],
        capture_output=True, text=True, env=env)

    assert proc.returncode != 0, "CLI must exit nonzero when passphrase is unset"
    assert "coffre chiffré n'est pas configuré" in proc.stderr
    # Nothing written: neither the plaintext working DB nor the encrypted store.
    assert not (home / "shield.db").exists(), "plaintext store must NOT be written"
    assert not (home / "shield.db.enc").exists()


def test_main_refuses_when_passphrase_empty(tmp_path, monkeypatch):
    """An EMPTY passphrase is as unsafe as an unset one (matches
    shadow_store._passphrase truthiness). main() returns 1, writes nothing."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "")  # empty → refuse
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.txt").write_text("x")
    rc = SW.main(["--root", str(docs)])
    assert rc == 1
    assert not (tmp_path / "shield.db").exists()
    assert not (tmp_path / "shield.db.enc").exists()


# ---- PART B: singleton-lock no-op ------------------------------------------

def test_cli_is_noop_when_lock_already_held(tmp_path, monkeypatch):
    """When another sweep holds the lock, this invocation is a safe no-op:
    exit 0, does NOT run the sweep (never touches _anonymise_file)."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pw")
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.txt").write_text("Jean Dupont")

    # Pre-hold the lock with THIS live PID so acquire_lock() returns False.
    (home / "sweep.lock").write_text(str(os.getpid()))

    # Sabotage the model path: a no-op run must never reach it.
    monkeypatch.setattr(
        M, "_anonymise_file",
        lambda p: (_ for _ in ()).throw(AssertionError("sweep ran despite held lock")))

    rc = SW.main(["--root", str(docs)])
    assert rc == 0  # safe no-op, not an error


# ---- PART B/D: real anonymize_fn wiring ------------------------------------

def test_sweep_injects_the_real_anonymise_file(tmp_path, monkeypatch):
    """Prove the CLI passes the plugin's REAL _anonymise_file as anonymize_fn.

    We STUB M._anonymise_file (so no Gemma/GLiNER runs) and record its calls.
    Because the CLI imports bubble_shield_mcp and passes
    bubble_shield_mcp._anonymise_file, patching that attribute is observed by
    the running sweep — a call on our tiny doc proves the wiring."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pw")
    docs = tmp_path / "docs"; docs.mkdir()
    doc = docs / "a.txt"; doc.write_text("Jean Dupont content")

    calls = []
    def _stub(path):
        calls.append(path)
        return "masked ⟦NOM_0001⟧"
    monkeypatch.setattr(M, "_anonymise_file", _stub)

    rc = SW.main(["--root", str(docs)])
    assert rc == 0
    assert len(calls) == 1, "the real _anonymise_file wiring should be invoked once"
    # And the resolved doc path was indexed into the shadow store.
    h = shadow_store.content_hash(doc)
    assert shadow_store.get_shadow(h) == "masked ⟦NOM_0001⟧"


def test_sweep_lock_released_after_run(tmp_path, monkeypatch):
    """After a successful sweep the lock is released so the next run acquires."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "test-pw")
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.txt").write_text("x")
    monkeypatch.setattr(M, "_anonymise_file", lambda p: "clean")
    assert SW.main(["--root", str(docs)]) == 0
    # Lock file gone → re-acquirable.
    assert shadow_index.acquire_lock() is True
    shadow_index.release_lock()
