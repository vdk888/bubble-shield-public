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

  3. FR état-civil FORM label-value layouts (fix/257): GLiNER misses PII in
     real DCC-style forms like:
       "Nom : DUBOIS"
       "Prénom : Marc"
       "Né(e) le : 03/05/1980 à : Lyon"
       "Passeport n° 12AB34567"
     This section adds deterministic regex recognizers for:
       - Nom / Prénom / Nom de naissance label lines → NOM
       - Lieu de naissance / "à : <VILLE>" in DOB label line → LIEU_NAISSANCE
       - Pièce d'identité / Passeport / CNI / Titre de séjour n° → PIECE_IDENTITE
     These fire even with the GLiNER daemon DOWN — they're the safety net for forms.

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
_CITY = r"[A-ZÉÈÀÂÎÔÛ][A-ZÉÈÀÂÎÔÛ''\-]+(?:\s+(?:\d{1,2}|[A-ZÉÈÀÂÎÔÛ][A-ZÉÈÀÂÎÔÛ''\-]+)){0,3}"

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
_NAMEWORD = r"[A-ZÉÈÀÂÎÔÛ][A-Za-zÀ-ÿ''\-]+"
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


# ── FR état-civil FORM label-value recognizers (fix/257) ─────────────────────
#
# GLiNER misses PII in real DCC-style forms where state-civil fields appear as
# "LABEL : VALUE" lines. These are deterministic safety nets that fire even with
# the NER daemon DOWN. Recall-first / ADD-only.

# Separator between label and value in a FR form: " : ", " :", ": ", ":" or " "
# (some DCCs use tab/space-only separation)
_SEP = r"\s*:\s*"
# A value token that runs to end-of-line (stripped).
_VALLINE = r"(?P<val>[^\n]+)"

# Placeholder / empty-marker values that must NOT be masked — they signal an
# unfilled or intentionally blank field in a template/form, not real PII.
# Guard applied in every form_*_matches function. Case-insensitive match on
# the full stripped value.
_PLACEHOLDER_VALUES = frozenset({
    "(vide)", "vide", "n/a", "na", "-", "--", "---", "néant", "neant",
    "non renseigné", "non renseignee", "non renseignée", "nr", "?", ".",
    "aucun", "aucune", "inconnu", "inconnue",
})


def _is_placeholder(val: str) -> bool:
    """Return True if val looks like an unfilled/blank template marker."""
    return val.lower().strip(".,; ") in _PLACEHOLDER_VALUES

# ── NOM / PRÉNOM label lines ──────────────────────────────────────────────────
# Matches: "Nom : DUBOIS", "Prénom : Marc", "Nom de naissance : MARTIN",
#          "Nom d'usage : LEROY", "Nom :", "NOM DE NAISSANCE :"
_NOM_LABEL_RE = re.compile(
    r"(?i)^(?:nom\s+(?:de\s+naissance|d['']\s*usage|de\s+famille)?|pr[eé]nom)"
    r"[^\S\n]*:[^\S\n]*" + _VALLINE,
    re.MULTILINE,
)


def form_nom_matches(text: str) -> List[Match]:
    """Match 'Nom : <VALUE>' and 'Prénom : <VALUE>' label lines — NOM entity.

    Guard: placeholder/empty-marker values like '(vide)', 'N/A', 'néant', '-'
    are NOT masked — they signal unfilled template fields, not real PII.
    """
    out: List[Match] = []
    for m in _NOM_LABEL_RE.finditer(text):
        val = m.group("val").strip()
        if not val or len(val) > 80:
            continue
        # Guard: skip placeholder / empty-marker values (template slots)
        if _is_placeholder(val):
            continue
        # Guard: value must contain at least one letter (not a blank line)
        if not re.search(r"[A-Za-zÀ-ÿ]", val):
            continue
        start = m.start("val")
        out.append(Match(start=start, end=start + len(val),
                         entity_type="NOM", value=val,
                         score=0.88, priority=57))
    return out


# ── LIEU DE NAISSANCE label lines ─────────────────────────────────────────────
# Matches: "Lieu de naissance : Lyon", "Lieu de naissance : BORDEAUX (France)"
# Also: "Né(e) le : 03/05/1980 à : Lyon"  (the "à : CITY" part on the SAME line)
_LIEU_LABEL_RE = re.compile(
    r"(?i)lieu\s+de\s+naissance[^\S\n]*:[^\S\n]*" + _VALLINE,
    re.MULTILINE,
)
# "Né(e) le : <DATE> à : <CITY>" — captures the CITY after "à :"
# Also handles "Né(e) le : <DATE> à <CITY>" (no colon after "à")
_NEE_A_CITY_RE = re.compile(
    r"(?i)n[eé]\s*\(?\s*e\s*\)?\s*le\s*:[^\n]*?\bà\s*:?\s*(?P<val>[A-ZÉÈÀÂÎÔÛa-zéèàâîôû][^\n,;]*)",
    re.MULTILINE,
)


