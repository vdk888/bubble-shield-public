"""
test_llm_ext.py — the optional local-LLM (Ollama) prose layer.

Two things must hold:
  1. When no Ollama is reachable (CI, the VPS, any machine without it), the
     layer is a silent no-op and the engine behaves EXACTLY like the
     pure-regex build — enabling use_llm must never change or break output.
  2. When Ollama answers, we map its JSON to vault-tokenised spans correctly,
     tolerating the response-shape variation real models produce.
"""
from bubble_shield import llm_ext
from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault


def test_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(llm_ext, "is_available", lambda: False)
    assert llm_ext.llm_matches("Monsieur Jean Dupont, société Acme.") == []


def test_engine_use_llm_fails_open_to_regex(monkeypatch):
    # With Ollama down, use_llm=True must equal the plain regex result.
    monkeypatch.setattr(llm_ext, "is_available", lambda: False)
    text = "Contact : b.martin@example.fr, IBAN FR7630006000011234567890189."
    base = AnonymizationEngine(vault=Vault()).anonymize(text)
    withllm = AnonymizationEngine(vault=Vault(), use_llm=True).anonymize(text)
    assert withllm.anonymized == base.anonymized
    assert withllm.entity_count == base.entity_count


def _fake_ollama(payload):
    """Return a canned Ollama /api/chat JSON content string."""
    return payload


def test_parses_entities_shape(monkeypatch):
    text = "Le dossier de Jean Dupont, salarié de la société Solaris, à Lyon."
    monkeypatch.setattr(llm_ext, "is_available", lambda: True)
    monkeypatch.setattr(llm_ext, "_call_ollama", lambda t: _fake_ollama(
        '{"entities":[{"type":"PERSON","text":"Jean Dupont"},'
        '{"type":"ORG","text":"Solaris"},'
        '{"type":"LOCATION","text":"Lyon"}]}'))
    matches = llm_ext.llm_matches(text)
    types = {m.entity_type for m in matches}
    assert types == {"NOM", "SOCIETE", "ADRESSE"}
    # every span must map back to the exact substring it claims
    for m in matches:
        assert text[m.start:m.end] == m.value
    # LLM guesses are review-grade, never certain
    assert all(m.score < 1.0 for m in matches)


def test_tolerates_bare_list_and_unknown_labels(monkeypatch):
    text = "Madame Claire Bernard habite Marseille."
    monkeypatch.setattr(llm_ext, "is_available", lambda: True)
    monkeypatch.setattr(llm_ext, "_call_ollama", lambda t: (
        '[{"type":"PERSON","text":"Claire Bernard"},'
        '{"type":"EMAIL","text":"ignored@x.fr"},'      # not our job → dropped
        '{"type":"LOCATION","text":"Marseille"}]'))
    matches = llm_ext.llm_matches(text)
    vals = sorted(m.value for m in matches)
    assert vals == ["Claire Bernard", "Marseille"]


def test_bad_json_is_swallowed(monkeypatch):
    monkeypatch.setattr(llm_ext, "is_available", lambda: True)
    monkeypatch.setattr(llm_ext, "_call_ollama", lambda t: "not json at all{{")
    assert llm_ext.llm_matches("anything") == []


def test_llm_match_feeds_vault_token(monkeypatch):
    # End-to-end: a name only the LLM can see gets a real ⟦NOM_…⟧ token,
    # and round-trips back through the vault.
    text = "Rendez-vous avec Killian Vasseur mardi."
    monkeypatch.setattr(llm_ext, "is_available", lambda: True)
    monkeypatch.setattr(llm_ext, "_call_ollama", lambda t:
                        '{"entities":[{"type":"PERSON","text":"Killian Vasseur"}]}')
    eng = AnonymizationEngine(vault=Vault(), use_llm=True)
    res = eng.anonymize(text)
    assert "Killian Vasseur" not in res.anonymized
    assert "⟦NOM_0001⟧" in res.anonymized
    assert eng.deanonymize(res.anonymized) == text
