"""
test_context_boost.py — context-word confidence boosting (Presidio-inspired).

A detection sitting near a cue word ("né le", "client", "titulaire", "demeurant"
…) is more likely real PII → boost its score. This lifts genuine names/dates over
the fail-closed threshold while leaving isolated form-label guesses low — directly
trimming the "à vérifier" clutter. Synthetic data only.
"""
from bubble_shield.context_boost import boost_by_context, CONTEXT_CUES
from bubble_shield.recognizers import Match


def _m(etype, value, start, score):
    return Match(start=start, end=start + len(value), entity_type=etype,
                 value=value, score=score)


def test_cue_word_before_entity_boosts_score():
    text = "Client : Marie Dubois"
    m = _m("NOM", "Marie Dubois", text.index("Marie"), 0.50)
    out = boost_by_context(text, [m])
    assert out[0].score > 0.50          # boosted
    assert out[0].score <= 1.0


def test_no_cue_leaves_score_unchanged():
    text = "Senior manager Marie Dubois"   # "Senior manager" is not a PII cue
    m = _m("NOM", "Marie Dubois", text.index("Marie"), 0.50)
    out = boost_by_context(text, [m])
    assert out[0].score == 0.50


def test_boost_can_cross_threshold():
    # A real name at 0.50 near "demeurant" should reach >= 0.6 (the fail-closed
    # threshold) so it stops being flagged "à vérifier".
    text = "demeurant 10 rue des Lilas, Marie Dubois née"
    m = _m("NOM", "Marie Dubois", text.index("Marie"), 0.50)
    out = boost_by_context(text, [m])
    assert out[0].score >= 0.6


def test_boost_is_capped_at_one():
    text = "client titulaire Marie Dubois"
    m = _m("NOM", "Marie Dubois", text.index("Marie"), 0.95)
    out = boost_by_context(text, [m])
    assert out[0].score <= 1.0


def test_cue_after_entity_also_boosts():
    # The cue can follow the entity ("Marie Dubois, née le 04/05/1980").
    text = "Marie Dubois née le 04/05/1980"
    m = _m("NOM", "Marie Dubois", 0, 0.50)
    out = boost_by_context(text, [m])
    assert out[0].score > 0.50


def test_cues_are_lowercased_and_nonempty():
    assert CONTEXT_CUES and all(c == c.lower() for c in CONTEXT_CUES)


def test_distant_cue_does_not_boost():
    # A cue word far away (beyond the window) must NOT boost.
    text = "client " + "x " * 60 + "Marie Dubois"
    m = _m("NOM", "Marie Dubois", text.index("Marie"), 0.50)
    out = boost_by_context(text, [m], window=40)
    assert out[0].score == 0.50
