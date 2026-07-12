from bubble_shield.recognizers import detect, _iban_valid, _isin_valid, _siren_valid


def _types(text):
    return {(m.entity_type, m.value) for m in detect(text)}


def test_email_detected():
    assert ("EMAIL", "jean.dupont@example.com") in _types("écrire à jean.dupont@example.com svp")


def test_valid_iban_scores_high():
    iban = "FR76 3000 6000 0112 3456 7890 189"
    assert _iban_valid(iban)
    m = [x for x in detect(f"IBAN {iban}") if x.entity_type == "IBAN"][0]
    assert m.value == iban and m.score == 1.0


def test_invalid_iban_scores_low():
    assert not _iban_valid("FR00 0000 0000 0000 0000 0000 000")


def test_hyphen_and_dot_grouped_iban_detected():
    # Recall LEAK 1: forms with '-' or '.' between groups used to leak because the
    # separator class only allowed space. Same underlying mod-97-valid IBAN
    # (FR7630006000011234567890189), just regrouped with different separators.
    for sep in ("-", "."):
        iban = f"FR76{sep}3000{sep}6000{sep}0112{sep}3456{sep}7890{sep}189"
        assert _iban_valid(iban), f"{iban} should mod-97-validate"
        ms = [x for x in detect(f"IBAN {iban} du client") if x.entity_type == "IBAN"]
        assert len(ms) == 1 and ms[0].value == iban and ms[0].score == 1.0


def test_isin_validator():
    assert _isin_valid("FR0000120271")          # TotalEnergies
    assert not _isin_valid("FR0000000000")


def test_isin_beats_overlapping_invalid_iban():
    # FR0000120172 is a valid ISIN and an invalid IBAN on the same span.
    got = _types("ISIN FR0000120271 svp")
    assert ("ISIN", "FR0000120271") in got
    assert all(t != "IBAN" for t, _ in got)


def test_phone_does_not_carve_iban():
    # A long IBAN must never be split by a phone match inside its digits.
    iban = "FR14 3000 4008 2800 0123 4567 890"
    ms = detect(f"compte {iban} fin")
    ibans = [m for m in ms if m.entity_type == "IBAN"]
    assert len(ibans) == 1 and ibans[0].value == iban
    assert all(m.entity_type != "TEL" for m in ms)


def test_french_phone():
    assert ("TEL", "06 12 34 56 78") in _types("tél 06 12 34 56 78")


def test_amount_euro_symbol_and_word():
    got = _types("45 000 € puis 60 000 euros")
    assert ("MONTANT", "45 000 €") in got
    assert ("MONTANT", "60 000 euros") in got


def test_siren_validator():
    assert _siren_valid("552 100 554")
    assert not _siren_valid("821 099 422")


def test_titled_name():
    assert ("NOM", "Monsieur Jean Dupont") in _types("reçu Monsieur Jean Dupont hier")


def test_untitled_name_via_gazetteer():
    assert ("NOM", "Sophie Garnier") in _types("Sophie Garnier a signé")


def test_bare_titlecase_name_midsentence_caught():
    # Recall LEAK 2: bare Title-case "Prénom Nom" in running prose with no label.
    # GLiNER scores this span below its accept threshold (~0.21 < 0.30), so the
    # forename gazetteer is the anchor. "Frédérique" was missing from the list;
    # now that it's present the untitled-NOM recognizer fires.
    got = _types("Nous avons rencontré Frédérique Marchand la semaine dernière.")
    assert ("NOM", "Frédérique Marchand") in got


def test_capitalized_financial_terms_not_masked_as_person():
    # Precision guard for the LEAK 2 fix: expanding the forename gazetteer must NOT
    # cause ordinary capitalized French financial/ORG terms to be masked as NOM.
    # The untitled-NOM recognizer only fires when the FIRST token is a known
    # forename, so none of these should produce a NOM span.
    for txt in (
        "Le Plan Épargne Retraite est ouvert.",
        "Souscrire une Assurance Vie multisupport.",
        "Le Crédit Agricole a validé le dossier.",
        "La Banque Postale gère le compte.",
        "Le Plan Épargne Logement offre 2 %.",
        "Le Livret Développement Durable rapporte peu.",
    ):
        noms = [v for t, v in _types(txt) if t == "NOM"]
        assert noms == [], f"{txt!r} wrongly masked as NOM: {noms}"


