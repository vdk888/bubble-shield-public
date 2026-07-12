import importlib.util, pathlib
_MCP = pathlib.Path(__file__).resolve().parents[1] / "plugin/bubble-shield/scripts/bubble_shield_mcp.py"
_spec = importlib.util.spec_from_file_location("bsmcp_589b", _MCP)
bsmcp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bsmcp)

def test_liasse_markers_detected():
    # Synthetic liasse-shaped text with several distinct fiscal form numbers.
    text = ("ETATS FISCAUX  N° 2065-SD  Exercice ... 2033-B ... 2033-C ... "
            "2058-A ... 2059-A ... liasse fiscale ...")
    assert bsmcp._is_structured_form(text) is True

def test_normal_prose_not_a_form():
    text = ("Bonjour, voici le compte rendu de notre reunion de mardi. Nous avons "
            "discute du budget et des prochaines etapes du projet client.")
    assert bsmcp._is_structured_form(text) is False

def test_below_marker_floor_not_a_form():
    # Only ONE marker (< _FORM_MARKER_MIN) — a passing mention, not a form.
    text = "Le formulaire 2065 est mentionne une seule fois dans ce paragraphe de prose."
    assert bsmcp._is_structured_form(text) is False

def test_bare_year_prose_not_a_form():
    # Ordinary prose mentioning multiple bare years must NOT trigger (the reviewer's FP).
    text = "En 2033, nous prevoyons environ 2050 emplois d'ici 2059 dans la region, un vrai projet."
    assert bsmcp._is_structured_form(text) is False

def test_suffixed_form_numbers_still_detected():
    # Real liasse form numbers (suffixed) must still trigger.
    text = "Bilan 2033-B ... compte de resultat 2033-C ... tableau 2058-A ..."
    assert bsmcp._is_structured_form(text) is True

def test_glued_form_numbers_still_detected():
    # Degraded/glued extraction (the incident failure mode) must STILL fingerprint.
    text = "determinationresultat2033Bfiscal2058Aetbilan2059Acloture"
    assert bsmcp._is_structured_form(text) is True

def test_label_only_bilan_detected():
    # A bilan with standard headings but NO form numbers must fingerprint.
    text = "BILAN ACTIF ... COMPTE DE RESULTAT ... CAPITAUX PROPRES ... exercice clos"
    assert bsmcp._is_structured_form(text) is True

def test_ordinary_prose_with_business_words_not_a_form():
    # Guard against over-trigger: ordinary prose mentioning business terms must NOT hit 3.
    text = ("Le bilan de notre reunion est positif. Nous avons parle du resultat commercial "
            "et des capitaux disponibles pour le projet client cette annee.")
    assert bsmcp._is_structured_form(text) is False
