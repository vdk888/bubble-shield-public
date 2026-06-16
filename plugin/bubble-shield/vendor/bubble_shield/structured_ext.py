"""
structured_ext.py — deterministic recognizers for FR-KYC FORM layouts.

Why this exists (validated 2026-06-01): in real CGP KYC PDFs the data is a
detached form — labels in one block, values in another. Two identifying fields
survive there as strong POSITIONAL patterns that the context-sensitive GLiNER
layer loses in a big chunk, but that a tiny regex nails with high precision:

  1. BIRTHPLACE printed right after a date of birth on the same value line:
       "04/05/1980 LYON (France)"  /  "12/09/1975 BORDEAUX"
     i.e. `DD/MM/YYYY  <City>[ (Country)]`. The city (and any parenthetical
     country) is the identifying birthplace. GLiNER caught these in isolation
     (caught in isolation 0.6-0.9) but dropped mid-document — this recovers them.

  2. MARRIAGE / PACS dates that sit far from their "Date de mariage/PACS" label,
     but appear as a bare `DD/MM/YYYY [LIEU]` on a value line that ISN'T a DOB
     (DOBs are already covered by the core DATE_NAISSANCE recognizer).

These are opt-in `extra_detectors` (same fail-open contract as gliner_ext), so the
core RECOGNIZERS list and the existing test suite are untouched. Recall-first:
they only ADD matches; overlap resolution in the engine lets a more-specific
match win.
"""
from __future__ import annotations

import re
from typing import List

from bubble_shield.recognizers import Match

# DD/MM/YYYY (the FR KYC date format), tolerant of . / - separators.
_DATE = r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}"
# A city token: capitalised word(s), incl. accents, hyphens, spaces (PARIS 09,
# SAINT-DENIS, LE MANS). We stop at a newline or a long run.
_CITY = r"[A-ZÉÈÀÂÎÔÛ][A-ZÉÈÀÂÎÔÛ’'\-]+(?:\s+(?:\d{1,2}|[A-ZÉÈÀÂÎÔÛ][A-ZÉÈÀÂÎÔÛ’'\-]+)){0,3}"

# "DD/MM/YYYY CITY (Country)" → capture the city + optional parenthetical.
_BIRTHPLACE_RE = re.compile(
    rf"{_DATE}\s+(?P<place>{_CITY}(?:\s*\([^)]+\))?)",
)


# Common all-caps headings / boilerplate words that can follow a date but are
# NOT a birthplace. Cheap precision guard (a date before a section heading).
# NB: we deliberately exclude particles like "LE"/"LA" here — they're the start
# of real cities (Le Mans, Le Havre, La Rochelle). The guard matches the FIRST
# token only when it's a standalone heading word, so "LE MANS" is kept.
_NOT_A_PLACE = {
    "AVERTISSEMENT", "ATTENTION", "NOTE", "ANNEXE", "DOCUMENT", "PAGE",
    "ARTICLE", "SIGNATURE", "FAIT", "DATE", "MONTANT", "TOTAL",
    "OBJET", "REFERENCE", "RÉFÉRENCE", "CLIENT", "CONSEILLER",
}


def birthplace_matches(text: str) -> List[Match]:
    """Find birthplaces printed right after a DOB on the same value line."""
    out: List[Match] = []
    for m in _BIRTHPLACE_RE.finditer(text):
        place = m.group("place").strip()
        # Guard against capturing pure noise: require at least one alpha city word.
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", place):
            continue
        # Drop section headings / boilerplate that merely happen to follow a date.
        first = re.split(r"[\s(]", place, 1)[0].upper()
        if first in _NOT_A_PLACE:
            continue
        start = m.start("place")
        out.append(Match(start=start, end=start + len(place),
                         entity_type="LIEU_NAISSANCE", value=place,
                         score=0.85, priority=58))  # > GLiNER, < checksum PII
    return out


# ── Civility + name recognizer (clean NAME source for form layouts) ─────────
#
# Why this exists (2026-06-01 refactor): name-token learning was sourced from the
# greedy core NOM regex, which over-extends across line breaks in the detached
# KYC form layout — a title+name glued to a product line → it learnt "EUROPE"/"LIFINITY"
# as part of the client's name and swept those across the dossier. GLiNER is
# semantically clean but UNDER-detects names in some form layouts. So we add
# a deterministic, PRECISE name source: a civility title followed by 1-3
# capitalised name words ON THE SAME LINE. The single-line constraint ([^\S\n] =
# whitespace but NOT newline) is the whole point — the name is just what sits
# beside the title; the product on the next line is never pulled in.
_TITLE = r"(?:M\.|Mr|Mme|Mlle|Monsieur|Madame|Mademoiselle|Me|Dr)"
_NAMEWORD = r"[A-ZÉÈÀÂÎÔÛ][A-Za-zÀ-ÿ’'\-]+"
_SP = r"[^\S\n]+"   # one-or-more whitespace, NEWLINE EXCLUDED (the form-layout fix)
# Same-line: "M. Jean DUPONT".
_CIVILITY_NAME_RE = re.compile(
    rf"{_TITLE}{_SP}(?P<name>{_NAMEWORD}(?:{_SP}{_NAMEWORD}){{0,2}})"
)
# Detached form layout: a LONE title on its own line, the name on the NEXT line
# (lone title line, name on the next line). The first pass with the
# single-line NOM regex loses these (the title line has no name); this recovers
# them. Only fires when the title is alone on its line (so we don't double-count
# the same-line case).
_CIVILITY_NEXTLINE_RE = re.compile(
    rf"^{_TITLE}[^\S\n]*\n[^\S\n]*(?P<name>{_NAMEWORD}(?:{_SP}{_NAMEWORD}){{0,2}})",
    re.MULTILINE,
)

# Form words that can trail a title but aren't a name (kept tight; the profile
# layer has the broader stoplist).
_NAME_TRAIL_STOP = {"pleine", "propriété", "email", "tél", "tel", "vous", "votre"}


def civility_name_matches(text: str) -> List[Match]:
    """Find 'civility title + name' both same-line ("M. Jean DUPONT") and the
    detached form layout (lone title line, name on the next line). Clean, precise
    NOM source — every name stays single-line so no product/heading is absorbed."""
    out: List[Match] = []
    for rx in (_CIVILITY_NAME_RE, _CIVILITY_NEXTLINE_RE):
        for m in rx.finditer(text):
            name = m.group("name").strip()
            words = name.split()
            # Trim trailing form words the name may have absorbed.
            while words and words[-1].lower() in _NAME_TRAIL_STOP:
                words.pop()
            if not words:
                continue
            name = " ".join(words)
            start = m.start("name")
            out.append(Match(start=start, end=start + len(name),
                             entity_type="NOM", value=name,
                             score=0.9, priority=56))
    return out


def make_structured_detector():
    """Return the combined deterministic form-layout detector callable
    (birthplaces after DOB + civility-prefixed names, both line-safe)."""
    def _detector(text: str) -> List[Match]:
        return birthplace_matches(text) + civility_name_matches(text)
    return _detector
