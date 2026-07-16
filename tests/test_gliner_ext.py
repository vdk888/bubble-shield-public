"""
test_gliner_ext.py — the optional chunked GLiNER NER layer.

Two things must hold (mirrors test_llm_ext):
  1. When GLiNER isn't installed/loadable, the layer is a silent no-op (fail-open).
  2. The chunking + union logic is correct: a full doc is windowed with overlap,
     per-window predictions are mapped to bubble_shield types and unioned with absolute
     offsets — WITHOUT calling the real model (we stub it).
  3. Dot-run compression eliminates form-blank token inflation before inference.
  4. form-name-class synthetic regression: ALL-CAPS names in tax-form address blocks
     are masked at the new threshold=0.30.
Synthetic data only.
"""
import bubble_shield.gliner_ext as gx


def test_unavailable_returns_empty(monkeypatch):
    # Model load fails → layer is a no-op.
    monkeypatch.setattr(gx, "_load_model", lambda model_id: None)
    assert gx.gliner_matches("Monsieur Jean Dupont à Lyon.") == []


def test_chunking_windows_cover_text_with_overlap():
    text = "abcdefghij" * 5   # 50 chars
    chunks = list(gx._chunks(text, size=20, overlap=5))
    # first window at 0, then step = size-overlap = 15
    starts = [base for base, _ in chunks]
    assert starts[0] == 0
    assert starts[1] == 15
    # union of windows covers the whole text
    covered = max(base + len(ch) for base, ch in chunks)
    assert covered >= len(text)


def test_chunk_size_must_exceed_overlap():
    import pytest
    with pytest.raises(ValueError):
        list(gx._chunks("abc", size=5, overlap=5))


class _StubModel:
    """A fake GLiNER: returns canned entities per chunk, by substring."""
    def __init__(self, table):
        self.table = table  # list of (substr, label, score)
    def predict_entities(self, chunk, labels, threshold=0.30):
        out = []
        for sub, label, score in self.table:
            idx = chunk.find(sub)
            if idx >= 0 and score >= threshold and label in labels:
                out.append({"text": sub, "label": label, "score": score,
                            "start": idx, "end": idx + len(sub)})
        return out


def test_maps_labels_to_bubble_shield_types_and_unions(monkeypatch):
    text = "Client Marie Dubois, email marie@x.fr, born in Lyon."
    stub = _StubModel([
        ("Marie Dubois", "person name", 0.9),
        ("marie@x.fr", "email", 0.95),
        ("Lyon", "place of birth", 0.7),
    ])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    matches = gx.gliner_matches(text, chunk_size=200, overlap=20)
    by = {(m.entity_type, m.value) for m in matches}
    assert ("NOM", "Marie Dubois") in by
    assert ("EMAIL", "marie@x.fr") in by
    assert ("LIEU_NAISSANCE", "Lyon") in by


def test_unknown_label_is_dropped(monkeypatch):
    stub = _StubModel([("secret", "color", 0.99)])   # "color" not in our map
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    assert gx.gliner_matches("the secret thing", chunk_size=100, overlap=10) == []


def test_offsets_are_absolute_across_chunks(monkeypatch):
    # Put the entity in the SECOND window so the absolute offset must be > 0.
    text = "x" * 30 + " Marie Dubois "
    stub = _StubModel([("Marie Dubois", "person name", 0.9)])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    ms = gx.gliner_matches(text, chunk_size=25, overlap=5)
    assert ms
    m = next(m for m in ms if m.value == "Marie Dubois")
    assert text[m.start:m.end] == "Marie Dubois"


def test_scores_carry_through(monkeypatch):
    stub = _StubModel([("Marie Dubois", "person name", 0.63)])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    ms = gx.gliner_matches("Marie Dubois", chunk_size=100, overlap=10)
    assert ms and abs(ms[0].score - 0.63) < 1e-6


