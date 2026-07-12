"""
test_task2_depollute_junk_gated.py — the junk lane is ALSO allowlist-gated.

The junk lane (lowercase + zipf>=4) auto-unmasks WITHOUT calling the
classifier. Task 2 requires the allowlist filter to be applied to the WHOLE
entry loop BEFORE triage, so a lowercase high-frequency value of a
NON-allowlisted type is NOT auto-unmasked either — it stays masked despite
passing the zipf test.

All PII in this file is SYNTHETIC. No real client values anywhere.
"""
from __future__ import annotations

from bubble_shield import known_pii_store as kps
from bubble_shield.depollute import depollute_gazetteer, triage


def _seed_typed(tmp_path, typed):
    p = tmp_path / "gaz.json"
    for value, etype in typed:
        kps.add_confirmed_pii(value, etype, path=p)
    return p


def test_junk_lane_non_allowlisted_type_stays_masked(tmp_path):
    # "conseiller" is a lowercase high-frequency common word → the triage
    # junk lane would auto-unmask it. But here it is registered as a
    # NON-allowlisted type (EMAIL). The allowlist gate must skip it entirely,
    # so it stays masked even though it passes the zipf junk test.
    assert triage("conseiller") == "junk"  # would auto-unmask if allowlisted

    gaz = _seed_typed(
        tmp_path,
        [
            ("conseiller", "EMAIL"),   # non-allowlisted → must stay masked
            ("fiscal", "NOM"),         # allowlisted junk → un-masks
        ],
    )
    q = tmp_path / "queue.json"

    def fail_if_called(tokens):
        raise AssertionError("classifier must not be called for pure junk lane")

    res = depollute_gazetteer(fail_if_called, gaz_path=gaz, queue_path=q)

    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "conseiller" in remaining          # non-allowlisted → stays masked
    assert "fiscal" not in remaining          # allowlisted junk → un-masked

    assert res["unmasked"] == ["fiscal"]
    assert "conseiller" not in res["unmasked"]
    assert "conseiller" not in res["kept"]     # skipped entirely, not "kept"
