import json
from pathlib import Path

from bubble_shield import known_pii_store as kps
from bubble_shield.depollute import depollute_gazetteer


def _seed_gaz(tmp_path, values):
    p = tmp_path / "gaz.json"
    for v in values:
        kps.add_confirmed_pii(v, "NOM", path=p)
    return p


def test_lowercase_junk_and_gemma_mot_are_unmasked(tmp_path):
    gaz = _seed_gaz(tmp_path, ["conseiller", "Déclarant", "Lenoir", "Dupont"])
    q = tmp_path / "queue.json"

    # fake Gemma: only adjudicates the 'uncertain' bucket it's given.
    # Déclarant=MOT (label, un-mask), Lenoir/Dupont=NOM (real name, stays masked)
    def fake_classify(tokens):
        m = {"Déclarant": "MOT"}
        return [{"token": t, "verdict": m.get(t, "NOM")} for t in tokens]

    res = depollute_gazetteer(fake_classify, gaz_path=gaz, queue_path=q)

    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    # conseiller (lowercase junk) + Déclarant (Gemma MOT) removed;
    # Lenoir + Dupont (Gemma said NOM) stay masked.
    assert "conseiller" not in remaining
    assert "Déclarant" not in remaining
    assert "Lenoir" in remaining
    assert "Dupont" in remaining

    assert set(res["unmasked"]) == {"conseiller", "Déclarant"}
    assert set(res["kept"]) == {"Lenoir", "Dupont"}
    assert res["logged"] == 2


def test_every_unmask_is_logged_to_review_queue(tmp_path):
    gaz = _seed_gaz(tmp_path, ["conseiller"])
    q = tmp_path / "queue.json"

    depollute_gazetteer(lambda toks: [], gaz_path=gaz, queue_path=q)

    logged = json.loads(Path(q).read_text())
    assert any("conseiller" in json.dumps(logged) for _ in [0])  # present in the audit log


def test_classify_error_fails_toward_masking(tmp_path):
    # An uncertain entry must STAY masked if classify_fn raises — never un-mask
    # on error. Only the junk lane (no Gemma needed) is un-masked in this case.
    gaz = _seed_gaz(tmp_path, ["conseiller", "Lenoir"])
    q = tmp_path / "queue.json"

    def boom(tokens):
        raise RuntimeError("daemon unreachable")

    res = depollute_gazetteer(boom, gaz_path=gaz, queue_path=q)

    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "conseiller" not in remaining  # junk lane unaffected by classify error
    assert "Lenoir" in remaining          # uncertain lane stays masked on error

    assert res["unmasked"] == ["conseiller"]
    assert res["kept"] == ["Lenoir"]


def test_no_uncertain_entries_never_calls_classify_fn(tmp_path):
    # All-junk gazetteer: classify_fn must not even be invoked.
    gaz = _seed_gaz(tmp_path, ["conseiller", "fiscal"])
    q = tmp_path / "queue.json"

    def fail_if_called(tokens):
        raise AssertionError("classify_fn should not be called with no uncertain entries")

    res = depollute_gazetteer(fail_if_called, gaz_path=gaz, queue_path=q)
    assert set(res["unmasked"]) == {"conseiller", "fiscal"}
    assert res["kept"] == []


def test_unmask_present_in_default_gazetteer_still_logs_audit_entry(tmp_path):
    """Reviewer-found bug (T5, same root cause as T9): add_candidate has a
    hardcoded gazetteer-skip check (`is_known_pii(value, path=None)`) that
    always reads the DEFAULT-path gazetteer, regardless of which gaz_path the
    caller is actually operating on. depollute_gazetteer operates on a CUSTOM
    gaz_path — but if the un-masked value ALSO happens to exist in the
    DEFAULT store (BUBBLE_SHIELD_HOME/gazetteer.json, seeded here via
    add_confirmed_pii with no path=), add_candidate's hardcoded path=None
    check finds it there and silently returns None -> no audit-log entry is
    ever written, even though depollute_gazetteer's own gazetteer copy just
    had the value un-masked (removed).

    This MUST fail on 159b08d (audit log entry silently dropped, logged count
    wrong) and pass once add_candidate honors the caller's gaz_path.
    """
    # "conseiller" seeded into a CUSTOM gazetteer (what depollute operates on).
    gaz = _seed_gaz(tmp_path, ["conseiller"])
    # The SAME value also exists in the DEFAULT-path gazetteer (no path= ->
    # resolves under $BUBBLE_SHIELD_HOME, which the autouse fixture isolates
    # to a per-test tmp dir).
    kps.add_confirmed_pii("conseiller", "NOM")

    q = tmp_path / "queue.json"

    res = depollute_gazetteer(lambda toks: [], gaz_path=gaz, queue_path=q)

    # The value WAS un-masked from the custom gazetteer.
    remaining = {e.value for e in kps.load_gazetteer(path=gaz).entries}
    assert "conseiller" not in remaining
    assert res["unmasked"] == ["conseiller"]

    # The audit-log entry MUST still be written to the review queue, even
    # though "conseiller" also lives in the DEFAULT gazetteer.
    assert q.is_file(), "review queue file was never written — audit entry silently dropped"
    logged_raw = json.loads(q.read_text())
    assert any(
        item.get("value") == "conseiller" for item in logged_raw.get("items", [])
    ), "un-masked value missing from review queue audit log"

    # "logged" must reflect the ACTUAL number of logged entries, not just
    # len(unmasked) — they must agree here since the log succeeded.
    assert res["logged"] == 1