def form_lieu_naissance_matches(text: str) -> List[Match]:
    """Match explicit 'Lieu de naissance : <VALUE>' and 'à : <CITY>' in DOB lines.

    Guard: placeholder/empty-marker values like '(vide)', 'N/A', 'néant', '-'
    are NOT masked — they signal unfilled template fields, not real PII.
    """
    out: List[Match] = []
    for m in _LIEU_LABEL_RE.finditer(text):
        val = m.group("val").strip()
        if not val or len(val) > 80:
            continue
        if _is_placeholder(val):
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", val):
            continue
        start = m.start("val")
        out.append(Match(start=start, end=start + len(val),
                         entity_type="LIEU_NAISSANCE", value=val,
                         score=0.88, priority=57))
    for m in _NEE_A_CITY_RE.finditer(text):
        val = m.group("val").strip().rstrip(",;")
        if not val or len(val) > 80:
            continue
        if _is_placeholder(val):
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", val):
            continue
        start = m.start("val")
        out.append(Match(start=start, end=start + len(val),
                         entity_type="LIEU_NAISSANCE", value=val,
                         score=0.88, priority=57))
    return out


# ── PIÈCE D'IDENTITÉ label lines ──────────────────────────────────────────────
# Matches label+value for ID documents:
#   "Passeport n° 12AB34567"
#   "CNI n° 1234567890123"
#   "Pièce d'identité : <ID>"
#   "Titre de séjour n° <ID>"
#   "N° passeport : 12AB34567"
# FR Passport: 2 digits + 2 letters + 5 digits  (e.g. 12AB34567, 05CD78901)
# CNI (carte nationale d'identité): 12-digit string (new) or old 9-char
# Generic: alphanumeric runs 6–20 chars that look like an ID number

_ID_VALUE = r"(?P<val>[A-Z0-9][A-Z0-9\s]{4,24})"

_PIECE_LABEL_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"(?:pi[eè]ce\s+d['']\s*identit[eé]|passeport|carte\s+(?:nationale\s+d['']\s*identit[eé]|d['']\s*identit[eé])|CNI|titre\s+de\s+s[eé]jour)"
    r"[^\S\n]*(?:n[o°]\s*|num[eé]ro\s*)?[:\s]+(?P<val>[A-Z0-9][A-Z0-9\s\-]{4,29})"
    r"|"
    r"n[o°]\s*(?:passeport|CNI|pièce|pi[eè]ce|carte)[^\S\n]*[:\s]+(?P<val2>[A-Z0-9][A-Z0-9\s\-]{4,29})"
    r")",
    re.MULTILINE,
)

# Also match bare "Passeport n° VALUE" / "N° pièce : VALUE" where the ID-number
# is on the same line after the keyword+n°.
_ID_INLINE_RE = re.compile(
    r"(?i)"
    r"(?:passeport|CNI|pi[eè]ce\s+d['']\s*identit[eé]|carte\s+(?:nationale\s+d['']\s*identit[eé]|d['']\s*identit[eé])|titre\s+de\s+s[eé]jour)"
    r"[^\S\n]*n[o°°]?[^\S\n]*:?[^\S\n]*"
    r"(?P<val>[A-Z0-9][A-Z0-9\s\-]{4,29})",
    re.MULTILINE,
)


def form_piece_identite_matches(text: str) -> List[Match]:
    """Match ID-document label lines — PIECE_IDENTITE entity.

    Patterns: 'Passeport n° <ID>', 'CNI n° <ID>', 'Pièce d'identité : <ID>',
    'Titre de séjour n° <ID>', 'N° pièce : <ID>'. Also handles bare
    ID numbers following those keywords inline. Never matches prose sentences
    (values are bounded to 30 chars and must start with an uppercase letter or digit).
    """
    out: List[Match] = []
    seen: set = set()

    def _emit(val: str, start: int) -> None:
        val = val.strip().rstrip(".,;")
        if not val or len(val) < 5:
            return
        if not re.search(r"[A-Z0-9]", val):
            return
        span = (start, start + len(val))
        if span in seen:
            return
        seen.add(span)
        out.append(Match(start=start, end=start + len(val),
                         entity_type="PIECE_IDENTITE", value=val,
                         score=0.90, priority=59))

    for m in _PIECE_LABEL_RE.finditer(text):
        val = m.group("val") or m.group("val2")
        if val:
            _emit(val, m.start("val") if m.group("val") else m.start("val2"))
    for m in _ID_INLINE_RE.finditer(text):
        val = m.group("val")
        if val:
            _emit(val, m.start("val"))
    return out


