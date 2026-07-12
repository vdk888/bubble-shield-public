"""Guard-rail tests — synthetic data ONLY (never real client identifiers)."""
from bubble_shield import pii_guard


# Standard checksum-valid test IBAN (public ECBS example, not a real account).
TEST_IBAN = "FR7630006000011234567890189"


def test_refuse_checksum_valid_iban_as_regex():
    res = pii_guard.check_input(TEST_IBAN, "regex")
    assert res["ok"] is False
    assert TEST_IBAN not in res["reason"]  # never echo the value


def test_refuse_real_email_as_regex():
    res = pii_guard.check_input("marc.durand@example.com", "regex")
    assert res["ok"] is False
    assert "example.com" not in res["reason"]


def test_allow_proper_regex_pattern():
    res = pii_guard.check_input(r"\b[A-Z]{2}-\d{5}\b", "regex")
    assert res["ok"] is True


def test_allow_simple_category_word_as_regex():
    res = pii_guard.check_input("dossier", "regex")
    assert res["ok"] is True


def test_refuse_proper_noun_gliner_label():
    res = pii_guard.check_input("Martin", "gliner_label")
    assert res["ok"] is False
    assert "Martin" not in res["reason"]


def test_allow_generic_phrase_gliner_label():
    res = pii_guard.check_input("employer name", "gliner_label")
    assert res["ok"] is True


def test_allow_french_generic_phrase_gliner_label():
    res = pii_guard.check_input("nom d'employeur", "gliner_label")
    assert res["ok"] is True


def test_refuse_keep_without_confirm():
    res = pii_guard.check_input("Bubble Invest SAS", "keep", confirm=False)
    assert res["ok"] is False


def test_allow_keep_with_confirm_nonpii_firm_name():
    res = pii_guard.check_input("Bubble Invest SAS", "keep", confirm=True)
    assert res["ok"] is True


def test_refuse_keep_with_confirm_but_checksum_iban():
    res = pii_guard.check_input(TEST_IBAN, "keep", confirm=True)
    assert res["ok"] is False
    assert TEST_IBAN not in res["reason"]


def test_refuse_gliner_label_that_is_nir():
    # French NIR (social security number) — step 1 must catch the digits.
    res = pii_guard.check_input("1 84 12 75 116 001 42", "gliner_label")
    assert res["ok"] is False


def test_refuse_allcaps_name_as_regex():
    # Two ALL-CAPS words, no metachars → looks like a literal name value.
    res = pii_guard.check_input("MARC DURAND", "regex")
    assert res["ok"] is False
    assert "DURAND" not in res["reason"]


def test_refuse_long_digit_run_as_regex():
    res = pii_guard.check_input("123456789012", "regex")
    assert res["ok"] is False


def test_empty_value_refused():
    assert pii_guard.check_input("", "regex")["ok"] is False
    assert pii_guard.check_input("   ", "gliner_label")["ok"] is False


def test_unknown_kind_refused():
    assert pii_guard.check_input("anything", "bogus")["ok"] is False


def test_reason_never_echoes_value_across_kinds():
    secret = "VERYSECRETVALUE12345"
    for kind in ("regex", "gliner_label", "keep"):
        res = pii_guard.check_input(secret, kind)
        if not res["ok"]:
            assert secret not in res["reason"]
