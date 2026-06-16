"""
context_boost.py — context-word confidence boosting (Presidio-inspired).

A detection that sits NEAR a PII cue word is much more likely to be real. A name
right after "Client :" / "demeurant" / "né le" is almost certainly a person; the
same capitalised words floating next to a form heading ("Senior manager") are
not. So we boost a match's score when a cue word appears within a small window
around it.

Why this matters for Bubble Shield (2026-06-01): the fail-closed verdict flagged lots of
low-confidence GLiNER guesses on form LABELS ("Senior manager", "conseiller") as
"à vérifier", cluttering the result. Boosting lifts the GENUINE low-confidence
detections (a real name near "Client:") over the 0.6 threshold so they're treated
as certain, while leaving the isolated label-guesses low. Net: fewer false
"à vérifier" items, same recall, no new risk (we only ever RAISE scores of things
we already detected — never invent or redact more).

Pure-stdlib, no dependency. Applied as a post-detection step in the engine,
BEFORE the allowlist filter and the residual scan.
"""
from __future__ import annotations

import re
from typing import List

from bubble_shield.recognizers import Match

# FR/finance cue words that signal real client PII nearby. Lower-case; matched
# case-insensitively on a word/loose basis. Kept focused on the KYC vocabulary.
CONTEXT_CUES = (
    "client", "titulaire", "souscripteur", "assuré", "adhérent", "bénéficiaire",
    "demeurant", "domicilié", "résidant", "réside", "adresse",
    "né", "née", "naissance", "nationalité",
    "conjoint", "conjointe", "époux", "épouse", "partenaire", "enfant",
    "monsieur", "madame", "mademoiselle", "nom", "prénom",
    "téléphone", "mobile", "courriel", "e-mail", "email",
    "iban", "compte", "fiscal", "passeport", "pièce d'identité", "sécurité sociale",
    "réalisé pour", "concernant",
)

# Boost amount and default proximity window (characters either side of the span).
_BOOST = float(__import__("os").environ.get("BUBBLE_SHIELD_CONTEXT_BOOST", "0.25"))
_DEFAULT_WINDOW = int(__import__("os").environ.get("BUBBLE_SHIELD_CONTEXT_WINDOW", "40"))

# Pre-compiled cue matcher (word-ish boundaries, accent-friendly).
_CUE_RE = re.compile(
    r"(?<![\wÀ-ÿ])(?:" + "|".join(re.escape(c) for c in CONTEXT_CUES) + r")(?![\wÀ-ÿ])",
    re.IGNORECASE,
)


# Entity types whose confidence comes from a CHECKSUM, not from context. A
# failed-checksum IBAN/ISIN/SIREN is low-score for a REASON (it's probably not a
# real one); proximity to "client" must NOT override that. We only boost the
# soft, context-dependent types (names, addresses, dates, free-form ids).
_NO_BOOST_TYPES = {"IBAN", "ISIN", "SIRET", "SIREN", "SECU"}


def boost_by_context(text: str, matches: List[Match], *,
                     window: int = _DEFAULT_WINDOW,
                     boost: float = _BOOST) -> List[Match]:
    """Return matches with scores boosted where a cue word sits within `window`
    characters of the span. Score is capped at 1.0. Checksum-backed types and
    already-certain (score==1.0) matches are never boosted — context can't make a
    failed checksum valid."""
    if not matches:
        return matches
    out: List[Match] = []
    for m in matches:
        if m.score >= 1.0 or m.entity_type in _NO_BOOST_TYPES:
            out.append(m)
            continue
        lo = max(0, m.start - window)
        hi = min(len(text), m.end + window)
        ctx = text[lo:hi]
        if _CUE_RE.search(ctx):
            new_score = min(1.0, m.score + boost)
            out.append(Match(start=m.start, end=m.end, entity_type=m.entity_type,
                             value=m.value, score=new_score, priority=m.priority))
        else:
            out.append(m)
    return out