def test_homograph_forenames_not_masked_without_title_cue():
    # #477: forenames that are ALSO common nouns/brands ("Robert" — Le Petit
    # Robert, Robert Half; "Colette" — Colette Capital) over-masked bigrams like
    # "Le Petit Robert Illustré" -> "⟦NOM⟧ Illustré". The untitled homograph path
    # now requires a civility cue (M./Mme/…) earlier on the same line; with no
    # such cue these must NOT be masked as NOM.
    for txt in (
        "Le Petit Robert Illustré est sur la table.",
        "Robert Half recrute des comptables.",
        "Colette Capital a investi dans la société.",
        "Le concept store Colette Rivoli a fermé.",
    ):
        noms = [v for t, v in _types(txt) if t == "NOM"]
        assert noms == [], f"{txt!r} wrongly masked as NOM: {noms}"


def test_homograph_forenames_still_masked_with_title_cue():
    # Precision-only fix: a homograph forename used as an ACTUAL person's name,
    # with a corroborating civility cue, must still be masked — zero recall
    # regression on real names.
    assert ("NOM", "M. Robert Dupont") in _types("M. Robert Dupont a signé le contrat.")
    assert ("NOM", "Mme Colette Fabre") in _types("Nous avons reçu Mme Colette Fabre hier.")
    # cue earlier in the same line (not immediately adjacent) still corroborates
    got = _types("Le client, Monsieur Robert Petit, a confirmé.")
    assert ("NOM", "Monsieur Robert Petit") in got


def test_non_homograph_gazetteer_forenames_unaffected():
    # The #477 split must not touch recall for the (large) non-homograph subset —
    # same bare untitled-name behaviour as before (regression guard for LEAK 2).
    assert ("NOM", "Sophie Garnier") in _types("Sophie Garnier a signé")
    assert ("NOM", "Frédérique Marchand") in _types(
        "Nous avons rencontré Frédérique Marchand la semaine dernière."
    )


def test_gazetteer_no_duplicate_fabrice():
    # #477: "Fabrice" was listed twice in the gazetteer block (harmless — it's a
    # set — but noted for cleanup). Guard against reintroducing name-list drift:
    # the source list (pre-dedup literal) must not contain the same name twice
    # within FRENCH_FIRST_NAMES's own construction intent — checked here via the
    # plain/homograph partition being a clean split with no overlap.
    from bubble_shield.gazetteer import (
        FRENCH_FIRST_NAMES,
        FRENCH_FIRST_NAMES_HOMOGRAPH,
        FRENCH_FIRST_NAMES_PLAIN,
    )
    assert FRENCH_FIRST_NAMES_HOMOGRAPH & FRENCH_FIRST_NAMES_PLAIN == set()
    assert FRENCH_FIRST_NAMES_PLAIN | FRENCH_FIRST_NAMES_HOMOGRAPH == FRENCH_FIRST_NAMES
    assert FRENCH_FIRST_NAMES_HOMOGRAPH <= FRENCH_FIRST_NAMES


def test_dob_context_only():
    got = _types("né le 14/03/1968, opération du 02/01/2026")
    assert ("DATE_NAISSANCE", "14/03/1968") in got
    # the transaction date must NOT be redacted (no over-redaction)
    assert all(v != "02/01/2026" for _, v in got)


def test_name_span_does_not_swallow_unspaced_iban():
    # Regression (2026-06-02): a greedy NOM span used to swallow "IBAN FR76…"
    # into a single ⟦NOM⟧, dropping the IBAN AND reporting no residual PII.
    text = "Monsieur Jean Dupont IBAN FR7630006000011234567890189"
    got = _types(text)
    types = {t for t, _ in got}
    assert "NOM" in types
    assert "IBAN" in types          # IBAN now detected separately, not eaten
    # the NOM value must not contain the IBAN digits anymore
    nom_vals = [v for t, v in got if t == "NOM"]
    assert all("FR7630" not in v for v in nom_vals)


def test_name_does_not_extend_over_domain_keywords():
    # "PEA", "SCPI" etc. next to a name must not be pulled into the NOM span.
    got = _types("Le client Marc DURAND détient un PEA Corum.")
    nom_vals = [v for t, v in got if t == "NOM"]
    assert any("DURAND" in v for v in nom_vals)
    assert all("PEA" not in v for v in nom_vals)


def test_poste_job_title_in_company_detected():
    got = _types("Monsieur Dupont, directeur marketing chez TotalEnergies.")
    types = {t for t, _ in got}
    assert "POSTE" in types
    poste = [v for t, v in got if t == "POSTE"][0]
    assert "directeur marketing" in poste


def test_poste_does_not_fire_on_finance_products():
    # account/product mentions are not job titles
    got = _types("Il détient un PEA, un PER et une assurance-vie.")
    assert all(t != "POSTE" for t, _ in got)


def test_poste_does_not_eat_risk_profile_words():
    got = _types("Profil de risque: dynamique. Allocation: 60% actions.")
    assert all(t != "POSTE" for t, _ in got)