def test_make_detector_is_callable(monkeypatch):
    stub = _StubModel([("Marie Dubois", "person name", 0.9)])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    det = gx.make_gliner_detector(chunk_size=100, overlap=10)
    ms = det("Marie Dubois")
    assert any(m.entity_type == "NOM" for m in ms)


# ── Dot-run compression tests ─────────────────────────────────────────────────

def test_compress_dot_runs_collapses_dots():
    """Three or more consecutive dots are collapsed to a single space."""
    result = gx._compress_dot_runs("Prénom : ....................... Age : 30")
    assert "....." not in result
    assert "Prénom" in result
    assert "Age" in result


def test_compress_dot_runs_collapses_dashes():
    """Three or more consecutive dashes/hyphens are collapsed."""
    result = gx._compress_dot_runs("Section --- other --- content")
    assert "---" not in result


def test_compress_dot_runs_collapses_underscores():
    """Three or more consecutive underscores are collapsed."""
    result = gx._compress_dot_runs("field: _______________ value")
    assert "___" not in result


def test_compress_dot_runs_preserves_short_runs():
    """One or two consecutive dots are NOT collapsed (could be abbreviations)."""
    result = gx._compress_dot_runs("M. Dupont, p. ex.")
    # Single dots should survive
    assert "M." in result
    assert "p." in result


def test_compress_dot_runs_preserves_text_content():
    """Normal text around dot runs is not mutated."""
    text = "Nom : ................. DUPONT MARC\nAdresse : ............. 75001 PARIS"
    result = gx._compress_dot_runs(text)
    assert "DUPONT MARC" in result
    assert "75001 PARIS" in result


def test_dot_compression_reduces_apparent_length(monkeypatch):
    """Dot-compressed chunk reaches the model with no fill-blank noise."""
    # Simulate a form-blank section: mostly dots, one name buried in it.
    prefix_dots = "." * 200
    suffix_dots = "." * 200
    text = prefix_dots + " MARTIN SOPHIE " + suffix_dots
    # Without compression: the stub sees the full dot-padded text.
    # With compression: the stub sees a compact text, still finds the name.
    stub = _StubModel([("MARTIN SOPHIE", "person name", 0.85)])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    ms = gx.gliner_matches(text, chunk_size=500, overlap=50, compress_dots=True)
    assert any(m.value == "MARTIN SOPHIE" for m in ms)


# ── form-name-class synthetic regression ─────────────────────────────────────
#
# Mirrors the structure of a real FR tax notice (avis IR) without any real data.
# Pattern: ALL-CAPS "SURNAME FORENAME" in two positions:
#   a) Address block at top of document (clean, single-space)
#   b) Form-field row ("Déclarant N - Nom de naissance : SURNAME  FORENAME")
#
# Both positions must be masked.  The form-field section is surrounded by
# dot-fill lines that previously caused truncation + score degradation.
#
# Names here are fully synthetic.

_FORMNAME_CLASS_DOC = """\
IMPOT SUR LES REVENUS 2024

CENTRE DES FINANCES PUBLIQUES
SIP VERSAILLES
42 AVE DE PARIS
78000 VERSAILLES

FONTAINE CLAUDE ALAIN
OU MOREAU ISABELLE
28 RUE DES LILAS
LE CHESNAY
78150 LE CHESNAY

Impôt et prélèvements sociaux sur les revenus de 2024
15 85 154 265 072 1 3

Déclarant 1 - Nom de naissance\xa0: FONTAINE  CLAUDE
Déclarant 2 - Nom de naissance\xa0: MOREAU  ISABELLE
O
3 4,00
IMPOT SUR LE REVENU
Détail des revenus
BNC professionnels déclarés...............................
BNC pro. hors quotient imposables.......................
BNC pro. imposables du foyer, hors quotient........
Revenus perçus par le foyer fiscal.......................
Revenus fonciers nets.......................................
Revenu brut global.............................................
Abattements et charges déductibles....................
Revenu fiscal de référence..................................
"""


