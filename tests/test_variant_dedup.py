"""
test_variant_dedup.py — name-variant de-duplication in the vault (PII-Shield idea).

"Jean Dupont", "M. Dupont", "Jean" all refer to ONE person, so
they should share the SAME person NUMBER in the token (NOM_0001) for a consistent,
readable anonymisation — while restoration still puts back each variant's EXACT
original surface form (we must NOT replace a lone "Jean" with the full name).
Synthetic data only.
"""
from bubble_shield.vault import Vault


def test_exact_repeat_same_token():
    v = Vault()
    t1 = v.token_for("Marie Dubois", "NOM")
    t2 = v.token_for("Marie Dubois", "NOM")
    assert t1 == t2                       # idempotent (unchanged behaviour)


def test_variant_shares_person_number():
    v = Vault()
    full = v.token_for("Marie Dubois", "NOM")        # NOM_0001
    # a later mention of just the surname is the SAME person
    surname = v.token_for("Dubois", "NOM")
    # same NUMBER (person 1), so the reader sees one consistent identity
    assert _num(full) == _num(surname)


def test_variant_restores_to_its_own_surface_form():
    v = Vault()
    full_tok = v.token_for("Marie Dubois", "NOM")
    sur_tok = v.token_for("Dubois", "NOM")
    # tokens may share the number but each restores to ITS OWN original
    assert v.value_for(full_tok) == "Marie Dubois"
    assert v.value_for(sur_tok) == "Dubois"


def test_unrelated_names_get_distinct_numbers():
    v = Vault()
    a = v.token_for("Marie Dubois", "NOM")
    b = v.token_for("Paul Durand", "NOM")
    assert _num(a) != _num(b)


def test_distinct_types_never_merge():
    v = Vault()
    name = v.token_for("Lyon", "NOM")
    place = v.token_for("Lyon", "LIEU_NAISSANCE")
    assert name != place                  # different type → different token


def _num(token: str) -> str:
    # ⟦NOM_0001⟧ -> "0001"
    import re
    m = re.search(r"_(\d+)", token)
    return m.group(1) if m else token
