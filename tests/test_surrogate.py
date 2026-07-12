"""
test_surrogate.py — realistic surrogate mode (DontFeedTheAI idea), opt-in.

Instead of opaque ⟦NOM_0001⟧ tokens, swap in plausible FAKE values
("Marie Durand", a fake IBAN) so the LLM reasons on natural-looking text. Still
fully reversible via the vault. OFF by default (opaque tokens fail safer); this
tests the opt-in surrogate generator + that round-trip restoration is exact.
"""
from bubble_shield.surrogate import surrogate_for, SurrogateVault
from bubble_shield.vault import Vault


def test_name_surrogate_is_realistic_not_a_token():
    s = surrogate_for("Jean Dupont", "NOM", seed=1)
    assert "⟦" not in s                      # not an opaque token
    assert s != "Jean Dupont"   # not the real value
    assert any(c.isalpha() for c in s)       # looks like a name


def test_email_surrogate_is_email_shaped():
    s = surrogate_for("jean.dupont@exemple.fr", "EMAIL", seed=1)
    assert "@" in s and "." in s.split("@")[1]
    assert s != "jean.dupont@exemple.fr"


def test_iban_surrogate_is_iban_shaped():
    s = surrogate_for("FR7630006000011234567890189", "IBAN", seed=1)
    assert s.startswith("FR") and len(s) >= 20
    assert s != "FR7630006000011234567890189"


def test_same_value_same_surrogate_within_vault():
    v = SurrogateVault()
    a = v.token_for("Marie Dubois", "NOM")
    b = v.token_for("Marie Dubois", "NOM")
    assert a == b                            # stable per value


def test_surrogate_round_trips_exactly():
    v = SurrogateVault()
    real = "Marie Dubois"
    sur = v.token_for(real, "NOM")
    # restore() maps the surrogate back to the real value
    assert v.restore("Bonjour " + sur + ", ça va ?") == "Bonjour " + real + ", ça va ?"


def test_distinct_values_get_distinct_surrogates():
    v = SurrogateVault()
    a = v.token_for("Marie Dubois", "NOM")
    b = v.token_for("Paul Durand", "NOM")
    assert a != b


def test_surrogate_vault_is_a_vault_subclass():
    # It must drop into the engine wherever a Vault is expected.
    assert isinstance(SurrogateVault(), Vault)
