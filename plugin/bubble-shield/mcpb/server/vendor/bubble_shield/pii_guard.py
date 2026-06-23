"""pii_guard.py — no-PII guard-rail for custom field configuration.

THE PRINCIPLE: a custom field is described by CATEGORY or PATTERN, never by a
concrete PII instance. When a user supplies a "pattern" or "gliner_label", we
run our own detectors on it to make sure they're not inadvertently giving us a
real IBAN, email, or proper noun (e.g. "hide my client Durand").

This module is the callable unit-testable core; bubble_shield_mcp.py calls it
before writing any config. It can also be invoked from a CLI or the webapp.

NEVER echo the offending value in the reason string — the guard's whole point
is not to store or surface PII, and the reason goes back to the user/LLM.
"""
from __future__ import annotations

import re
from typing import Optional


# ── Regex metacharacter detection ─────────────────────────────────────────────

# Characters that appear in regex patterns but not in literal data values.
_METACHAR_RE = re.compile(r'[\\[\]{}()+*?^$|]|\B\\d\B|\B\\w\B|\B\\s\B')


def _has_regex_metachar(s: str) -> bool:
    """Return True if `s` contains at least one regex metacharacter."""
    return bool(_METACHAR_RE.search(s))


def _looks_like_literal_data(s: str) -> bool:
    """Return True when the string looks like a concrete data value rather than
    a pattern descriptor (no metacharacters AND is data-shaped).

    Data-shaped heuristics:
    - Contains '@' (email)
    - 6+ consecutive digits (ID/IBAN fragment)
    - ALL-CAPS word run of 2+ separate words (e.g. "MARC DURAND")
    """
    if "@" in s:
        return True
    if re.search(r'\d{6,}', s):
        return True
    # Two or more ALL-CAPS words (like a name in shouted form)
    words = s.split()
    caps_words = [w for w in words if re.fullmatch(r'[A-ZÉÈÀÂÎÔÙÛ]{2,}', w)]
    if len(caps_words) >= 2:
        return True
    return False


# ── Proper-noun heuristic for GLiNER labels ───────────────────────────────────

# Common prepositions/articles that can appear capitalised in multi-word phrases
_COMMON_SMALL_WORDS = {
    "de", "du", "des", "la", "le", "les", "un", "une", "of", "the",
    "en", "au", "aux", "par", "sur", "sous", "dans", "avec",
    "and", "or", "for", "a", "an",
}


def _looks_like_proper_noun_label(word: str) -> bool:
    """Return True if `word` looks like a proper noun (capitalised, ≥5 chars,
    not a common article/preposition).

    This catches "Durand", "Martin", "Paris" — words a user might
    accidentally pass as a GLiNER category label.
    """
    if not word:
        return False
    if word.lower() in _COMMON_SMALL_WORDS:
        return False
    # Title-case (capitalised first letter) AND ≥ 5 chars → likely proper noun
    if word[0].isupper() and len(word) >= 5 and not word.isupper():
        return True
    # All-caps ≥ 3 chars also suspicious (type codes are usually <3 chars like "M.")
    if word.isupper() and len(word) >= 3:
        return True
    return False


# ── Core guard function ───────────────────────────────────────────────────────

