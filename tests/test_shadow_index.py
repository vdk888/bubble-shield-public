from pathlib import Path

from bubble_shield import shadow_index, shadow_store

def test_index_one_stores_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    f = tmp_path / "d.txt"; f.write_text("Jean Dupont content")
    fake = lambda p: "masked ⟦NOM_0001⟧ content"
    h = shadow_index.index_one(str(f), anonymize_fn=fake)
    assert shadow_store.get_shadow(h) == "masked ⟦NOM_0001⟧ content"
    assert h == shadow_store.content_hash(f)

def test_index_one_clears_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    f = tmp_path / "d.txt"; f.write_text("x")
    shadow_store.mark_pending(str(f))
    shadow_index.index_one(str(f), anonymize_fn=lambda p: "clean")
    assert str(f) not in shadow_store.pending_files()

def test_run_sweep_indexes_new_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    root = tmp_path / "protected"; root.mkdir()
    (root / "a.txt").write_text("alpha"); (root / "b.txt").write_text("beta")
    calls = []
    fake = lambda p: (calls.append(p) or f"clean:{Path(p).name}")
    r1 = shadow_index.run_sweep(str(root), anonymize_fn=fake)
    assert r1["indexed"] == 2
    r2 = shadow_index.run_sweep(str(root), anonymize_fn=fake)   # resumable
    assert r2["indexed"] == 0 and r2["skipped"] == 2           # nothing reprocessed
    assert len(calls) == 2                                     # model fn called twice total


def test_run_sweep_survives_dataless_file(tmp_path, monkeypatch):
    """Task 13b — one dataless/unreadable file (Dropbox online-only placeholder)
    must NOT abort the whole sweep. It is DEFERRED (marked pending for a later
    sweep) while every other file still indexes.

    A real online-only placeholder raises OSError the moment its bytes are read.
    We simulate that precisely: content_hash reads the bytes, so we make it raise
    OSError for exactly ONE file (the "dataless" one) and let the rest read
    normally. _try_materialize is forced to keep failing so the file stays
    dataless (models/Dropbox aren't going to hydrate it in the test env).
    """
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    root = tmp_path / "protected"; root.mkdir()
    good1 = root / "a.txt"; good1.write_text("alpha")
    dead = root / "dataless.txt"; dead.write_text("placeholder-metadata-only")
    good2 = root / "b.txt"; good2.write_text("beta")

    real_hash = shadow_store.content_hash
    def hash_or_dataless(p):
        if Path(p).name == "dataless.txt":
            raise OSError(11, "Resource deadlock avoided")   # the Dropbox errno
        return real_hash(p)
    monkeypatch.setattr(shadow_store, "content_hash", hash_or_dataless)
    # Materialization keeps failing for the dataless file only; the real files
    # hydrate fine (they're on a normal disk).
    monkeypatch.setattr(
        shadow_index, "_try_materialize",
        lambda p: Path(p).name != "dataless.txt")

    calls = []
    fake = lambda p: (calls.append(p) or f"clean:{Path(p).name}")
    r = shadow_index.run_sweep(str(root), anonymize_fn=fake)

    # The sweep completed past the unreadable file: both good files indexed,
    # the dataless one deferred (not fatal, not indexed).
    assert r["indexed"] == 2
    assert r["deferred"] == 1
    assert str(dead.resolve()) in shadow_store.pending_files()   # queued for retry
    # The anonymize_fn ran only for the two readable files, never the dataless one.
    assert len(calls) == 2
    assert not any("dataless.txt" in c for c in calls)


def test_run_sweep_materializes_then_indexes(tmp_path, monkeypatch):
    """Task 13b — if a file was dataless on first probe but materializes on retry,
    the sweep indexes it in the same run (materialize-then-index path)."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    root = tmp_path / "protected"; root.mkdir()
    f = root / "late.txt"; f.write_text("hydrates on retry")

    real_hash = shadow_store.content_hash
    state = {"first": True}
    def hash_late(p):
        if Path(p).name == "late.txt" and state["first"]:
            state["first"] = False
            raise OSError(11, "Resource deadlock avoided")
        return real_hash(p)
    monkeypatch.setattr(shadow_store, "content_hash", hash_late)
    # Materialization succeeds → sweep retries content_hash and indexes.
    monkeypatch.setattr(shadow_index, "_try_materialize", lambda p: True)

    r = shadow_index.run_sweep(str(f.parent), anonymize_fn=lambda p: "clean")
    assert r["indexed"] == 1 and r["deferred"] == 0


def test_run_sweep_survives_uncertifiable_file(tmp_path, monkeypatch):
    """Task 13b — a READABLE file whose anonymisation can't be certified (models
    down / scanned image / unreachable second pass → a non-OSError exception)
    must FAIL CLOSED per-file (no shadow stored, marked pending) WITHOUT aborting
    the sweep. Proven with an anonymize_fn that raises for one file."""
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    root = tmp_path / "protected"; root.mkdir()
    (root / "a.txt").write_text("alpha")
    bad = root / "uncertifiable.txt"; bad.write_text("needs a model that is down")
    (root / "b.txt").write_text("beta")

    class _NERDown(RuntimeError):
        pass
    def anon(path):
        if Path(path).name == "uncertifiable.txt":
            raise _NERDown("NER daemon offline — cannot certify")
        return "clean-shadow"

    r = shadow_index.run_sweep(str(root), anonymize_fn=anon)
    # Sweep completed: the two good files indexed, the un-certifiable one FAILED
    # (fail-closed: no shadow), and the walk never aborted.
    assert r["indexed"] == 2
    assert r["failed"] == 1
    # No shadow was stored for the failed file (read would fall through to the
    # read-time fail-closed path), and it is queued for retry.
    assert shadow_store.get_shadow(shadow_store.content_hash(bad)) is None
    assert str(bad.resolve()) in shadow_store.pending_files()