# ── DÉNOMINATION / RAISON SOCIALE label lines (fix #259) ─────────────────────
#
# Corporate form lines like:
#   "Dénomination ou raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI"
#   "Raison sociale : SCI DU HAMEAU"
#   "Dénomination sociale : SAS INNOVATION LABS"
#
# For professional-practice SELARLs / SCMs / SCPs, the company name EMBEDS the
# practitioner's personal name (e.g. "SELARL DU DOCTEUR JEAN MARTIN"), so the
# company name is PII. We mask the whole VALUE (everything after the colon), NOT
# the forme-juridique TYPE word ("SELARL", "SAS", "SCI") when it stands alone
# without a label — only the company name on a labeled line is at risk here.
#
# Precision guards:
#   - Requires an explicit "dénomination" or "raison sociale" label + colon.
#   - Placeholder/empty-marker values are skipped (same guard as other form recogs).
#   - Value must contain at least one letter (not a blank line).
#   - Value capped at 120 chars (company names ≤ ~100 chars in practice).
#   - "Forme juridique : SELARL" (type-only, no name) → NOT matched here because
#     neither "dénomination" nor "raison sociale" appears as the label.
_RAISON_SOCIALE_LABEL_RE = re.compile(
    r"(?i)"
    r"^(?:"
    r"d[eé]nomination(?:\s+sociale)?(?:\s+ou\s+raison\s+sociale)?"
    r"|raison\s+sociale"
    r")"
    r"[^\S\n]*:[^\S\n]*" + _VALLINE,
    re.MULTILINE,
)


def form_raison_sociale_matches(text: str) -> List[Match]:
    """Match 'Dénomination (ou) (sociale)? : <VALUE>' and 'Raison sociale : <VALUE>'
    label lines — RAISON_SOCIALE entity (fix #259).

    Masks the whole company name value, including any embedded practitioner names
    (SELARL DU DOCTEUR …). Does NOT mask standalone forme-juridique type words
    ("Forme juridique : SELARL") — only labeled dénomination/raison-sociale lines.

    Guard: placeholder/empty-marker values and values with no letters are skipped.
    """
    out: List[Match] = []
    for m in _RAISON_SOCIALE_LABEL_RE.finditer(text):
        val = m.group("val").strip()
        if not val or len(val) > 120:
            continue
        # Guard: skip placeholder / empty-marker values (template slots)
        if _is_placeholder(val):
            continue
        # Guard: value must contain at least one letter (not a blank line)
        if not re.search(r"[A-Za-zÀ-ÿ]", val):
            continue
        start = m.start("val")
        out.append(Match(start=start, end=start + len(val),
                         entity_type="RAISON_SOCIALE", value=val,
                         score=0.90, priority=59))
    return out


def make_structured_detector():
    """Return the combined deterministic form-layout detector callable.

    Covers (fail-open, ADD-only, daemon-independent):
      - birthplaces after DOB on inline value lines
      - civility-prefixed names (same-line and detached-layout)
      - FR état-civil FORM label-value lines:
          Nom/Prénom → NOM  (placeholder guard: (vide)/N/A/néant etc. skipped)
          Lieu de naissance / "à : CITY" in DOB lines → LIEU_NAISSANCE
          Passeport/CNI/Pièce d'identité n° → PIECE_IDENTITE
          Dénomination/Raison sociale : → RAISON_SOCIALE  (fix #259)
      - Note: "Né(e) le : DD/MM/YYYY" DOB masking is handled by the core
        DATE_NAISSANCE recognizer in recognizers.py (fix #257-b), which now
        matches the parenthetical form Né(e) le in addition to née/né/nee.
      - Note: Full 14-digit SIRET (including hyphen-separated NIC suffix) masking
        is handled by the core SIRET recognizer in recognizers.py (fix #259).
    """
    def _detector(text: str) -> List[Match]:
        matches: List[Match] = []
        try:
            matches += birthplace_matches(text)
        except Exception:
            pass
        try:
            matches += civility_name_matches(text)
        except Exception:
            pass
        try:
            matches += form_nom_matches(text)
        except Exception:
            pass
        try:
            matches += form_lieu_naissance_matches(text)
        except Exception:
            pass
        try:
            matches += form_piece_identite_matches(text)
        except Exception:
            pass
        try:
            matches += form_raison_sociale_matches(text)
        except Exception:
            pass
        return matches
    return _detector
