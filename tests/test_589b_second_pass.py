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
    def __init__(self, original, anonymized):
        self.original, self.anonymized = original, anonymized

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