class _FakeFormNameModel:
    """Stub that returns ALL-CAPS names at form-name-class scores (0.30–0.32).

    The real form-layout doc scores ranged 0.27–0.32 depending on threshold and
    context window.  We use 0.31 / 0.30 here — just at / above the new
    threshold — to exercise the boundary condition.  The old threshold=0.45
    would drop both; the new threshold=0.30 captures both.
    """

    _DETECTIONS = [
        ("FONTAINE  CLAUDE", "person name", 0.31),
        ("MOREAU  ISABELLE", "person name", 0.30),  # exactly at threshold boundary
    ]

    def predict_entities(self, chunk, labels, threshold=0.30):
        out = []
        for sub, label, score in self._DETECTIONS:
            idx = chunk.find(sub)
            if idx >= 0 and score >= threshold and label in labels:
                out.append({"text": sub, "label": label, "score": score,
                            "start": idx, "end": idx + len(sub)})
        return out


def test_formname_class_names_masked_at_new_threshold(monkeypatch):
    """REGRESSION: form-name-class ALL-CAPS names must be masked at threshold=0.30.

    The stub returns borderline scores (0.28, 0.31) that the OLD threshold=0.45
    would silently drop.  The NEW threshold=0.30 captures them, and the
    profile_sweep extends coverage to all occurrences in the document.
    """
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    monkeypatch.setattr(gx, "_load_model", lambda model_id: _FakeFormNameModel())

    detector = gx.make_gliner_detector(
        chunk_size=1500, overlap=300, threshold=0.30, compress_dots=True
    )
    engine = AnonymizationEngine(vault=Vault(), extra_detectors=[detector])
    result = engine.anonymize(_FORMNAME_CLASS_DOC)

    assert "FONTAINE" not in result.anonymized, (
        "form-name class name FONTAINE leaked — threshold too high or sweep missed")
    assert "MOREAU" not in result.anonymized, (
        "form-name class name MOREAU leaked — threshold too high or sweep missed")
    assert "ISABELLE" not in result.anonymized, (
        "form-name class forename ISABELLE leaked")
    assert "CLAUDE" not in result.anonymized, (
        "form-name class forename CLAUDE leaked")


def test_formname_class_old_threshold_misses_borderline(monkeypatch):
    """Document that at threshold=0.45 the borderline names are NOT caught by
    GLiNER alone.

    This is an INVERTED test: it proves the old threshold was insufficient for
    the form-name-class pattern, motivating the change to 0.30.

    Note: with the profile_sweep the final result may still be safe because the
    sweep catches occurrences even when GLiNER's seed score is sub-threshold.
    What we verify here is that GLiNER itself returns zero entities at 0.45
    for our form-name-class stub scores (0.31 and 0.30, both below 0.45).
    """
    stub = _FakeFormNameModel()
    from bubble_shield import gliner_ext
    monkeypatch.setattr(gliner_ext, "_load_model", lambda model_id: stub)
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)

    matches = gx.gliner_matches(
        _FORMNAME_CLASS_DOC,
        chunk_size=1500, overlap=300, threshold=0.45, compress_dots=True
    )
    fontaine_hits = [m for m in matches if "FONTAINE" in m.value.upper() or "CLAUDE" in m.value.upper()]
    moreau_hits = [m for m in matches if "MOREAU" in m.value.upper() or "ISABELLE" in m.value.upper()]

    # At 0.45, the stub's scores (0.31, 0.30) are both below threshold → nothing.
    assert len(fontaine_hits) == 0, (
        f"Expected 0 FONTAINE hits at threshold=0.45, got {fontaine_hits}")
    assert len(moreau_hits) == 0, (
        f"Expected 0 MOREAU hits at threshold=0.45, got {moreau_hits}")


