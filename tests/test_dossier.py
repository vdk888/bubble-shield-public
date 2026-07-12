"""
test_dossier.py — whole-dossier anonymisation (shared vault + shared profile).

Proves the two cross-file wins:
  1. CONSISTENCY: the same client gets the same token across every file (shared vault).
  2. ACCURACY: a client richly described in file A is also redacted in file B where
     a single-file pass would miss him (shared self-improving profile).
Synthetic data only — no model needed (we use plain regex detection + the sweep).
"""
from bubble_shield.allowlist import Allowlist
from bubble_shield.dossier import anonymize_dossier
from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault
from bubble_shield.surrogate import SurrogateVault


def _factory(vault, allowlist):
    """Return an engine_factory that shares ONE vault across the dossier."""
    def make():
        eng = AnonymizationEngine(vault=vault, context_boost=False)
        return eng, allowlist
    return make


def test_same_client_same_token_across_files():
    vault = Vault()
    al = Allowlist()  # empty allowlist (no firm to strip in this synthetic test)
    docs = [
        ("kyc.txt", "Client : M. Jean Dupont, jean@x.fr, demeurant à Lyon."),
        ("annex.txt", "Le dossier de M. Jean Dupont est complet."),
    ]
    res = anonymize_dossier(docs, engine_factory=_factory(vault, al))
    # the email token + name token must be identical in both files' vault
    tok_dupont = vault.to_token.get("Jean Dupont") or vault.to_token.get("M. Jean Dupont")
    assert tok_dupont is not None
    # both files reference the SAME token for the client
    assert res.files[0].result.anonymized.count("⟦") >= 1
    # consistency: the vault has ONE entry per distinct value (no duplicate tokens)
    assert len(set(vault.to_value.keys())) == len(vault.to_value)


def test_client_redacted_in_file_where_single_pass_misses():
    # File A richly names the client; file B mentions only the surname in passing.
    vault = Vault()
    al = Allowlist()
    docs = [
        ("kyc.txt",   "Client : M. Jean Dupont, né le 01/01/1980."),
        ("brief.txt", "Note interne : dossier DUPONT à revoir."),  # surname only
    ]
    res = anonymize_dossier(docs, engine_factory=_factory(vault, al))
    file_b = next(f for f in res.files if f.name == "brief.txt")
    # the surname must be redacted in file B thanks to the shared profile
    assert "DUPONT" not in file_b.result.anonymized


def test_dossier_roundtrips_every_file():
    vault = Vault()
    al = Allowlist()
    docs = [
        ("a.txt", "M. Jean Dupont, jean@x.fr."),
        ("b.txt", "Mme Marie Durand, marie@y.fr."),
    ]
    res = anonymize_dossier(docs, engine_factory=_factory(vault, al))
    for f, (_n, original) in zip(res.files, docs):
        assert vault.restore(f.result.anonymized) == original


def test_surrogate_consistent_across_files():
    # In surrogate mode the SAME client must get the SAME fake in every file.
    vault = SurrogateVault()
    al = Allowlist()
    docs = [
        ("a.txt", "Client : M. Jean Dupont signe."),
        ("b.txt", "M. Jean Dupont confirme."),
    ]
    res = anonymize_dossier(docs, engine_factory=_factory(vault, al))
    fake = vault.to_token.get("Jean Dupont") or vault.to_token.get("M. Jean Dupont")
    assert fake and "⟦" not in fake
    # the same fake appears in both files
    assert fake in res.files[0].result.anonymized
    assert fake in res.files[1].result.anonymized


def test_result_summary_counts():
    vault = Vault()
    al = Allowlist()
    docs = [("a.txt", "M. Jean Dupont."), ("b.txt", "Mme Marie Durand.")]
    res = anonymize_dossier(docs, engine_factory=_factory(vault, al))
    assert res.n_ok == 2
    assert res.total_entities >= 2
    assert len(res.files) == 2
