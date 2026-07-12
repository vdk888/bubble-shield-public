"""test_policy_loop_e2e.py — end-to-end policy write→read→engine-behaviour loop.

This is the test that should have existed from day one.  It proves that
save_policy() + load_policy() + AnonymizationEngine share ONE source of truth,
and that flipping a toggle in the webapp POST /dashboard/policy route changes
what the live plugin engine actually does — no monkeypatching of internals,
only BUBBLE_SHIELD_POLICY pointed at a tmp file so the test is hermetic.

Covers:
  1. parity: root DEFAULT_POLICY_PATH == vendored DEFAULT_POLICY_PATH
  2. write KEEP via save_policy() → engine leaves email in clear
  3. write CLOAK via save_policy() → engine cloaks email
  4. same loop driven through the webapp POST /dashboard/policy route
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_BUBBLE_SHIELD = REPO_ROOT / "plugin" / "bubble-shield" / "vendor"

TEST_TEXT = "Veuillez contacter marc@example.com pour toute question."
TEST_EMAIL = "marc@example.com"

# #392: EMAIL is an IDENTIFYING type and can NEVER be kept-in-clear (the floor).
# The toggle-honoured case must use a NON-identifying type. MONTANT (euro amounts)
# is the canonical keep-in-clear type for the CGP allocation/risk use-case.
TEST_AMOUNT_TEXT = "Le portefeuille est valorisé à 250 000 EUR."
TEST_AMOUNT = "250 000 EUR"


def _import_vendor_policy():
    """Import the vendored copy of policy.py as a separate module object.

    We use importlib so we can get an independent module reference (not the
    already-imported ``bubble_shield.policy``), which lets us compare their
    DEFAULT_POLICY_PATH values without interference.
    """
    vendor_path = str(VENDOR_BUBBLE_SHIELD)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    # Force a fresh import under a unique name so it doesn't collide with the
    # root package already on sys.path.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_vendor_bubble_shield_policy",
        VENDOR_BUBBLE_SHIELD / "bubble_shield" / "policy.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── test 1: root DEFAULT_POLICY_PATH == vendored DEFAULT_POLICY_PATH ─────────

def test_root_and_vendor_default_policy_path_are_identical():
    """The root package and the vendored copy must resolve to the SAME path.

    This is the exact parity that was broken before the fix: the old code
    derived the path relative to __file__, so the vendored copy pointed at
    a non-existent ``plugin/bubble-shield/vendor/webapp/data/policy.json``
    while the webapp wrote to ``webapp/data/policy.json``.

    After the fix both resolve to ``~/.bubble_shield/policy.json``
    (or whatever BUBBLE_SHIELD_HOME / BUBBLE_SHIELD_POLICY are set to).
    """
    import bubble_shield.policy as root_policy

    vendor_policy = _import_vendor_policy()

    # Compare the LIVE resolver, not the frozen import-time attribute: the path
    # is now resolved at call time from the env (#382), and the two modules are
    # imported at different moments (so their import-time DEFAULT_POLICY_PATH
    # snapshots can differ if BUBBLE_SHIELD_HOME moved between imports). Under the
    # SAME env they must resolve to the SAME path — that is the real parity.
    assert root_policy._env_policy_path() == vendor_policy._env_policy_path(), (
        f"Root path={root_policy._env_policy_path()!r} "
        f"!= vendored path={vendor_policy._env_policy_path()!r}"
    )


# ── test 2 & 3: save_policy() → load_policy() → engine behaviour ─────────────

def test_keep_montant_leaves_it_in_clear(tmp_path, monkeypatch):
    """Write MONTANT=KEEP via save_policy(), build an engine, assert amount stays clear.

    #392: the toggle-honoured path uses a NON-identifying type (MONTANT). Trying to
    KEEP an identifying type like EMAIL no longer works — see
    test_email_keep_is_floored_to_cloak below.
    """
    import bubble_shield.policy as P
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    # Re-evaluate DEFAULT_POLICY_PATH now that the env var is set.
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    # Write a policy that KEEPs MONTANT (cloak=False) — the default, but be explicit.
    pol = P.default_policy()
    pol["MONTANT"] = False  # KEEP
    P.save_policy(pol)

    # Build an engine the same way the live posttool does.
    loaded = P.load_policy()
    assert loaded["MONTANT"] is False, "MONTANT should be KEEP after save_policy"

    engine = AnonymizationEngine(
        vault=Vault(mission="e2e_test"),
        match_filter=P.make_match_filter(loaded),
    )
    result = engine.anonymize(TEST_AMOUNT_TEXT)

    assert TEST_AMOUNT in result.anonymized, (
        f"MONTANT=KEEP: amount should be in clear, got: {result.anonymized!r}"
    )
    assert "⟦MONTANT" not in result.anonymized


def test_email_keep_is_floored_to_cloak(tmp_path, monkeypatch):
    """#392 floor: trying to KEEP EMAIL (identifying) is coerced to CLOAK at save,
    load, and runtime — the email is masked no matter what the policy file says."""
    import bubble_shield.policy as P
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    pol = P.default_policy()
    pol["EMAIL"] = False  # try to KEEP an identifying type
    P.save_policy(pol)

    loaded = P.load_policy()
    assert loaded["EMAIL"] is True, "#392: EMAIL floored to cloak at load"

    engine = AnonymizationEngine(
        vault=Vault(mission="e2e_floor"),
        match_filter=P.make_match_filter(loaded),
    )
    result = engine.anonymize(TEST_TEXT)

    assert TEST_EMAIL not in result.anonymized, (
        f"#392 floor: EMAIL must be masked even with EMAIL=keep, got: {result.anonymized!r}"
    )
    assert "⟦EMAIL" in result.anonymized


def test_cloak_email_removes_it(tmp_path, monkeypatch):
    """Write EMAIL=CLOAK via save_policy(), build an engine, assert email is cloaked."""
    import bubble_shield.policy as P
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    pol = P.default_policy()
    pol["EMAIL"] = True  # CLOAK (also the default, but be explicit)
    P.save_policy(pol)

    loaded = P.load_policy()
    assert loaded["EMAIL"] is True

    engine = AnonymizationEngine(
        vault=Vault(mission="e2e_test"),
        match_filter=P.make_match_filter(loaded),
    )
    result = engine.anonymize(TEST_TEXT)

    assert TEST_EMAIL not in result.anonymized, (
        f"EMAIL=CLOAK: email must be replaced, got: {result.anonymized!r}"
    )
    assert "⟦EMAIL" in result.anonymized


def test_full_round_trip_keep_then_cloak(tmp_path, monkeypatch):
    """Flip MONTANT KEEP→CLOAK, assert engine behaviour changes between calls.

    #392: round-trips a NON-identifying type (MONTANT). Identifying types can't be
    kept (the floor), so they have no KEEP→CLOAK flip to exercise.
    """
    import bubble_shield.policy as P
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    def build_engine():
        pol = P.load_policy()
        return AnonymizationEngine(
            vault=Vault(mission="e2e_round_trip"),
            match_filter=P.make_match_filter(pol),
        )

    # --- Phase 1: KEEP ---
    pol = P.default_policy()
    pol["MONTANT"] = False
    P.save_policy(pol)

    result_keep = build_engine().anonymize(TEST_AMOUNT_TEXT)
    assert TEST_AMOUNT in result_keep.anonymized, "Phase 1 (KEEP): amount must be clear"

    # --- Phase 2: CLOAK ---
    pol["MONTANT"] = True
    P.save_policy(pol)

    result_cloak = build_engine().anonymize(TEST_AMOUNT_TEXT)
    assert TEST_AMOUNT not in result_cloak.anonymized, "Phase 2 (CLOAK): amount must be gone"
    assert "⟦MONTANT" in result_cloak.anonymized


# ── test 4: webapp POST /dashboard/policy route drives the same loop ──────────

def test_webapp_policy_route_keep_montant_then_engine_read(tmp_path, monkeypatch):
    """POST /dashboard/policy → MONTANT KEEP → engine sees amount in clear.

    #392: the toggle-honoured path uses a NON-identifying type. Uses FastAPI
    TestClient exactly like test_webapp.py does, patching DEFAULT_POLICY_PATH via
    monkeypatch so the test is hermetic.
    """
    import bubble_shield.policy as P
    from fastapi.testclient import TestClient
    from webapp.app import app

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    client = TestClient(app)

    # Build form data: all entity types cloaked EXCEPT MONTANT (omit cloak_MONTANT).
    form_data = {f"cloak_{etype}": "on" for etype in P.ENTITY_CATALOG if etype != "MONTANT"}
    # MONTANT is absent → the route interprets that as KEEP (False).

    resp = client.post("/dashboard/policy", data=form_data)
    assert resp.status_code == 200

    # The policy file must now exist and say MONTANT=False (KEEP).
    written = json.loads(policy_file.read_text(encoding="utf-8"))
    assert written.get("MONTANT") is False, (
        f"webapp should have saved MONTANT=False (KEEP), got {written.get('MONTANT')!r}"
    )

    # Build the engine the same way posttool does: load_policy() with no explicit path.
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    loaded = P.load_policy()
    assert loaded["MONTANT"] is False, "load_policy() must read the webapp-written file"

    engine = AnonymizationEngine(
        vault=Vault(mission="webapp_e2e"),
        match_filter=P.make_match_filter(loaded),
    )
    result = engine.anonymize(TEST_AMOUNT_TEXT)

    assert TEST_AMOUNT in result.anonymized, (
        f"After webapp KEEP toggle, amount must be in clear; got: {result.anonymized!r}"
    )


def test_webapp_policy_route_cannot_keep_identifying(tmp_path, monkeypatch):
    """#392 floor: POST /dashboard/policy with EMAIL unchecked must NOT save
    EMAIL=keep — the route forces identifying types to cloak, and the engine masks
    the email even though the form omitted its checkbox."""
    import bubble_shield.policy as P
    from fastapi.testclient import TestClient
    from webapp.app import app

    policy_file = tmp_path / "policy.json"
    monkeypatch.setenv("BUBBLE_SHIELD_POLICY", str(policy_file))
    monkeypatch.setattr(P, "DEFAULT_POLICY_PATH", str(policy_file))

    client = TestClient(app)

    # Omit cloak_EMAIL (the un-mask attempt). The route must still save EMAIL=cloak.
    form_data = {f"cloak_{etype}": "on" for etype in P.ENTITY_CATALOG if etype != "EMAIL"}
    resp = client.post("/dashboard/policy", data=form_data)
    assert resp.status_code == 200

    written = json.loads(policy_file.read_text(encoding="utf-8"))
    assert written.get("EMAIL") is True, (
        f"#392 floor: webapp must force EMAIL=cloak, got {written.get('EMAIL')!r}"
    )

    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    loaded = P.load_policy()
    assert loaded["EMAIL"] is True
    engine = AnonymizationEngine(
        vault=Vault(mission="webapp_floor"),
        match_filter=P.make_match_filter(loaded),
    )
    result = engine.anonymize(TEST_TEXT)
    assert TEST_EMAIL not in result.anonymized, (
        f"#392 floor: email must be masked despite unchecked box; got: {result.anonymized!r}"
    )
    assert "⟦EMAIL" in result.anonymized