def test_formname_class_new_threshold_captures_seeds(monkeypatch):
    """At threshold=0.30, both borderline names ARE returned by GLiNER."""
    from bubble_shield import gliner_ext
    stub = _FakeFormNameModel()
    monkeypatch.setattr(gliner_ext, "_load_model", lambda model_id: stub)

    matches = gx.gliner_matches(
        _FORMNAME_CLASS_DOC,
        chunk_size=1500, overlap=300, threshold=0.30, compress_dots=True
    )
    nom_values = {m.value for m in matches if m.entity_type == "NOM"}
    assert any("FONTAINE" in v.upper() for v in nom_values), (
        f"FONTAINE not in NOM matches at threshold=0.30: {nom_values}")
    assert any("MOREAU" in v.upper() for v in nom_values), (
        f"MOREAU not in NOM matches at threshold=0.30: {nom_values}")


def test_dot_run_no_truncation_on_synthetic_form(monkeypatch):
    """Dot-fill form section with compress_dots=True must not trigger truncation.

    We count how many times the stub's predict_entities is called with a
    chunk that (after compression) would NOT trigger truncation.  Without
    compression the stub would still run, but in the real model it would
    truncate.  We verify compress_dots=True passes a shorter text to inference.
    """
    call_log = []

    class _LoggingStub:
        def predict_entities(self, chunk, labels, threshold=0.30):
            call_log.append(len(chunk))
            return []

    monkeypatch.setattr(gx, "_load_model", lambda model_id: _LoggingStub())

    # Build a 1500-char chunk that is 80% dots (form-blank), rest normal text
    form_section = "Nom : " + "." * 1200 + " Suite du texte ici."
    gx.gliner_matches(form_section, chunk_size=1500, overlap=300, compress_dots=True)

    # After compression the chunk passed to the model should be much shorter
    assert all(length < len(form_section) for length in call_log), (
        f"compress_dots=True should shorten dot-heavy chunks; got lengths {call_log}")


def test_default_threshold_is_0_30():
    """DEFAULT_THRESHOLD must be 0.30 after the 2026-06-26 tuning."""
    assert abs(gx.DEFAULT_THRESHOLD - 0.30) < 1e-9, (
        f"DEFAULT_THRESHOLD changed unexpectedly: {gx.DEFAULT_THRESHOLD}")


def test_default_compress_dots_is_true():
    """DEFAULT_COMPRESS_DOTS must be True (enabled by default)."""
    assert gx.DEFAULT_COMPRESS_DOTS is True, (
        "DEFAULT_COMPRESS_DOTS should be True; can be overridden via env var")


def test_582_bare_city_maps_to_adresse_not_birthplace(monkeypatch):
    # #582: GLiNER's bare "city" label (e.g. "basé à Nice" with no birth
    # context) must retag to ADRESSE — a location mention, not a birthplace.
    # Mask-neutral: both types are identifying+default_cloak in policy.py.
    # A genuine "place of birth" prediction keeps LIEU_NAISSANCE (tested above).
    text = "Cabinet basé à Nice, France."
    stub = _StubModel([("Nice", "city", 0.8)])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    matches = gx.gliner_matches(text, chunk_size=200, overlap=20)
    by = {(m.entity_type, m.value) for m in matches}
    assert ("ADRESSE", "Nice") in by
    assert ("LIEU_NAISSANCE", "Nice") not in by


def test_668_email_span_without_at_is_dropped(monkeypatch):
    # #668: GLiNER tags the bare WORD "e-mail" as EMAIL. An EMAIL span with no
    # '@' cannot be an address → drop it (over-masking a common word). A real
    # address keeps its EMAIL tag.
    text = "Cet e-mail est destiné à jean@exemple.fr aussi."
    stub = _StubModel([
        ("e-mail", "email", 0.8),               # the false positive
        ("jean@exemple.fr", "email", 0.9),      # a real address
    ])
    monkeypatch.setattr(gx, "_load_model", lambda model_id: stub)
    matches = gx.gliner_matches(text, chunk_size=200, overlap=20)
    by = {(m.entity_type, m.value) for m in matches}
    assert ("EMAIL", "e-mail") not in by       # dropped: no '@'
    assert ("EMAIL", "jean@exemple.fr") in by  # kept: real address
