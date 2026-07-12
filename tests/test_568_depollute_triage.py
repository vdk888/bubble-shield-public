from bubble_shield.depollute import triage


def test_lowercase_common_word_is_junk():
    assert triage("conseiller") == "junk"
    assert triage("fiscal") == "junk"


def test_capitalized_common_word_is_uncertain():
    # Déclarant/Monsieur are capitalized → can't decide on freq alone → Gemma
    assert triage("Déclarant") == "uncertain"
    assert triage("Monsieur") == "uncertain"


def test_capitalized_common_surname_is_uncertain():
    # Petit/Martin are common AND capitalized → uncertain (Gemma decides)
    assert triage("Petit") == "uncertain"


def test_rare_surname_is_uncertain():
    # rare surnames span the whole frequency range — no safe "keep" lane.
    # Gemma adjudicates instead of auto-keeping.
    assert triage("Lenoir") == "uncertain"


def test_empty_is_uncertain_not_crash():
    assert triage("") == "uncertain"
