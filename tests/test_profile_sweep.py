"""
test_profile_sweep.py — the self-improving two-pass loop.

Pass 1 discovers client PII → ClientProfile → pass 2 sweeps for detached/partial
references + reuses the profile across a dossier. The critical guards (don't
learn firm/fund/heading words as the client; don't glue cross-newline product
names onto a person) are regression-tested. Synthetic data only.
"""
from bubble_shield.allowlist import Allowlist
from bubble_shield.profile_sweep import ClientProfile, two_pass_detect, _distinctive_tokens
from bubble_shield.recognizers import Match


def _m(entity_type, value, score=1.0):
    return Match(start=0, end=len(value), entity_type=entity_type, value=value,
                 score=score)


# ── learning guards ─────────────────────────────────────────────────────────

def test_learns_structured_pii_verbatim():
    p = ClientProfile()
    p.learn([_m("EMAIL", "marie@gmail.com"), _m("IBAN", "FR7630006000011234567890189")])
    assert "marie@gmail.com" in p.values
    assert "FR7630006000011234567890189" in p.values


def test_learns_real_person_name_with_first_name():
    p = ClientProfile()
    p.learn([_m("NOM", "Marie Dubois")])   # "Marie" is a gazetteer first name
    assert "Marie Dubois" in p.values
    assert "Dubois" in p.name_tokens


def test_does_not_learn_single_bare_word_as_name():
    # "Contrat" alone (a heading) must never become a client name token.
    p = ClientProfile()
    p.learn([_m("NOM", "Contrat")])
    assert "Contrat" not in p.name_tokens
    assert not p.values


def test_does_not_learn_fund_heading_pair():
    # Regex NOM over-fires on "Contrat EUROPE" (no person signal) → not learned.
    p = ClientProfile()
    p.learn([_m("NOM", "Contrat EUROPE", score=0.8)])
    assert "EUROPE" not in p.name_tokens


def test_newline_glued_name_keeps_line2_name_drops_known_form_words():
    # The regex NOM over-extends across line breaks. We flatten lines and the
    # stopword list drops the common form/product/heading words while keeping
    # real name tokens — crucially incl. a name that sits on LINE 2 after a lone
    # civility label ("Monsieur\nJeremy LOUIS"), which a first-line-only rule lost.
    toks = _distinctive_tokens("Marie Dubois\nContrat compte total")
    assert "Dubois" in toks
    # known form/heading words are dropped
    assert "Contrat" not in toks and "compte" not in toks and "total" not in toks
    toks2 = _distinctive_tokens("Monsieur\nJeremy LOUIS\nVotre conjoint")
    assert "Jeremy" in toks2 and "LOUIS" in toks2
    assert "Votre" not in toks2 and "conjoint" not in toks2

    # NOTE: a truly novel product word (e.g. a fund brand) the stoplist doesn't
    # know CAN survive as a token CANDIDATE here — that's by design. The
    # precision gate lives in ClientProfile.learn() (allowlist + person-signal),
    # tested separately, not in this raw tokeniser.


def test_allowlist_blocks_firm_from_profile():
    al = Allowlist(phrases=("acme conseil",), email_domains=("acme.com",))
    p = ClientProfile()
    p.learn([_m("NOM", "ACME Conseil"), _m("EMAIL", "bob@acme.com"),
             _m("NOM", "Marie Dubois")], allowlist=al)
    assert "marie dubois" in {v.lower() for v in p.values}
    assert not any("acme" in v.lower() for v in p.values)


# ── sweeping ────────────────────────────────────────────────────────────────

def test_sweep_finds_detached_reference():
    p = ClientProfile()
    p.learn([_m("NOM", "Marie Dubois")])
    # A later doc mentions only the surname, far from any first name.
    hits = p.sweep("Le dossier de Mme DUBOIS est complet.")
    assert any("DUBOIS" in h.value for h in hits)


def test_sweep_matches_value_across_whitespace():
    p = ClientProfile()
    p.learn([_m("ADRESSE", "12 avenue des Lilas")])
    hits = p.sweep("réside 12 avenue\ndes Lilas à Lyon")
    assert any("avenue" in h.value for h in hits)


# ── cross-doc end-to-end (the prize) ────────────────────────────────────────

class _StubEngine:
    """Minimal engine stub: returns preset entities, supports extra_detectors."""
    def __init__(self, entities):
        self._entities = entities
        self.extra_detectors = []
    def anonymize(self, text):
        extra = []
        for d in self.extra_detectors:
            extra.extend(d(text))
        class R:  # noqa
            pass
        r = R()
        r.entities = list(self._entities) + extra
        return r


def test_two_pass_carries_profile_across_docs():
    # Doc 1 (KYC) reveals the client; doc 2 (DER) only has the surname buried.
    eng = _StubEngine([_m("NOM", "Marie Dubois"), _m("EMAIL", "marie@gmail.com")])
    _, profile = two_pass_detect("KYC: Marie Dubois, marie@gmail.com", eng)
    assert "Dubois" in profile.name_tokens
    # Reuse the SAME profile on a second doc where only "DUBOIS" appears.
    hits = profile.sweep("Document d'entrée: Mme DUBOIS, client.")
    assert any("DUBOIS" in h.value for h in hits)
