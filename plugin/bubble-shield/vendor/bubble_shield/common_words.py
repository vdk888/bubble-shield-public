# bubble_shield/common_words.py
"""common_words.py — conservative French common-word stoplist for NOM precision (#348).

Drops ONLY NOM spans whose value EXACTLY matches a curated common-word list.
NEVER suppresses arbitrary lowercase tokens — a real lowercase form-field name
("dupont") must still be masked. Recall is the priority; an unlisted common word
slipping through is acceptable, a leaked name is not.
"""
from __future__ import annotations
from typing import Iterable, List

# Curated list of ordinary French words GLiNER mis-flags as person-names in
# financial docs. Lowercase, accent-bearing. Extend as the safe-list surfaces more.
_COMMON = {
    "marchés", "marché", "financiers", "financier", "investissements",
    "investissement", "immobilier", "immobilière", "patrimoine", "patrimonial",
    "épargne", "assurance", "contrat", "fonds", "actions", "obligations",
    "rendement", "capital", "souscription", "versement", "rachat", "arbitrage",
    "gestion", "conseil", "société", "entreprise", "client", "dossier",
    "document", "montant", "euros", "compte", "banque", "crédit", "placement",
}

def is_common_word(value: str) -> bool:
    """True iff `value` (single token, case-insensitive) is on the common-word list."""
    v = str(value).strip().lower()
    return v in _COMMON

def filter_matches(matches: Iterable) -> List:
    """Drop NOM matches that are common words. Non-NOM and non-listed pass through."""
    out = []
    for m in matches:
        if getattr(m, "entity_type", "") == "NOM" and is_common_word(getattr(m, "value", "")):
            continue
        out.append(m)
    return out
