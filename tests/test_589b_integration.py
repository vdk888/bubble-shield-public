# tests/test_589b_integration.py — synthetic liasse; Gemma faked deterministically.
import importlib.util, pathlib
_MCP = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/bubble_shield_mcp.py"
_spec = importlib.util.spec_from_file_location("bsmcp_589bI", _MCP)
bsmcp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bsmcp)

# Synthetic liasse-shaped text carrying the 6 leak CLASSES (all fake values).
SIRET="321 654 987 00011"; PRENOM="Camille"; DOB="03011980"; VILLE_NAIS="Nantes"
CABINET="ACME COMPTA"; CP_VILLE="44000 NANTES"; TEL="0240000000"
SYNTH = (f"ETATS FISCAUX N° 2065-SD 2033-B 2033-C 2058-A liasse\n"
         f"Nom et adresse du conseil : {CABINET} RUE TEST 1 {CP_VILLE} {TEL}\n"
         f"Signataire: DURAND {PRENOM}\nSIRET {SIRET}\n"
         f"Associe: naissance {DOB} commune {VILLE_NAIS}\n")

def test_liasse_all_classes_masked(monkeypatch):
    # Force daemon "up" + a deterministic engine result via the real engine if available,
    # else monkeypatch _engine to return a stub whose anonymize() returns SYNTH unchanged
    # (simulating the fast pass MISSING everything — the worst case).
    class _V:
        def __init__(s): s.n=0
        def token_for(s,v,t): s.n+=1; return f"⟦{t}_{s.n:04d}⟧"
        def save(s,*a,**k): pass
    class _R:
        original=SYNTH; anonymized=SYNTH; verdict_state="masked_ok"
    class _E:
        vault=_V()
        def anonymize(s,t): return _R()
    monkeypatch.setattr(bsmcp, "_engine", lambda text, filename_basename="": (_E(), "/tmp/v.json", True))
    monkeypatch.setattr(bsmcp, "_gemma_extract_call", lambda text: [
        {"type":"SIRET","text":SIRET},{"type":"PRENOM","text":PRENOM},
        {"type":"DATE_NAISSANCE","text":DOB},{"type":"LIEU_NAISSANCE","text":VILLE_NAIS},
        {"type":"RAISON_SOCIALE","text":CABINET},{"type":"ADRESSE","text":CP_VILLE},
        {"type":"TELEPHONE","text":TEL}])
    out = bsmcp._anonymise_text(SYNTH, filename_basename="liasse.pdf")
    for leaked in (SIRET, PRENOM, DOB, VILLE_NAIS, CABINET, CP_VILLE, TEL):
        assert leaked not in out, f"{leaked} still clear after 2nd pass"
    assert "seconde passe" in out  # the latency/structured-form note is present
