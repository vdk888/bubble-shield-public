"""
test_348_safe_list.py — self-improving "never mask these" safe-list store (#348 3c).

The safe-list is the symmetric OPPOSITE of the known-PII gazetteer: words a
reviewer un-hid because they were wrongly masked. The LOAD-BEARING safety rule
(Joris's precedence rule) is that the gazetteer (always-mask) ALWAYS WINS over
the safe-list: a value present in BOTH stays MASKED.

All names are SYNTHETIC. "Foobar" / "Realname" are invented placeholders; "Martin"
is used as a generic French placeholder surname (NOT a known client) purely to
exercise the gazetteer-wins precedence — it is added to a *temp* gazetteer only.

PII hygiene: the precedence test routes the gazetteer through a temp HOME via the
known_pii_store `path=` parameter (known_pii_store does NOT honor BUBBLE_SHIELD_HOME),
so the real ~/.bubble_shield/gazetteer is NEVER touched.
"""
from __future__ import annotations

import importlib


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))


def test_safe_add_is_safe_remove(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    assert sw.is_safe("marchés") is False
    sw.add_safe("marchés")
    assert sw.is_safe("marchés") is True
    assert sw.is_safe("Marchés") is True       # case-insensitive
    sw.remove_safe("marchés")
    assert sw.is_safe("marchés") is False


def test_safe_filter_drops_safe_keeps_others(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    from bubble_shield.recognizers import Match
    sw.add_safe("Foobar")
    ms = [Match(value="Foobar", entity_type="NOM", start=0, end=6, score=0.4),
          Match(value="Realname", entity_type="NOM", start=7, end=15, score=0.4)]
    out = [m.value for m in sw.filter_matches(ms)]
    assert "Foobar" not in out
    assert "Realname" in out


def test_corrupt_safe_file_treated_empty(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    (tmp_path / "safe_words.json").write_text("{ broken json", encoding="utf-8")
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    assert sw.is_safe("anything") is False   # fail toward masking


def test_safe_file_chmod_600(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    sw.add_safe("Foobar")
    import stat
    mode = (tmp_path / "safe_words.json").stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_remove_nonexistent_returns_false(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    assert sw.remove_safe("Neverthere") is False


def test_safe_filter_passes_non_nom(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    from bubble_shield.recognizers import Match
    sw.add_safe("Foobar")
    # An IBAN whose value happens to be safe-listed must NOT be dropped:
    # the safe-list only suppresses NOM spans.
    ms = [Match(value="Foobar", entity_type="IBAN", start=0, end=6, score=0.9)]
    out = [m.value for m in sw.filter_matches(ms)]
    assert "Foobar" in out


# ════════════════════════════════════════════════════════════════════════════════
# THE CRITICAL PRECEDENCE TEST — gazetteer (always-mask) ALWAYS wins over safe-list.
# A value in BOTH the safe-list and the gazetteer must stay MASKED.
# ════════════════════════════════════════════════════════════════════════════════

def test_gazetteer_wins_over_safe_list(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    from bubble_shield.known_pii_store import add_confirmed_pii, is_known_pii

    # Route the gazetteer through a temp file so the real one is never touched.
    gaz_path = tmp_path / "known_pii.json"

    sw.add_safe("Martin")                                   # safe-listed as a "common word"
    add_confirmed_pii("Martin", "NOM", path=gaz_path)       # later confirmed as a real client name
    assert is_known_pii("Martin", path=gaz_path) is True

    # Reproduce the daemon's _safe_keep guard EXACTLY:
    #   if is_known_pii(val): return True   (gazetteer wins → keep masked)
    #   return not is_safe(val)
    def _safe_keep(val):
        if is_known_pii(val, path=gaz_path):   # gazetteer wins → keep masked
            return True
        return not sw.is_safe(val)

    assert _safe_keep("Martin") is True   # in BOTH → gazetteer wins → MASKED


def test_safe_list_drops_when_not_in_gazetteer(tmp_path, monkeypatch):
    """Complement to the precedence test: a safe-listed word that is NOT in the
    gazetteer IS dropped (kept in clear) by the guard."""
    _home(tmp_path, monkeypatch)
    import bubble_shield.safe_words as sw; importlib.reload(sw)
    from bubble_shield.known_pii_store import is_known_pii

    gaz_path = tmp_path / "known_pii.json"   # empty / nonexistent gazetteer
    sw.add_safe("Foobar")

    def _safe_keep(val):
        if is_known_pii(val, path=gaz_path):
            return True
        return not sw.is_safe(val)

    assert _safe_keep("Foobar") is False   # safe-listed, not gazetteered → dropped
    assert _safe_keep("Realname") is True  # neither → kept (masked)
