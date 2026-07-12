"""Task 9 — singleton sweep lock (no two sweeps at once).

MLX/Metal is NOT concurrency-safe: two sweeps running the model pipeline at the
same time crash the process. The lock makes an overlapping launchd fire a safe
no-op instead. A STALE lock (the holder PID is dead — e.g. a sweep was killed
mid-run) must be stolen, not honoured, or a crash would wedge the sweep forever.
"""

from bubble_shield import shadow_index


def test_second_acquire_blocked_while_held(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    assert shadow_index.acquire_lock() is True
    assert shadow_index.acquire_lock() is False    # already held
    shadow_index.release_lock()
    assert shadow_index.acquire_lock() is True      # released → re-acquirable
    shadow_index.release_lock()


def test_stale_lock_is_stolen(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    (tmp_path / "sweep.lock").write_text("999999")  # PID that isn't alive
    assert shadow_index.acquire_lock() is True       # stale → stolen
    shadow_index.release_lock()
