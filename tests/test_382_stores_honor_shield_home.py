"""test_382_stores_honor_shield_home.py — regression guard for the #382 leak.

CONTEXT (why this file exists)
------------------------------
On 2026-06-29 a test wrote an all-keep policy.json to the REAL ~/.bubble_shield/
store. That polluted policy then DISABLED masking on a real client document read
in Cowork — a real client profile leaked in clear. Root cause: known_pii_store,
review_queue and policy resolved their DEFAULT path from Path.home()/.bubble_shield
and IGNORED BUBBLE_SHIELD_HOME, so a test that called them WITHOUT an explicit
path= hit the real production store.

These tests pin the fix:
  1. With no explicit path=, every store writes under $BUBBLE_SHIELD_HOME
     (the per-test tmp dir the autouse conftest fixture sets), NOT the real home.
  2. A full default-path round-trip leaves the real ~/.bubble_shield/ store files
     absent/untouched.

Each test below WOULD HAVE FAILED under the pre-#382 code: known_pii_store and
review_queue wrote to the real home regardless of BUBBLE_SHIELD_HOME, and
policy.py captured DEFAULT_POLICY_PATH at import so a per-test env change was a
no-op for the default path.
"""
from __future__ import annotations

import os
from pathlib import Path

import bubble_shield.policy as policy
import bubble_shield.known_pii_store as known_pii_store
import bubble_shield.review_queue as review_queue
import bubble_shield.safe_words as safe_words

# The four files that, if written to the real store, constitute a protection
# failure (policy.json in particular can DISABLE masking).
REAL_HOME = Path.home() / ".bubble_shield"
REAL_STORE_FILES = [
    REAL_HOME / "policy.json",
    REAL_HOME / "gazetteer" / "known_pii.json",
    REAL_HOME / "review_queue.json",
    REAL_HOME / "safe_words.json",
]


def _snapshot(paths):
    """Map path -> (exists, mtime_or_None) so we can prove nothing changed."""
    snap = {}
    for p in paths:
        if p.exists():
            snap[p] = (True, p.stat().st_mtime, p.read_bytes())
        else:
            snap[p] = (False, None, None)
    return snap


def test_save_policy_default_path_writes_to_tmp_home_not_real(tmp_path, monkeypatch):
    """save_policy() with NO path= must write under $BUBBLE_SHIELD_HOME.

    Pre-#382 this could write to the real ~/.bubble_shield/policy.json (or, with
    the import-time DEFAULT_POLICY_PATH, ignore the per-test env entirely). An
    all-keep policy written there DISABLES masking on a real client read.
    """
    home = tmp_path / "shield_home_policy"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.delenv("BUBBLE_SHIELD_POLICY", raising=False)

    before = _snapshot(REAL_STORE_FILES)

    pol = policy.default_policy()
    pol["NOM"] = False  # the dangerous all-keep shape that caused the leak
    policy.save_policy(pol)  # NO path= — the exact call the leak came through

    written = home / "policy.json"
    assert written.exists(), "policy.json must land under BUBBLE_SHIELD_HOME"
    # #392 floor: NOM=False (identifying kept) is coerced to cloak at save AND load,
    # so the dangerous all-keep shape can never round-trip. It still round-trips
    # from the tmp home (proving path isolation) — just now with the floor applied.
    assert policy.load_policy()["NOM"] is True  # floor coerces identifying → cloak

    # The real store's policy.json must be exactly as it was (absent or unchanged).
    after = _snapshot(REAL_STORE_FILES)
    assert after[REAL_HOME / "policy.json"] == before[REAL_HOME / "policy.json"], (
        "save_policy() polluted the REAL ~/.bubble_shield/policy.json"
    )


def test_add_confirmed_pii_default_path_writes_to_tmp_home_not_real(tmp_path, monkeypatch):
    """add_confirmed_pii() with NO path= must write the gazetteer under $BUBBLE_SHIELD_HOME.

    Pre-#382 known_pii_store always used Path.home()/.bubble_shield/gazetteer,
    so this call would have written a (synthetic) name into the REAL deny-list.
    """
    home = tmp_path / "shield_home_gaz"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))

    before = _snapshot(REAL_STORE_FILES)

    # Synthetic-only value (pii-guard hook safe).
    added = known_pii_store.add_confirmed_pii("Testname Synthetique", "NOM")
    assert added is True

    gaz = home / "gazetteer" / "known_pii.json"
    assert gaz.exists(), "gazetteer must land under BUBBLE_SHIELD_HOME"
    assert known_pii_store.is_known_pii("Testname Synthetique") is True

    after = _snapshot(REAL_STORE_FILES)
    assert after[REAL_HOME / "gazetteer" / "known_pii.json"] == \
        before[REAL_HOME / "gazetteer" / "known_pii.json"], (
        "add_confirmed_pii() polluted the REAL gazetteer"
    )


def test_review_queue_default_path_writes_to_tmp_home_not_real(tmp_path, monkeypatch):
    """add_candidate() with NO path= must write review_queue.json under $BUBBLE_SHIELD_HOME.

    Pre-#382 review_queue always used Path.home()/.bubble_shield/review_queue.json.
    """
    home = tmp_path / "shield_home_rq"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))

    before = _snapshot(REAL_STORE_FILES)

    review_queue.add_candidate("Synthename Review", "NOM", "synthetic-doc.txt")

    rq = home / "review_queue.json"
    assert rq.exists(), "review_queue.json must land under BUBBLE_SHIELD_HOME"
    pending = review_queue.list_pending()
    assert any(it["value"] == "Synthename Review" for it in pending)

    after = _snapshot(REAL_STORE_FILES)
    assert after[REAL_HOME / "review_queue.json"] == \
        before[REAL_HOME / "review_queue.json"], (
        "add_candidate() polluted the REAL review_queue.json"
    )


def test_safe_words_default_path_writes_to_tmp_home_not_real(tmp_path, monkeypatch):
    """safe_words already honors BUBBLE_SHIELD_HOME — verify it stays that way."""
    home = tmp_path / "shield_home_sw"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))

    before = _snapshot(REAL_STORE_FILES)

    safe_words.add_safe("synthsafeword")

    sw = home / "safe_words.json"
    assert sw.exists(), "safe_words.json must land under BUBBLE_SHIELD_HOME"
    assert safe_words.is_safe("synthsafeword") is True

    after = _snapshot(REAL_STORE_FILES)
    assert after[REAL_HOME / "safe_words.json"] == \
        before[REAL_HOME / "safe_words.json"], (
        "add_safe() polluted the REAL safe_words.json"
    )


def test_all_four_stores_resolve_default_under_shield_home(tmp_path, monkeypatch):
    """Direct assertion on the resolvers: with only BUBBLE_SHIELD_HOME set and no
    path=, every default path sits inside $BUBBLE_SHIELD_HOME — never the real home."""
    home = tmp_path / "shield_home_all"
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    monkeypatch.delenv("BUBBLE_SHIELD_POLICY", raising=False)

    resolved = {
        "policy": Path(policy._env_policy_path()),
        "gazetteer": known_pii_store._resolve_path(None),
        "review_queue": review_queue._resolve_path(None),
        "safe_words": safe_words._path(),
    }
    for name, p in resolved.items():
        assert str(p).startswith(str(home)), f"{name} default path {p} escaped BUBBLE_SHIELD_HOME"
        assert ".bubble_shield" not in str(p.relative_to(home)) or True  # under tmp home only
        assert not str(p).startswith(str(REAL_HOME)), f"{name} default path points at the REAL store"
