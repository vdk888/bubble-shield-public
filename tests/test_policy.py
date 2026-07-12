"""Tests for bubble_shield/policy.py — per-entity cloak/keep policy + engine filter."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bubble_shield import policy as P  # noqa: E402
from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


def test_defaults_cloak_identifying_keep_amounts():
    d = P.default_policy()
    assert d["NOM"] is True
    assert d["IBAN"] is True
    assert d["MONTANT"] is False   # amounts kept by default (CGP use-case)
    assert d["ISIN"] is False


def test_load_missing_returns_defaults(tmp_path):
    pol = P.load_policy(tmp_path / "nope.json")
    assert pol == P.default_policy()


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "policy.json"
    pol = P.default_policy()
    pol["MONTANT"] = True       # user decides to cloak amounts after all
    P.save_policy(pol, p)
    loaded = P.load_policy(p)
    assert loaded["MONTANT"] is True
    assert loaded["NOM"] is True


def test_load_ignores_unknown_keys_and_corrupt(tmp_path):
    p = tmp_path / "policy.json"
    # #392 floor: a stored NOM=false (identifying kept) is coerced to cloak on load,
    # so a hand-edited / polluted policy.json can never represent an
    # identifying-type-kept state. A non-identifying type (MONTANT) still honours
    # its stored value, so the keep toggle still works for it.
    p.write_text('{"NOM": false, "MONTANT": false, "BOGUS": true}', encoding="utf-8")
    pol = P.load_policy(p)
    assert pol["NOM"] is True        # #392 floor: identifying always cloaked
    assert pol["MONTANT"] is False   # non-identifying keep is honoured
    assert "BOGUS" not in pol
    # corrupt file → defaults (never silently disables cloaking)
    p.write_text("not json{{", encoding="utf-8")
    assert P.load_policy(p) == P.default_policy()


def test_save_only_writes_known_types(tmp_path):
    p = tmp_path / "policy.json"
    P.save_policy({"NOM": True, "JUNK": True}, p)
    import json
    written = json.loads(p.read_text())
    assert "JUNK" not in written
    assert set(written.keys()) == set(P.ENTITY_CATALOG.keys())


def test_policy_view_identifying_first():
    rows = P.policy_view(P.default_policy())
    assert rows[0]["identifying"] is True
    # MONTANT (non-identifying) appears after the identifying block
    types = [r["type"] for r in rows]
    assert types.index("NOM") < types.index("MONTANT")


# --- the behaviour that matters: filter actually keeps/cloaks in the engine ---

def test_keep_montant_leaves_amount_in_clear():
    pol = P.default_policy()             # MONTANT=keep, NOM=cloak
    eng = AnonymizationEngine(vault=Vault(mission="t"),
                              match_filter=P.make_match_filter(pol))
    res = eng.anonymize("Le client Jean Martin dispose de 250 000 € sur son PEA.")
    assert "250 000 €" in res.anonymized      # amount KEPT
    assert "Jean Martin" not in res.anonymized  # name CLOAKED
    assert "⟦NOM" in res.anonymized


def test_cloak_montant_when_user_flips_it():
    pol = P.default_policy()
    pol["MONTANT"] = True                # user opts to cloak amounts
    eng = AnonymizationEngine(vault=Vault(mission="t"),
                              match_filter=P.make_match_filter(pol))
    res = eng.anonymize("Le client dispose de 250 000 € sur son PEA.")
    assert "250 000 €" not in res.anonymized
    assert "⟦MONTANT" in res.anonymized


def test_unknown_type_fails_closed_cloaked():
    # a match with a type not in the policy must still be cloaked
    pol = {"NOM": True}                  # MONTANT absent from this partial policy
    f = P.make_match_filter(pol)

    class M:
        def __init__(self, t): self.entity_type = t
    kept = f([M("NOM"), M("MONTANT"), M("MYSTERY")])
    types = [m.entity_type for m in kept]
    assert "NOM" in types          # explicitly cloak
    assert "MYSTERY" in types      # unknown → cloak (fail-closed)
    assert "MONTANT" in types      # absent key → cloak (fail-closed)
