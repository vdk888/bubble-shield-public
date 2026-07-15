import importlib.util, pathlib, types, pytest
_MCP = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/bubble_shield_mcp.py"
_spec = importlib.util.spec_from_file_location("bsmcp_589b3", _MCP)
bsmcp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bsmcp)

class _FakeVault:
    def __init__(self): self.n = 0
    def token_for(self, value, entity_type):
        self.n += 1
        return f"⟦{entity_type}_{self.n:04d}⟧"

class _Res:
    # entity_count / has_residual model the FAST-PASS outcome. Defaults (0, False)
    # = "fast pass found nothing" → the #589 suspicious-empty case that must still
    # fail closed when Gemma also finds nothing. Tests exercising the "fast pass
    # already masked real PII" path pass entity_count>0 explicitly.
    def __init__(self, original, anonymized, entity_count=0, has_residual=False):
        self.original, self.anonymized = original, anonymized
        self.entity_count = entity_count
        self.has_residual = has_residual

class _Engine:
    def __init__(self): self.vault = _FakeVault()

def test_second_pass_masks_gemma_spans(monkeypatch):
    # Gemma finds a forename the fast pass left clear.
    monkeypatch.setattr(bsmcp, "_gemma_extract_call",
                        lambda text: [{"type": "PRENOM", "text": "Jean"}])
    res = _Res(original="... N° 2065 ...", anonymized="Signataire: ⟦NOM_0001⟧ Jean")
    out = bsmcp._gemma_second_pass(res, _Engine())
    assert "Jean" not in out          # forename now masked
    assert "⟦PRENOM_0001⟧" in out

def test_second_pass_failclosed_on_daemon_error(monkeypatch):
    def boom(text): raise ConnectionError("daemon down")
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", boom)
    res = _Res(original="... 2065 ...", anonymized="raw body with Jean")
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())

def test_second_pass_failclosed_on_empty_spans(monkeypatch):
    # A substantial structured form where Gemma finds NOTHING is suspicious → fail-closed.
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [])
    res = _Res(original="x"*200, anonymized="a fairly long masked body "*10)
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())

def test_second_pass_failclosed_sub120_empty_spans(monkeypatch):
    # #589-B final review: the sub-120 carve-out is REMOVED. A triggered structured
    # form where Gemma finds NOTHING must fail closed regardless of length — "nothing
    # to mask" on a fingerprinted form is itself suspicious at ANY size.
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [])
    short_body = "short masked body"  # well under 120 chars
    assert len(short_body) < 120
    res = _Res(original="N° 2065", anonymized=short_body)
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())

def test_second_pass_note_present_when_spans_applied(monkeypatch):
    # Sanity counterpart: when a span IS applied, the honest note IS present.
    monkeypatch.setattr(bsmcp, "_gemma_extract_call",
                        lambda text: [{"type": "PRENOM", "text": "Jean"}])
    res = _Res(original="... N° 2065 ...", anonymized="Signataire: ⟦NOM_0001⟧ Jean")
    out = bsmcp._gemma_second_pass(res, _Engine())
    assert "Jean" not in out
    assert "seconde passe" in out

def test_second_pass_failclosed_on_nonmatching_spans(monkeypatch):
    # Gemma returns spans, but NONE match the text → nothing applied → fail-closed on a substantial form.
    monkeypatch.setattr(bsmcp, "_gemma_extract_call",
                        lambda text: [{"type": "NOM", "text": "ValeurQuiNexistePas"}])
    res = _Res(original="x"*200, anonymized="un corps masqué assez long "*10)
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())


# ── #589-D (2026-07-15): the fast-pass-already-covered-it fix ────────────────────
# A structured form the fast pass ALREADY fully masked (entity_count>0, no residual)
# where Gemma runs fine + finds 0 to add is VERIFIED-CLEAN, not unverified. The old
# code fail-closed it → a well-masked liasse could NEVER index (observed live: 1
# liasse stuck at 96%). Fix: return the masked body in this case, while STILL failing
# closed when the fast pass found ~nothing (the #589 suspicious-empty danger).

def test_form_fully_masked_by_fastpass_gemma_finds_nothing_is_VERIFIED(monkeypatch):
    """THE FIX: fast pass masked real PII (entity_count>0, no residual) + Gemma ran
    fine + 0 new spans → return the masked body (do NOT fail closed)."""
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [])  # Gemma: nothing to add
    res = _Res(original="Liasse fiscale "+"mot "*100,
               anonymized="⟦NOM_0001⟧ ⟦SIRET_0002⟧ "+"masqué "*100,
               entity_count=7, has_residual=False)   # fast pass already caught 7
    out = bsmcp._gemma_second_pass(res, _Engine())
    assert out.startswith("⟦NOM_0001⟧"), "the already-masked body is returned"
    assert "seconde passe" in out, "the structured-form note is still appended"


def test_form_fastpass_found_NOTHING_gemma_finds_nothing_STILL_failclosed(monkeypatch):
    """THE PRESERVED #589 GUARANTEE: fast pass found ~nothing on a substantial form
    (entity_count==0) + Gemma also finds nothing → the extraction may have hidden
    entities from BOTH passes → MUST still fail closed. This is the whole point of
    the escalation and must not regress."""
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [])
    res = _Res(original="Liasse fiscale "+"mot "*100,
               anonymized="corps assez long sans jetons "*10,
               entity_count=0, has_residual=False)   # fast pass caught NOTHING
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())


def test_form_with_residual_never_verified_even_if_fastpass_had_entities(monkeypatch):
    """Belt-and-suspenders: if the fast pass left RESIDUAL visible PII (a real leak
    marker), the applied==0 path must NOT certify it clean, even with entity_count>0.
    has_residual=True → not the verified-clean case → fail closed."""
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [])
    res = _Res(original="Liasse fiscale "+"mot "*100,
               anonymized="⟦NOM_0001⟧ "+"masqué "*100,
               entity_count=3, has_residual=True)    # masked some BUT residual remains
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())


def test_gemma_failure_still_failclosed_even_if_fastpass_covered_it(monkeypatch):
    """The fix must NOT weaken the daemon-failure guarantee: if Gemma actually FAILS
    (exception), fail closed even when the fast pass had entities — we never verified."""
    def boom(text): raise TimeoutError("gemma timed out")
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", boom)
    res = _Res(original="Liasse "+"mot "*100, anonymized="⟦NOM_0001⟧ "+"x "*100,
               entity_count=9, has_residual=False)
    with pytest.raises(bsmcp.StructuredFormUnverifiedError):
        bsmcp._gemma_second_pass(res, _Engine())
