import importlib.util, pathlib
_GC = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/gemma_classifier.py"
_spec = importlib.util.spec_from_file_location("gc_589b", _GC)
gc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gc)

def test_parse_extract_output_typed_lines():
    raw = "PRENOM: Jean\nSIRET: 123 456 789 00011\nLIEU_NAISSANCE: Lyon\nMOT: taux"
    spans = gc._parse_extract(raw)
    kinds = {(s["type"], s["text"]) for s in spans}
    assert ("PRENOM", "Jean") in kinds
    assert ("SIRET", "123 456 789 00011") in kinds
    assert ("LIEU_NAISSANCE", "Lyon") in kinds
    # a non-PII 'MOT' line is dropped
    assert all(s["type"] != "MOT" for s in spans)

def test_parse_extract_empty_on_garbage():
    assert gc._parse_extract("(no PII found)") == []

def test_parse_extract_drops_quoted_aucune_sentinel():
    # Gemma sometimes echoes the sentinel back wrapped in guillemets/quotes and
    # with stray whitespace/case — must still be dropped, not treated as a span.
    raw = (
        "NOM: « (aucune) »\n"
        "PRENOM:  \"(AUCUNE)\"  \n"
        "SIRET: '  aucune  '\n"
        "ADRESSE: 12 rue des Lilas"
    )
    spans = gc._parse_extract(raw)
    kinds = {(s["type"], s["text"]) for s in spans}
    assert kinds == {("ADRESSE", "12 rue des Lilas")}