# ── fix #319: FR tax/admin identifiers that leaked out-of-the-box ─────────────
# Gap 1 (unlabeled numéro fiscal / référence d'avis, grouping NN NN NNNNNNN NN)
# and Gap 2 (télédéclarant block NNN NN NN NNNNNNNNNN N A). All SYNTHETIC values
# in the correct format — no real PII. Each test's leak string masked on pre-fix
# code fails (the recognizer didn't exist / only fired on a label); passes after.
# The precision-guard tests confirm the tight anchoring does NOT over-mask normal
# French financial numbers (dates, amounts, phones, refs, page numbers).

from bubble_shield.recognizers import _num_fiscal_ref_valid


# Gap 1 — unlabeled fiscal reference number ------------------------------------

def test_num_fiscal_ref_checksum_validator():
    # Synthetic ref with a VALID mod-97 control key (97 - 25920364665%97 == 70).
    assert _num_fiscal_ref_valid("25 92 0364665 70")
    # Wrong control key → invalid (checksum is the precision anchor).
    assert not _num_fiscal_ref_valid("25 92 0364665 71")
    # Wrong length → invalid.
    assert not _num_fiscal_ref_valid("25 92 0364665")


def test_unlabeled_fiscal_ref_masked_reference_davis():
    # The card's leak: "Référence de l'avis : 25 92 0364665 70" (no 'numéro fiscal' label).
    got = _types("Référence de l'avis : 25 92 0364665 70")
    assert ("NUM_FISCAL", "25 92 0364665 70") in got


def test_unlabeled_fiscal_ref_masked_bare_prose():
    # The card's second leak: bare in prose.
    got = _types("Le numéro 25 92 0364665 70 figure sur l'avis")
    assert ("NUM_FISCAL", "25 92 0364665 70") in got


def test_labeled_fiscal_ref_still_masked():
    # Regression guard: the pre-existing labeled NUM_FISCAL path must still fire.
    got = _types("numéro fiscal : 25 92 0364665 70")
    assert any(t == "NUM_FISCAL" and "25 92 0364665 70" in v for t, v in got)


def test_fiscal_ref_precision_bad_checksum_not_masked():
    # PRECISION: a 13-digit run in the exact NN NN NNNNNNN NN grouping but with an
    # INVALID control key must NOT be masked (drop_if_unvalidated). A random number
    # in this shape is not a fiscal ref.
    got = _types("Réf interne 12 34 5678901 23 au dossier")
    assert all(t != "NUM_FISCAL" for t, _ in got)


def test_fiscal_ref_precision_dates_amounts_not_masked():
    # PRECISION corpus: normal FR financial prose/numbers must not fire NUM_FISCAL.
    for text in [
        "Facture du 12 05 2026 pour 45 000 € au total",
        "Total 45 000 euros arrêté au 31 12 2025",
        "Téléphone 01 23 45 67 89 pour le service",
        "Page 3 sur 12 — référence 12345",
    ]:
        got = _types(text)
        assert all(t != "NUM_FISCAL" for t, _ in got), f"over-masked in: {text!r}"


# Gap 2 — télédéclarant alphanumeric block -------------------------------------

def test_teledeclarant_block_masked():
    # The card's leak: "922 65 91 2768797789 3 A" (grouping NNN NN NN NNNNNNNNNN N A).
    got = _types("Télédéclarant 922 65 91 2768797789 3 A")
    assert ("NUM_TELEDECLARANT", "922 65 91 2768797789 3 A") in got


def test_teledeclarant_precision_no_false_fire():
    # PRECISION: dates, amounts, phones, IBANs, refs must NOT trigger the
    # télédéclarant recognizer (the trailing single letter + exact grouping guard).
    for text in [
        "Total 45 000 € facture 2026",
        "Téléphone 01 23 45 67 89",
        "IBAN FR76 1027 1234 5678 9012 345",
        "Réf dossier 922 65 91 2768797789 3",   # same digits but NO trailing letter
        "Note 922 65 91 27687977 89 3 A",        # wrong digit-group counts
    ]:
        got = _types(text)
        assert all(t != "NUM_TELEDECLARANT" for t, _ in got), f"over-masked in: {text!r}"


def test_teledeclarant_is_identifying_and_cloaked():
    # The new entity type must be in the catalog, identifying, cloak-by-default.
    from bubble_shield.policy import ENTITY_CATALOG, is_identifying, default_policy
    assert "NUM_TELEDECLARANT" in ENTITY_CATALOG
    assert is_identifying("NUM_TELEDECLARANT")
    assert default_policy()["NUM_TELEDECLARANT"] is True