def check_input(
    value: str,
    kind: str,
    *,
    confirm: bool = False,
) -> dict:
    """Check whether `value` is safe to store as a custom field config entry.

    Args:
        value:   The user-supplied string (pattern / label / keep_value).
        kind:    One of "regex", "gliner_label", "keep".
        confirm: For kind="keep", must be True to store a literal.

    Returns:
        {"ok": True}                    — safe to store
        {"ok": False, "reason": "..."}  — REFUSE (reason explains what to fix)

    IMPORTANT: the offending value is NEVER included in the reason string.
    """
    if not value or not value.strip():
        return {"ok": False, "reason": "La valeur ne peut pas être vide."}

    # ── STEP 1: run the regex/checksum core on the supplied string ────────────
    # Import here to avoid circular import (pii_guard is imported by mcp which
    # imports engine which imports recognizers).
    try:
        from bubble_shield.recognizers import detect as _detect
        matches = _detect(value)
        checksum_valid = [m for m in matches if m.score >= 1.0]
        if checksum_valid:
            types_found = {m.entity_type for m in checksum_valid}
            if kind == "keep":
                # For keep-list: a checksum-valid IBAN/NIR in the firm identifier
                # is almost certainly wrong (you'd never whitelist a client's IBAN).
                # Refuse even with confirm — this is the hard case.
                financial_types = {"IBAN", "SECU", "NUM_FISCAL", "ISIN", "SIREN", "SIRET"}
                if types_found & financial_types:
                    return {
                        "ok": False,
                        "reason": (
                            "La valeur contient une donnée financière ou d'identité validée "
                            "par checksum. Vous ne devriez jamais ajouter un identifiant "
                            "bancaire ou fiscal client à la liste blanche — "
                            "cela l'exemptrait de l'anonymisation."
                        )
                    }
                # Non-financial checksum (e.g. an email) in a keep-list is fine
                # with confirm — a firm email domain is a legitimate keep entry.
            else:
                # For regex/gliner_label: a checksum-valid PII instance is a REFUSE
                return {
                    "ok": False,
                    "reason": (
                        "La valeur contient une donnée PII réelle validée par checksum "
                        "(IBAN, email, NIR, ISIN, SIREN…). "
                        "Décrivez une CATÉGORIE ou un PATTERN regex "
                        "(ex: \\\\b[A-Z]{2}-\\\\d{5}\\\\b), jamais une valeur réelle."
                    )
                }
    except Exception:
        pass  # guard is best-effort; a crash here should not block the user

    # ── STEP 2: kind-specific checks ─────────────────────────────────────────

    if kind == "regex":
        # A legitimate regex pattern contains metacharacters.
        if not _has_regex_metachar(value) and _looks_like_literal_data(value):
            return {
                "ok": False,
                "reason": (
                    "Le pattern ressemble à une valeur réelle (pas de métacaractères regex). "
                    "Utilisez des métacaractères pour décrire la FORME du champ "
                    "(ex: \\\\d{5}, [A-Z]{2}-\\\\d+, \\\\b\\\\w{8}\\\\b), "
                    "jamais une instance concrète."
                )
            }
        return {"ok": True}

    elif kind == "gliner_label":
        # GLiNER labels must be short, generic, mostly lowercase category phrases.
        words = value.strip().split()
        # Too many words → probably not a category phrase
        if len(words) > 4:
            return {
                "ok": False,
                "reason": (
                    "L'étiquette GLiNER doit être une courte phrase catégorielle "
                    "(≤ 4 mots), pas une description longue. "
                    "Exemple : 'employer name', 'numéro de compte'."
                )
            }
        # No digits in a category label
        if re.search(r'\d', value):
            return {
                "ok": False,
                "reason": (
                    "L'étiquette GLiNER ne doit pas contenir de chiffres — "
                    "elle décrit une CATÉGORIE générique (ex: 'employer name'), "
                    "pas une valeur spécifique."
                )
            }
        # Check each word for proper-noun shape
        for word in words:
            if _looks_like_proper_noun_label(word):
                return {
                    "ok": False,
                    "reason": (
                        "L'étiquette GLiNER semble contenir un nom propre. "
                        "Elle doit décrire une CATÉGORIE générique (ex: 'nom d\\'employeur', "
                        "'numéro client', 'adresse postale'), "
                        "pas un individu ou une valeur spécifique."
                    )
                }
        return {"ok": True}

    elif kind == "keep":
        # The keep-list is the one place a literal IS expected (the firm's own
        # name/domain). But it requires explicit confirmation.
        if not confirm:
            return {
                "ok": False,
                "reason": (
                    "Pour ajouter une valeur littérale à la liste blanche, "
                    "passez confirm=true pour confirmer qu'il s'agit de l'identifiant "
                    "propre de la firme (son nom, domaine e-mail ou téléphone) "
                    "et jamais d'une donnée client."
                )
            }
        return {"ok": True}

    else:
        return {
            "ok": False,
            "reason": f"kind inconnu : {kind!r}. Valeurs valides : regex, gliner_label, keep."
        }
