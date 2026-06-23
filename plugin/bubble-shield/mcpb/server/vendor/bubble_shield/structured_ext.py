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


# ── DÉNOMINATION / RAISON SOCIALE label lines (fix #259, extended fix #264) ───
#
# Corporate form lines like:
#   "Dénomination ou raison sociale : SELARL DU DOCTEUR FAKENAME TESTONI"
#   "Dénomination de l'entreprise : SELARL DU DOCTEUR FAKENAME TESTONI"
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
    r"d[eé]nomination(?:\s+(?:sociale|de\s+l['']\s*entreprise))?(?:\s+ou\s+raison\s+sociale)?"
    r"|raison\s+sociale"
    r")"
    r"[^\S\n]*:[^\S\n]*" + _VALLINE,
    re.MULTILINE,
)


def form_raison_sociale_matches(text: str) -> List[Match]:
    """Match 'Dénomination (ou) (sociale|de l'entreprise)? : <VALUE>' and
    'Raison sociale : <VALUE>' label lines — RAISON_SOCIALE entity (fix #259, #264).

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
        # Bug B fix: normalise doubled-prefix artifact so vault keys are consistent.
        # "SELARL SELARL DU DOCTEUR X" → "SELARL DU DOCTEUR X" (same canonical form
        # as the clean "Raison sociale : SELARL DU DOCTEUR X" occurrence) so that
        # vault.token_for() collapses both to ONE token.
        canonical_val = _canonical_company_name(val)
        start = m.start("val")
        out.append(Match(start=start, end=start + len(val),
                         entity_type="RAISON_SOCIALE", value=canonical_val,
                         score=0.90, priority=59))
    return out


# ── FORME-JURIDIQUE-ANCHORED recognizer (fix #264) ───────────────────────────
#
# In liasse fiscale PDFs, the company name appears UNLABELED — as page headers,
# table headers, repeated footer lines — with the forme-juridique type word
# leading directly into the company name WITHOUT a "raison sociale :" label:
#
#   "SELARL DU DOCTEUR FAKENAME TESTONI"        (standalone header)
#   "SELARL SELARL DU DOCTEUR FAKENAME TESTONI" (doubled prefix artifact)
#   "EXPERTISE SELARL SELARL DU DOCTEUR FAKENAME TESTONI"  (prefixed)
#   "01012025 31122025 SELARL SELARL DU DOCTEUR FAKENAME TESTONI"
#
# The doubled-prefix pattern "SELARL SELARL …" is a PDF extraction artifact
# from some liasse layouts where the type word appears in two adjacent blocks.
#
# Precision guards (CRITICAL — false positives are the main risk here):
#   - Type word MUST be followed by at least one personal-name token (≥2 letters,
#     not a generic stopword). Bare "SELARL" / "La SELARL exerce…" → NOT matched.
#   - "Forme juridique : SELARL" or "Type : SAS" → NOT matched because the
#     forme-juridique-anchored pattern requires actual company-name words after
#     the type, and these label lines don't have them.
#   - The name captured is the words following the (possibly doubled) type word —
#     NOT the type word itself. We use the canonical (de-duplicated) form for the
#     vault key, but always emit ALL matched characters for correct span masking.
#   - Company name must be 2–12 CAPS words (liasse headers, not prose sentences).
#   - We don't fire on lines where SELARL/SAS/… appears mid-sentence with a
#     lowercase word nearby (that's prose, not a company name header).

# Forme-juridique type words (full list of common FR professional structures)
_FORME_JURIDIQUE = (
    r"SELARL|SARL|SASU|SAS|SCI|SCM|SCP|SA|SNC|EURL|SCOP|SELAFA|SELARLU|SELAS"
)

# Python set of the same words — used to guard against degenerate seeds that
# canonicalise to a bare type word (Bug A fix: "Raison sociale : SARL" must
# NOT cause the repetition pass to mask every bare "SARL" in the document).
_FORME_JURIDIQUE_SET: frozenset = frozenset({
    "SELARL", "SARL", "SASU", "SAS", "SCI", "SCM", "SCP", "SA", "SNC",
    "EURL", "SCOP", "SELAFA", "SELARLU", "SELAS",
})

# A CAPS name word: uppercase-only, may include apostrophe, hyphen, accented CAPS.
# Must be ≥2 chars and NOT a pure-number token.
_CAPS_WORD = r"[A-ZÉÈÀÂÎÔÙÛÜ][A-ZÉÈÀÂÎÔÙÛÜ''\-]{1,}"

# Words that alone (with nothing following) do NOT constitute a company name.
# These are stopwords that can START a name segment (e.g. "SELARL DU DOCTEUR X")
# but only if followed by a genuine CAPS personal-name word. The guard below
# checks that after optional article/preposition tokens there is at least one
# CAPS word that is NOT itself a pure stopword.
_FORME_JURI_NAME_STOPONLY = frozenset({
    # Pure prepositions / articles — alone are NOT a name
    "DE", "DES", "ET", "EN", "AU", "AUX",
    # Generic organisational nouns that alone don't identify a practice
    # (e.g. "SELARL CABINET" is not PII — needs a name after it)
    "CABINET", "ETUDE", "ETUDES", "GROUPE", "GROUPEMENT",
    "ASSOCIATION", "SOCIETE",
})

# The forme-juridique-anchored pattern:
#   optional leading garbage (digits/uppercase words before the type)
#   then: (TYPE_WORD)+ (one or two occurrences to handle doubling)
#   then: 1–12 CAPS words constituting the company name
#
# We capture only what follows the (normalised) type word(s) as the "name" group,
# because that's the PII payload. The full match (including the type prefix) is
# what we report so the vault token covers the whole span.
#
# Pattern notes:
#   (?:SELARL\s+)? — optional doubled prefix (SELARL SELARL …)
#   (?P<type>…)    — the authoritative type word (used for normalisation)
#   \s+            — at least one space between type and name
#   (?P<name>…)    — the company name words (CAPS, 1–12 tokens)
_FORME_JURI_RE = re.compile(
    rf"(?<!\w)(?:{_FORME_JURIDIQUE})\s+(?P<type>(?:{_FORME_JURIDIQUE})\s+)?(?P<name>(?:{_CAPS_WORD})(?:\s+(?:{_CAPS_WORD}|DU|DES|DE|LA|LE|LES|DU|ET){{0,}}(?:{_CAPS_WORD})){{0,11}})",
    re.UNICODE,
)

# Simplified, direct pattern: TYPE (optionally doubled) then NAME words
# We rebuild this more carefully:
_FORME_JURI_RE = re.compile(
    # optional doubled type (e.g. SELARL SELARL)
    rf"(?<!\w)(?:{_FORME_JURIDIQUE})(?:\s+(?:{_FORME_JURIDIQUE}))?\s+(?P<name>(?:{_CAPS_WORD})"
    # allow interleaved prepositions (DU, DE, DES, etc.) + more CAPS words, 0-11 more tokens
    rf"(?:\s+(?:DU|DES|DE|LA|LE|LES|ET|DU|AU|AUX|D['']\s*|L['']\s*){{0,1}}(?:{_CAPS_WORD})){{0,11}})",
    re.UNICODE,
)


def forme_juridique_anchored_matches(text: str) -> List[Match]:
    """Find unlabeled company names anchored by their forme-juridique type word.

    Catches patterns like:
      "SELARL DU DOCTEUR FAKENAME TESTONI"
      "SELARL SELARL DU DOCTEUR FAKENAME TESTONI"  (doubled prefix artifact)
      "EXPERTISE SELARL DU DOCTEUR FAKENAME TESTONI" (leading context words OK)

    Emits the FULL match span (type word + name) as RAISON_SOCIALE so the vault
    can produce a consistent token with the labeled-form matches.

    Precision guards:
      - Name group must contain at least one CAPS word that is NOT a pure stopword
        (prevents "SELARL DE" or "SCI ET" bare-preposition-only matches).
      - "Forme juridique : SELARL" / "Type : SAS" → not matched because the label
        line has "forme juridique" / "type" immediately before the ":" and our
        line-prefix check skips those.
      - First non-preposition token must NOT be a pure-digit string (dates/years).
      - Does NOT match when SELARL/SAS appears mid-lowercase-prose (ALL-CAPS
        name continuation required by the pattern).
    """
    out: List[Match] = []
    for m in _FORME_JURI_RE.finditer(text):
        name = m.group("name").strip()
        if not name:
            continue
        # Collect the real name tokens (skip leading prepositions/articles)
        tokens = re.split(r"\s+", name)
        real_tokens = [t for t in tokens if t.upper() not in _FORME_JURI_NAME_STOPONLY]
        # Must have at least one real CAPS name word (not a pure-digit, not stopword)
        if not real_tokens:
            continue
        # First real token must not be pure digits (fiscal year / date marker)
        if re.match(r"^\d+$", real_tokens[0]):
            continue
        # First real token must be ≥2 letters (actual name word, not single-char)
        if not re.search(r"[A-ZÉÈÀÂÎÔÙÛÜ]{2,}", real_tokens[0]):
            continue
        # Precision: skip "Forme juridique : SELARL …" / "Type : SAS …" lines
        # by checking the line prefix before the match for those label patterns.
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_prefix = text[line_start:m.start()].rstrip()
        if re.search(
            r"(?i)(?:forme\s+juridique|type\s+de\s+soci[eé]t[eé]|nature\s+juridique|type)\s*:",
            line_prefix,
        ):
            continue
        # Emit the WHOLE match span (type word + name) as RAISON_SOCIALE.
        # Bug B fix: normalise the value to canonical form so the vault always
        # keys on the same string regardless of which occurrence (doubled-prefix
        # or clean) was found first.
        start = m.start()
        end = m.end()
        full_val = _canonical_company_name(text[start:end].strip())
        out.append(Match(start=start, end=end,
                         entity_type="RAISON_SOCIALE", value=full_val,
                         score=0.82, priority=57))  # lower than labeled (0.90)
    return out


# ── DOC-LEVEL COMPANY-NAME REPETITION POST-PASS (fix #264) ───────────────────
#
# Once we know the canonical company name from ANY detection (labeled or
# forme-juridique-anchored), we scan the full document for every verbatim
# occurrence of that string and emit a RAISON_SOCIALE match for each one.
#
# This is the definitive fix for the liasse fiscale 14× leak: the name appears
# as unlabeled page/table headers and there is no label to anchor on, but the
# string is identical to the labeled occurrence found earlier.
#
# Contract:
#   - ADD-only (never removes existing matches)
#   - Vault emits consistent tokens across all occurrences (same value → same token)
#   - Works with or without the forme-juridique-anchored recognizer (belt+suspenders)
#   - Only emits spans that do NOT already overlap an existing match (dedup)

def _canonical_company_name(val: str) -> str:
    """Normalise a company name found by any recognizer to its canonical form.

    Handles the doubled-prefix extraction artifact:
      "SELARL SELARL DU DOCTEUR X"  →  "SELARL DU DOCTEUR X"
    The canonical form is the shorter one (with the duplicate type prefix removed),
    so the vault key is consistent regardless of which occurrence was found first.
    """
    val = val.strip()
    # Detect doubled prefix: "TYPE TYPE ..." → "TYPE ..."
    doubled = re.match(
        rf"^({_FORME_JURIDIQUE})\s+\1\s+",
        val,
        re.UNICODE,
    )
    if doubled:
        val = val[doubled.end() - (len(doubled.group(1)) + 1):]
    return val.strip()


def doc_level_repetition_matches(text: str, found: List[Match]) -> List[Match]:
    """Find all verbatim repetitions of company names already identified in `found`.

    For each unique RAISON_SOCIALE value (canonical form), scan the full document
    for every occurrence and emit a Match for each that isn't already covered by
    an existing match in `found`.

    This is the core fix for the liasse fiscale 14× leak (#264): the labeled
    "Dénomination" line gives us the name once; this pass finds the other 13.
    """
    if not found:
        return []

    # Collect unique canonical names from RAISON_SOCIALE detections
    canonical_names: set = set()
    for m in found:
        if m.entity_type == "RAISON_SOCIALE":
            c = _canonical_company_name(m.value)
            if c and len(c) >= 4:   # sanity: skip ultra-short strings
                # Bug A fix: skip seeds that are a bare forme-juridique type word.
                # If "Raison sociale : SARL" yields canonical "SARL", using that as
                # a seed would mask every bare "SARL" in the document — simultaneous
                # over-mask (type labels, prose) AND under-mask (SARL DUPONT → only
                # "SARL" masked, "DUPONT" leaks). A bare type word is never PII by
                # itself; only the full company name is.
                if c.upper() in _FORME_JURIDIQUE_SET:
                    continue
                canonical_names.add(c)
            # Also register the original (non-canonical) form in case it's different —
            # but only if it is not itself a bare type word.
            orig = m.value.strip()
            if orig and orig != c and orig.upper() not in _FORME_JURIDIQUE_SET:
                canonical_names.add(orig)

    if not canonical_names:
        return []

    # Build a set of already-covered spans to avoid double-emitting
    covered: set = {(m.start, m.end) for m in found}

    extra: List[Match] = []
    for name in canonical_names:
        if not name:
            continue
        # Escape for literal search
        pattern = re.compile(re.escape(name), re.UNICODE)
        for occ in pattern.finditer(text):
            span = (occ.start(), occ.end())
            if span in covered:
                continue
            # Don't emit if fully inside an already-covered span
            if any(cs <= occ.start() and occ.end() <= ce for (cs, ce) in covered):
                continue
            covered.add(span)
            extra.append(Match(
                start=occ.start(), end=occ.end(),
                entity_type="RAISON_SOCIALE", value=name,
                score=0.88, priority=59,
            ))

    return extra


def make_structured_detector():
    """Return the combined deterministic form-layout detector callable.

    Covers (fail-open, ADD-only, daemon-independent):
      - birthplaces after DOB on inline value lines
      - civility-prefixed names (same-line and detached-layout)
      - FR état-civil FORM label-value lines:
          Nom/Prénom → NOM  (placeholder guard: (vide)/N/A/néant etc. skipped)
          Lieu de naissance / "à : CITY" in DOB lines → LIEU_NAISSANCE
          Passeport/CNI/Pièce d'identité n° → PIECE_IDENTITE
          Dénomination/Raison sociale / Dénomination de l'entreprise :
            → RAISON_SOCIALE  (fix #259, #264)
      - Forme-juridique-anchored recognizer: "(SELARL|SARL|SAS|…) <CAPS NAME>"
          → RAISON_SOCIALE even without a label (fix #264 — liasse fiscale headers)
          Handles doubled-prefix artifact "SELARL SELARL DU DOCTEUR …".
          Precision guards: bare type words / form-label lines / prose → NOT matched.
      - Doc-level repetition post-pass (fix #264): once a RAISON_SOCIALE is
          identified (from ANY source), scan the whole document for verbatim
          repetitions and emit a match for each unlabeled occurrence.
      - Note: "Né(e) le : DD/MM/YYYY" DOB masking is handled by the core
        DATE_NAISSANCE recognizer in recognizers.py (fix #257-b).
      - Note: Full 14-digit SIRET masking is in the core SIRET recognizer (#259).
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
        # ── fix #264: forme-juridique-anchored unlabeled headers ─────────────
        try:
            matches += forme_juridique_anchored_matches(text)
        except Exception:
            pass
        # ── fix #264: doc-level repetition post-pass ─────────────────────────
        # Must run AFTER all primary detectors so it has the full set of
        # RAISON_SOCIALE matches to derive canonical names from.
        try:
            matches += doc_level_repetition_matches(text, matches)
        except Exception:
            pass
        # ── fix #266: signataire / gérant / déclarant labeled blocks ─────────
        try:
            matches += signataire_matches(text)
        except Exception:
            pass
        # ── fix #266: doc-level person-name repetition post-pass ─────────────
        # Must run LAST — after all primary detectors AND the #264 repetition
        # pass so it has full RAISON_SOCIALE + NOM context to derive seeds from.
        try:
            matches += doc_level_person_repetition_matches(text, matches)
        except Exception:
            pass
        return matches
    return _detector


# ── PERSON-NAME EXTRACTION FROM RAISON SOCIALE (fix #266) ───────────────────
_RAISON_SOCIALE_PREFIXES: frozenset = frozenset({
    "SELARL", "SARL", "SASU", "SAS", "SCI", "SCM", "SCP", "SA", "SNC",
    "EURL", "SCOP", "SELAFA", "SELARLU", "SELAS",
    "DU", "DE", "DES", "ET", "EN", "AU", "AUX", "LA", "LE", "LES", "D", "L",
    "DOCTEUR", "DR", "MAITRE", "ME", "PROFESSEUR", "PR",
    "CABINET", "ETUDE", "ETUDES", "GROUPE", "GROUPEMENT",
    "ASSOCIATION", "SOCIETE", "CENTRE", "CLINIQUE", "MEDICAL", "MEDICALE",
})

_COMMON_FRENCH_SURNAMES: frozenset = frozenset({
    "MARTIN", "BERNARD", "THOMAS", "PETIT", "ROBERT", "RICHARD",
    "DURAND", "DUBOIS", "MOREAU", "LAURENT", "SIMON", "MICHEL",
    "LEFEBVRE", "LEFEVRE", "LEROY", "ROUX", "DAVID", "BERTRAND",
    "MOREL", "FOURNIER", "GIRARD", "BONNET", "DUPONT", "LAMBERT",
    "FONTAINE", "ROUSSEAU", "VINCENT", "MULLER", "LECOMTE", "BLANC",
    "GRAY", "GRIS", "LEGRAND", "GRAND", "BRUN", "LEBRUN", "LENOIR",
    "ROI", "PAGE", "SAGE", "LION", "ROSE", "VIOLET", "BOIS",
    "PIERRE", "PAUL", "ANDRE", "CLAUDE", "MARIE", "ANNE", "JEAN",
    "NICOLAS", "MARC", "LUC", "PASCAL", "ERIC", "ALAIN",
    "NORD", "SUD", "EST", "OUEST", "FRANCE", "PARIS",
    "NOIR", "ROUGE", "VERT", "BLEU",
})

_PERSON_TOKEN_MIN_LEN = 4


def extract_person_name_from_raison_sociale(company_name: str) -> List[str]:
    """Extract personal name tokens from a company name (fix #266).

    Given "SELARL DU DOCTEUR FAKENAME TESTONI", return ["FAKENAME", "TESTONI"].
    Strips forme-juridique prefix + honorific words; returns residual CAPS tokens.
    """
    tokens = re.split(r"[\s''\-]+", company_name.strip().upper())
    while tokens and tokens[0] in _RAISON_SOCIALE_PREFIXES:
        tokens.pop(0)
    tokens = [t for t in tokens if len(t) >= 2 and re.search(r"[A-Z\xc9\xc8\xc0\xc2\xce\xd4\xd9\xdb\xdc]", t)]
    return tokens


def _person_name_seeds(tokens: List[str]) -> List[str]:
    """Return seed strings for the doc-level person-name repetition pass (fix #266).

    Includes the full PAIR (both orderings) when >=2 tokens.
    Includes a lone token only if it is NOT a common word and has length >= _PERSON_TOKEN_MIN_LEN.
    """
    seeds: List[str] = []
    if not tokens:
        return seeds
    if len(tokens) >= 2:
        pair_ab = " ".join(tokens[:2])
        pair_ba = " ".join(reversed(tokens[:2]))
        seeds.append(pair_ab)
        if pair_ba != pair_ab:
            seeds.append(pair_ba)
    for tok in tokens:
        if (len(tok) >= _PERSON_TOKEN_MIN_LEN
                and tok.upper() not in _COMMON_FRENCH_SURNAMES
                and tok.upper() not in _RAISON_SOCIALE_PREFIXES
                and tok.upper() not in _FORME_JURIDIQUE_SET):
            seeds.append(tok)
    return seeds


_SIGNATAIRE_LABEL_RE = re.compile(
    r"(?i)"
    r"^(?:"
    r"signataire"
    r"|g[e\xe9]rant"
    r"|co\s*g[e\xe9]rant"
    r"|d[e\xe9]clarant"
    r"|repr[e\xe9]sentant\s+l[e\xe9]gal"
    r"|repr[e\xe9]sentant"
    r"|nom\s*(?:\([^)]*\))?\s*du\s*(?:signataire|d[e\xe9]clarant|g[e\xe9]rant|repr[e\xe9]sentant)"
    r"|qualit[e\xe9]\s+du\s+(?:signataire|d[e\xe9]clarant)"
    r"|mandataire\s+social"
    r"|pr[e\xe9]sident"
    r"|directeur\s+g[e\xe9]n[e\xe9]ral"
    r")"
    r"(?:[^\S\n]*/[^\S\n]*(?:signataire|d[e\xe9]clarant|g[e\xe9]rant))*"
    r"[^\S\n]*:[^\S\n]*(?P<val>[^\n]+)",
    re.MULTILINE,
)

_ROLE_WORDS: frozenset = frozenset({
    "GÉRANT", "GERANT", "PRÉSIDENT", "PRESIDENT", "DIRECTEUR",
    "SIGNATAIRE", "DÉCLARANT", "DECLARANT", "REPRÉSENTANT", "REPRESENTANT",
    "MANDATAIRE", "ASSOCIÉ", "ASSOCIE", "COGÉRANT", "COGERANT",
    "ADMINISTRATEUR", "LIQUIDATEUR", "DOCTEUR", "DR", "MAITRE", "ME",
    "PROFESSEUR", "PR",
})


def _strip_leading_role(val: str) -> str:
    """Strip leading role/position word from a signataire value (fix #266)."""
    words = val.split()
    if words and words[0].upper() in _ROLE_WORDS:
        remainder = " ".join(words[1:]).strip()
        if remainder:
            return remainder
    return val


def signataire_matches(text: str) -> List[Match]:
    """Match Signataire/Gerant/Declarant: <NAME> and similar role labels -> NOM (fix #266).

    Precision guards: placeholder values skipped; value capped at 80 chars;
    value must contain at least one letter. Leading role words are stripped.
    """
    out: List[Match] = []
    for m in _SIGNATAIRE_LABEL_RE.finditer(text):
        raw_val = m.group("val").strip()
        if not raw_val or len(raw_val) > 80:
            continue
        if _is_placeholder(raw_val):
            continue
        if not re.search(r"[A-Za-z\xc0-\xff]", raw_val):
            continue
        val = _strip_leading_role(raw_val).strip()
        if not val:
            val = raw_val
        start = m.start("val")
        out.append(Match(start=start, end=start + len(raw_val),
                         entity_type="NOM", value=val,
                         score=0.90, priority=58))
    return out


def doc_level_person_repetition_matches(text: str, found: List[Match]) -> List[Match]:
    """Find all verbatim repetitions of the practitioner's personal name (fix #266).

    Derives person name from RAISON_SOCIALE matches (strips company prefix) and
    from high-confidence NOM matches with >=2 CAPS tokens. Emits NOM for each
    uncovered occurrence. ADD-only, fail-open, vault-consistent.

    fix #273 — glued-token left-boundary:
    PDF extraction artifacts can GLUE a seed to the preceding token with no space,
    e.g. "g\u00e9rantETESTONI" where "E" is the trailing char of the preceding word
    and "TESTONI" is the known surname. The standard word-boundary check
    ``(?<![A-Za-z])`` fails because "E" IS a letter.

    For seeds derived from RAISON_SOCIALE (known, high-confidence), we use a
    LOOSE left boundary — no left-char restriction — so the seed is found even
    when glued. The right boundary (``(?![A-Za-z])``) stays strict to prevent
    matching inside longer words (e.g. "TESTONIAN"). Only applies to lone-token
    seeds (length >= 6) from RAISON_SOCIALE derivation; pair seeds and NOM-derived
    seeds keep the standard boundary (they are inherently longer / multi-token and
    the glue artifact only affects lone tokens).

    Precision guard: lone-token seeds shorter than 6 chars or in
    _COMMON_FRENCH_SURNAMES are already excluded by _person_name_seeds(), so the
    loose boundary never fires for "MARTIN", "PETIT", etc.
    """
    if not found:
        return []

    # Track which seeds came from RAISON_SOCIALE derivation (fix #273: loose left
    # boundary for PDF-glued lone tokens).
    seeds: set = set()
    raison_sociale_lone_seeds: set = set()   # lone tokens only (no space in seed)

    for m in found:
        if m.entity_type == "RAISON_SOCIALE":
            c = _canonical_company_name(m.value)
            tokens = extract_person_name_from_raison_sociale(c)
            if not tokens:
                continue
            for seed in _person_name_seeds(tokens):
                seeds.add(seed)
                # A lone token seed has no space and comes from a known company name
                # -> eligible for the glued-token loose-left-boundary pass (#273).
                if " " not in seed and len(seed) >= 6:
                    raison_sociale_lone_seeds.add(seed)

    for m in found:
        if m.entity_type == "NOM":
            val = m.value.strip().upper()
            toks = re.split(r"\s+", val)
            toks = [t for t in toks if re.search(r"[A-Z\xc9\xc8\xc0\xc2\xce\xd4\xd9\xdb\xdc]{2,}", t)]
            if len(toks) >= 2:
                for seed in _person_name_seeds(toks):
                    seeds.add(seed)

    if not seeds:
        return []

    covered: set = {(m.start, m.end) for m in found}

    # Process seeds longest-first so pair matches (e.g. "TESTONI FAKENAME")
    # are added before lone-token seeds (e.g. "FAKENAME"), and lone tokens
    # that fall inside an already-covered pair span are skipped.
    sorted_seeds = sorted(seeds, key=len, reverse=True)

    extra: List[Match] = []
    for seed in sorted_seeds:
        if not seed or len(seed) < _PERSON_TOKEN_MIN_LEN:
            continue

        # Standard pattern: strict word boundary on both sides.
        pattern = re.compile(
            r"(?<![A-Za-z\xc0-\xff])"
            + re.escape(seed)
            + r"(?![A-Za-z\xc0-\xff])",
            re.UNICODE,
        )
        # fix #273 -- loose-left-boundary pattern for RAISON_SOCIALE lone-token
        # seeds: the seed may be immediately preceded by any character (including a
        # letter from a PDF-extraction artifact), but the RIGHT boundary is still
        # strict so we never match inside a longer word.
        glued_pattern = None
        if seed in raison_sociale_lone_seeds:
            glued_pattern = re.compile(
                re.escape(seed)
                + r"(?![A-Za-z\xc0-\xff])",
                re.UNICODE,
            )

        def _collect(occ, _covered=covered, _extra=extra):
            span = (occ.start(), occ.end())
            if span in _covered:
                return
            # Don't emit if fully inside an already-covered span
            if any(cs <= occ.start() and occ.end() <= ce for (cs, ce) in _covered):
                return
            _covered.add(span)
            # Use the ACTUAL matched text as the value so the vault restores
            # exactly what was in the document (fix: de-anon fidelity -- round-trip
            # name inversion, Bug 1 of #266 fidelity review).  Token consistency
            # is preserved: vault._token_for_name groups by distinctive words, so
            # "TESTONI FAKENAME" and "FAKENAME TESTONI" both resolve to person-
            # number 1 (\u27e6NOM_0001\u27e7 / \u27e6NOM_0001a\u27e7) and each restores to
            # its own surface form.
            _extra.append(Match(
                start=occ.start(), end=occ.end(),
                entity_type="NOM", value=occ.group(0),
                score=0.88, priority=58,
            ))

        for occ in pattern.finditer(text):
            _collect(occ)

        # fix #273 -- glued-token pass: scan with the loose-left pattern and emit
        # only occurrences NOT already found by the standard pattern (covered set
        # already updated above). Only accept when the preceding char IS a letter or
        # digit — i.e., a genuine glue artifact that the standard pattern missed.
        if glued_pattern is not None:
            for occ in glued_pattern.finditer(text):
                if occ.start() > 0 and re.match(r"[A-Za-z\xc0-\xff0-9]", text[occ.start() - 1]):
                    _collect(occ)

    return extra
