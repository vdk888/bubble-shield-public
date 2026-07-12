"""
test_allowlist.py — the "this is NOT the client" precision filter.

The allowlist drops detections that match the advisory firm's own identity or
public third parties (regulators, fund houses) — generic per-cabinet, never
client-specific. All fixtures are SYNTHETIC (no real client data).
"""
import json

from bubble_shield.allowlist import (
    Allowlist,
    PUBLIC_THIRD_PARTIES,
    load_deployment_allowlist,
)
from bubble_shield.recognizers import Match


def _m(entity_type, value):
    return Match(start=0, end=len(value), entity_type=entity_type, value=value)


def test_firm_name_is_allowlisted():
    al = Allowlist(phrases=("acme conseil", "10 rue du test"))
    assert al.is_allowlisted("ACME Conseil")
    assert al.is_allowlisted("10 rue du Test, 75000 Paris")  # phrase ⊂ value


def test_non_allowlisted_passes_through():
    al = Allowlist(phrases=("acme conseil",))
    assert not al.is_allowlisted("Jean Dupont")


def test_email_domain_match():
    al = Allowlist(email_domains=("acme.com",))
    assert al.is_allowlisted("advisor@acme.com")
    assert al.is_allowlisted("x@sub.acme.com")        # subdomain
    assert not al.is_allowlisted("client@gmail.com")


def test_phone_match_is_format_agnostic():
    # The unspaced firm phone leaked before the digit-normalised fix. (synthetic)
    al = Allowlist(phones=("01 23 45 67 89",))
    assert al.is_allowlisted("0123456789")            # unspaced
    assert al.is_allowlisted("01 23 45 67 89")        # spaced
    assert al.is_allowlisted("+33 1 23 45 67 89")     # +33 form
    assert not al.is_allowlisted("06 11 22 33 44")    # a different number


def test_filter_keeps_only_non_allowlisted():
    al = Allowlist(phrases=("acme conseil",), email_domains=("acme.com",))
    matches = [
        _m("NOM", "ACME Conseil"),          # firm → dropped
        _m("EMAIL", "bob@acme.com"),        # firm domain → dropped
        _m("NOM", "Marie Client"),          # client → kept
        _m("EMAIL", "marie@gmail.com"),     # client → kept
    ]
    kept = al.filter(matches)
    vals = {m.value for m in kept}
    assert vals == {"Marie Client", "marie@gmail.com"}


def test_public_third_parties_drops_regulators_and_fund_houses():
    # PUBLIC_THIRD_PARTIES is in source (generic, never client-specific).
    al = PUBLIC_THIRD_PARTIES
    assert al.is_allowlisted("AMF")
    assert al.is_allowlisted("Autorité des marchés financiers")
    assert al.is_allowlisted("Corum")
    assert al.is_allowlisted("BNP Paribas")
    # the firm's OWN identity is NOT in source (it's loaded from the gitignored
    # deployment config), so a firm name is not allowlisted by this set alone.
    assert not al.is_allowlisted("Marie Dubois")


def test_empty_value_is_not_allowlisted():
    assert not PUBLIC_THIRD_PARTIES.is_allowlisted("")
    assert not PUBLIC_THIRD_PARTIES.is_allowlisted("   ")


def test_load_deployment_allowlist_merges_firm_config(tmp_path, monkeypatch):
    # A local deployment config adds the firm's own identity on top of the
    # public third parties. Firm data lives ONLY in this file, never in source.
    cfg = tmp_path / "deployment_allowlist.json"
    cfg.write_text(json.dumps({
        "phrases": ["acme patrimoine", "jean conseiller"],
        "email_domains": ["acme-patrimoine.fr"],
        "phones": ["01 23 45 67 89"],
    }), encoding="utf-8")
    monkeypatch.setenv("BUBBLE_SHIELD_DEPLOYMENT_ALLOWLIST", str(cfg))
    al = load_deployment_allowlist()
    # firm identity from the config
    assert al.is_allowlisted("ACME Patrimoine")
    assert al.is_allowlisted("advisor@acme-patrimoine.fr")
    assert al.is_allowlisted("01 23 45 67 89")
    # public third parties still present (merged)
    assert al.is_allowlisted("AMF")
    # a plausible client is still not allowlisted
    assert not al.is_allowlisted("Marie Dubois")


def test_load_deployment_allowlist_falls_back_without_config(tmp_path, monkeypatch):
    # No config → public third parties only, engine still works.
    monkeypatch.setenv("BUBBLE_SHIELD_DEPLOYMENT_ALLOWLIST", str(tmp_path / "nope.json"))
    al = load_deployment_allowlist()
    assert al.is_allowlisted("AMF")          # public set present
    assert not al.is_allowlisted("ACME Patrimoine")  # no firm config loaded
