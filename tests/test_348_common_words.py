# tests/test_348_common_words.py
def test_common_words_suppressed_real_names_kept():
    from bubble_shield.common_words import is_common_word
    assert is_common_word("marchés") is True
    assert is_common_word("Marchés") is True       # case-insensitive
    assert is_common_word("investissements") is True
    assert is_common_word("Dupont") is False        # a real name is NOT common
    assert is_common_word("dupont") is False        # conservative: lowercase name not auto-suppressed

def test_common_words_filter_drops_only_listed():
    from bubble_shield.common_words import filter_matches
    from bubble_shield.recognizers import Match
    ms = [Match(value="marchés", entity_type="NOM", start=0, end=7, score=0.4),
          Match(value="Dupont", entity_type="NOM", start=8, end=14, score=0.4)]
    out = [m.value for m in filter_matches(ms)]
    assert "marchés" not in out
    assert "Dupont" in out
