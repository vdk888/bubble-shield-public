"""
test_568_conflict_flag.py — #568 Task 9: conflict loop.

When the engine RE-SEEDS a value that Gemma recently un-masked (i.e. the value
is currently sitting in the review queue as a pending depollute candidate), the
re-seed must NOT silently win — it should still happen (fail-toward-masking:
masking always wins), but it must ALSO flag a conflict entry for a human
tiebreaker, since the engine and Gemma disagree and Gemma is the more accurate
judge.

All values are synthetic. Every test passes explicit tmp paths (gaz_path /
queue_path) so the real ~/.bubble_shield/ store is never touched (on top of
the autouse BUBBLE_SHIELD_HOME isolation fixture in conftest.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from bubble_shield import known_pii_store as kps
from bubble_shield import review_queue as rq


def test_reseed_of_recently_unmasked_value_flags_conflict(tmp_path: Path) -> None:
    gaz = tmp_path / "gaz.json"
    q = tmp_path / "queue.json"

    # Simulate: the value was depollute-un-masked by Gemma — it sits in the
    # review queue as a pending candidate (the depollute log).
    rq.add_candidate("Compte", "NOM", "doc1.txt", path=q)

    # The engine re-seeds it into the gazetteer → must flag a conflict for a
    # human tiebreaker, not silently re-add it clean.
    kps.reseed_with_conflict_check("Compte", "NOM", gaz_path=gaz, queue_path=q)

    # The seed always happens (fail-toward-masking wins).
    assert kps.is_known_pii("Compte", path=gaz), (
        "reseed must ALWAYS add the value to the gazetteer (masking wins)"
    )

    # The conflict is additive: it must show up in the queue's audit trail.
    # add_candidate dedups by normalized token (existing #2 DEDUP rule), so the
    # conflict flag lands as a second doc_ref ("conflict:reseed") on the SAME
    # pending item, with occurrence_count bumped — not a brand-new duplicate
    # entry. Assert on that marker rather than raw string-count duplication.
    raw = json.loads(q.read_text())
    dumped = json.dumps(raw)
    assert "conflict" in dumped.lower(), "conflict marker must be logged in the queue"
    items = raw["items"]
    assert any(
        it.get("value") == "Compte" and "conflict:reseed" in it.get("doc_refs", [])
        for it in items
    ), "the conflict flag must be recorded against the Compte pending item"


def test_reseed_of_recently_unmasked_value_flags_conflict_on_default_paths(
    tmp_path: Path,
) -> None:
    """PRODUCTION-PATH regression (reviewer-found CRITICAL): both real callers
    (bubble_shield_mcp.py:654 -> seed_vault_into_gazetteer(engine.vault)) invoke
    reseed_with_conflict_check with DEFAULT gaz_path/queue_path (path=None), not
    tmp-isolated paths. The conftest autouse fixture points BUBBLE_SHIELD_HOME at
    a per-test tmp dir, so exercising the default-path resolution here still
    never touches the real ~/.bubble_shield/ store.

    Bug: add_candidate's gazetteer-skip check hardcoded `is_known_pii(value,
    path=None)`. reseed_with_conflict_check's step 1 (add_confirmed_pii) already
    wrote `value` into the (same, default) gazetteer before step 2 called
    add_candidate — so the skip-check saw "already known" and silently dropped
    the conflict entry. On tmp/mismatched paths (the other tests in this file)
    the skip-check reads a DIFFERENT (real, untouched) default gazetteer and
    the bug never surfaces — that's the test-isolation accident that let this
    ship. This test uses ONLY default paths end-to-end so it reproduces the
    actual production call shape.
    """
    # Simulate Gemma un-masking "Compte": it sits pending in the review queue
    # at the DEFAULT queue path (no path= override).
    rq.add_candidate("Compte", "NOM", "doc1.txt")

    # The engine re-seeds it into the gazetteer at the DEFAULT gaz path too —
    # exactly how seed_vault_into_gazetteer / bubble_shield_mcp.py call it in
    # production (no gaz_path/queue_path override).
    kps.reseed_with_conflict_check("Compte", "NOM")

    # The seed must ALWAYS succeed (fail-toward-masking), reading the same
    # default gazetteer path.
    assert kps.is_known_pii("Compte"), (
        "reseed must ALWAYS add the value to the gazetteer (masking wins), "
        "even on default paths"
    )

    # The conflict must ALSO be flagged — this is Task 9's entire deliverable,
    # and it must fire on the production (default-path) call shape, not just
    # under test-only tmp-path isolation.
    raw = json.loads((Path(kps._shield_home()) / "review_queue.json").read_text())
    items = raw["items"]
    assert any(
        it.get("value") == "Compte" and "conflict:reseed" in it.get("doc_refs", [])
        for it in items
    ), (
        "the conflict flag must be recorded against the Compte pending item "
        "even when reseed_with_conflict_check is called with default paths "
        "(the production call shape)"
    )


def test_reseed_without_prior_unmask_does_not_flag_conflict(tmp_path: Path) -> None:
    """A normal, first-time seed of a value that was NEVER un-masked by Gemma
    must NOT create a spurious conflict entry — only actual disagreements are
    flagged."""
    gaz = tmp_path / "gaz.json"
    q = tmp_path / "queue.json"

    kps.reseed_with_conflict_check("Dupont", "NOM", gaz_path=gaz, queue_path=q)

    assert kps.is_known_pii("Dupont", path=gaz)

    if q.exists():
        raw = json.loads(q.read_text())
        assert "Dupont" not in json.dumps(raw), (
            "no conflict should be logged when the value was never "
            "Gemma-un-masked"
        )


def test_reseed_never_blocks_the_seed_even_if_queue_is_broken(tmp_path: Path) -> None:
    """Fail-toward-masking: even if the review-queue side errors out, the
    gazetteer add must still happen. The conflict flag is purely additive."""
    gaz = tmp_path / "gaz.json"
    # Point queue_path at a directory (not a file) to force a queue read/write
    # failure inside reseed_with_conflict_check.
    bad_q = tmp_path / "not_a_file_dir"
    bad_q.mkdir()

    kps.reseed_with_conflict_check("Martin", "NOM", gaz_path=gaz, queue_path=bad_q)

    assert kps.is_known_pii("Martin", path=gaz), (
        "the seed must succeed even if the conflict-queue side fails"
    )


def test_seed_vault_into_gazetteer_flags_conflict_for_unmasked_value(
    tmp_path: Path,
) -> None:
    """seed_vault_into_gazetteer must route its per-value add through
    reseed_with_conflict_check, so a vault value that was recently
    Gemma-un-masked gets flagged too, not just the direct API."""
    from bubble_shield.vault import Vault

    gaz = tmp_path / "gaz.json"
    q = tmp_path / "queue.json"

    rq.add_candidate("Zorgwick Bramblesnap", "NOM", "doc1.txt", path=q)

    vault = Vault(mission="test-568-t9")
    vault.token_for("Zorgwick Bramblesnap", "NOM")

    added = kps.seed_vault_into_gazetteer(vault, path=gaz, queue_path=q)
    assert added == 1

    assert kps.is_known_pii("Zorgwick Bramblesnap", path=gaz)
    raw = json.loads(q.read_text())
    items = raw["items"]
    assert any(
        it.get("value") == "Zorgwick Bramblesnap"
        and "conflict:reseed" in it.get("doc_refs", [])
        for it in items
    ), (
        "seed_vault_into_gazetteer must flag a conflict via "
        "reseed_with_conflict_check for a value that was pending in the queue"
    )
