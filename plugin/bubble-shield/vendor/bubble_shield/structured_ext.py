"""
structured_ext.py — deterministic recognizers for FR-KYC FORM layouts.

fix #280 (2026-06-24 rev 3): close self-corroboration loop — exclude footer NOMs from
  corroboration pool.
  SELF-CORROBORATION BUG (proven rev2→rev3): Layer 1 filename_footer_matches() emits a
  footer NOM for every filename token (incl. brand/insurer names like ZEPHYRA that sit
  in the "au document <filename>" quote). Layer 2 doc_level_person_repetition_matches()
  was building its corroboration pool nom_detected_words from the SAME `found` list —
  which already contains those Layer-1 footer NOMs. So any filename token in the footer
  self-corroborated → seeded body-wide → over-masked.
  PROOF: "ZEPHYRA DUPONT - DER.pdf", body mentions ZEPHYRA (insurer) → ZEPHYRA was
  masked body-wide. PREDICA passed rev2 ONLY because it's on the stop-list (false pass).
  FIX: filename_footer_matches() now returns (matches, footer_nom_spans) where
  footer_nom_spans is a frozenset of (start, end) tuples. The _detector passes
  footer_nom_spans to doc_level_person_repetition_matches() which EXCLUDES those spans
  from nom_detected_words. Only independent body-recognizer NOMs (civility, form-label,
  signataire) can corroborate. Footer-only names still mask via Layer 1 positional.

fix #280 (2026-06-24 rev 2): filename-seeded masking for footer/boilerplate name leak.
  "Page de signatures complémentaire au document DURAND Théophile - DER 012026..."
  The client's name appears verbatim in the footer boilerplate as a quoted filename.
  No content recognizer catches this (no label, no civic-context).

  REVISED FIX (over-mask ship-blockers closed):
  The original approach seeded ALL non-stop-list filename tokens body-wide. This was
  INHERENTLY INCOMPLETE — "PREDICA DUPONT.pdf" seeded PREDICA, over-masking the
  insurer name everywhere in the doc body.

  Two-layer replacement:
    Layer 1 — POSITIONAL footer masking (filename_footer_matches):
      Directly matches the footer boilerplate pattern
      "au document <name-fragment>" and emits NOM matches RIGHT THERE.
      Covers the pure-footer case (D1: name only in footer, no body occurrence)
      without seeding anything body-wide.
    Layer 2 — CORROBORATED body-wide seeding:
      A filename candidate token is promoted to a body-wide repetition seed ONLY
      when it is corroborated — i.e., it already appears in a NOM match detected
      by another recognizer (civility, form-label, signataire, etc.) elsewhere.
      Footer NOMs from Layer 1 are EXCLUDED from the corroboration pool (rev3 fix).
      "DUPONT" in "M. DUPONT" → NOM detected → corroborated → body-wide seed.
      "PREDICA" / "ZEPHYRA" → footer NOM only, not independent → NOT seeded.

  Belt-and-suspenders: the stop-list is extended with previously missing insurer/
  brand tokens (PREDICA, HELVETIA, PREVOIR, ARIAL, AG2R, ALLIANZ, MONCEAU, CNP,
  SEQUOIA, BOURSE, FINAL, LIASSE, FISCALE, SELARL, etc.) so even if corroboration
  ever misfires, those words don't seed. But the corroboration rule is the real fix.

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
from typing import List, Optional

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


# ── STANDALONE COMMUNE + POSTCODE recognizer (fix #395) ──────────────────────
#
# The full-address ADRESSE recognizer (recognizers.py, priority 55) requires a
# STREET NUMBER + voie ("15 RUE DES SOURCES …"), so it catches a commune+postcode
# ONLY when it sits inside a full address. A BARE commune+postcode with no street
# — "MONTBOURG-LES-PINS 99000" standing alone — falls through every layer: GLiNER
# does not reliably flag FR communes as LIEU/ADRESSE, so it produces NO detection,
# no sub-threshold candidate, no review-queue entry → a silent leak with no human
# safety net (verified in Joris's stores, 2026-06-29).
#
# This recognizer catches exactly that shape: a town-name-shaped span IMMEDIATELY
# followed by a 5-digit French postcode. The ADJACENCY is the precision signal —
# a town next to a FR postcode is almost certainly an address fragment. Emits an
# ADRESSE Match so it masks under the existing ADRESSE policy (+ the #392 floor
# makes ADRESSE always-cloak). ADDITIVE to the full-address path — it does not
# touch it (the full-address recognizer still fires first inside a real address).
#
# Precision discipline (ties to the #348 NOM-filter discipline):
#   - Only fires when a TOWN-NAME token (capitalised, alpha, ≥2 chars) sits right
#     before the postcode. A lone 5-digit number with no town token before it is
#     NOT matched — so "le montant est 45000 euros" (lowercase "montant") and
#     "référence 12345" (lowercase "référence") never fire.
#   - Postcode validated to a plausible FR range (floor 01000) — rejects 00xxx.
#   - \b\d{5}\b adjacency avoids SIREN/SIRET fragments (9/14 digits) and most
#     amounts (the amount-word before is lowercase, not a town token).
#   - A small stop-set blocks ALL-CAPS heading words that can precede a 5-digit
#     number but aren't a commune (FACTURE, MONTANT, TOTAL, REFERENCE…).
#   - Org names (regulators / fund houses) that happen to precede a postcode are
#     dropped DOWNSTREAM by the engine allowlist (a town after an org is anyway
#     implausible) — we keep this recognizer self-contained and cheap.

# A French postcode: exactly 5 digits as a standalone token.
_FR_POSTCODE = r"\b(\d{5})\b"
# A commune name: one or more capitalised/uppercase tokens (accents, hyphens,
# apostrophes), allowing internal lowercase particles glued by hyphens or spaces
# — "MONTBOURG-LES-PINS", "Saint-Germain-en-Laye", "AIX EN PROVENCE", "Le Havre".
# Each leading token starts uppercase; we allow up to 5 tokens so long compound
# commune names ("SAINT-RÉMY-DE-PROVENCE") fit. Particles (de/en/sur/le/la/les/du)
# are joined by hyphen/space inside the name.
_COMMUNE_TOKEN = r"[A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ''\-]+"
_COMMUNE_NAME = (
    rf"{_COMMUNE_TOKEN}"
    rf"(?:[ \-](?:de|du|des|en|et|sur|sous|le|la|les|aux?|lès|l['']"
    rf"|{_COMMUNE_TOKEN})){{0,5}}"
)
# Commune name IMMEDIATELY followed (small gap: spaces/comma, no newline) by a
# 5-digit postcode. The town span is captured separately from the postcode so the
# emitted Match covers the WHOLE commune+postcode shape.
_COMMUNE_POSTCODE_RE = re.compile(
    rf"(?P<commune>{_COMMUNE_NAME})[ ,]{{1,3}}{_FR_POSTCODE}"
)

# ALL-CAPS heading / boilerplate words that can precede a 5-digit number but are
# NOT a commune. A commune span ENDING in one of these (its last token) is dropped.
_NOT_A_COMMUNE = {
    "FACTURE", "MONTANT", "TOTAL", "REFERENCE", "RÉFÉRENCE", "NUMERO", "NUMÉRO",
    "DOSSIER", "CLIENT", "CONTRAT", "POLICE", "COMPTE", "CODE", "PAGE", "ARTICLE",
    "TEL", "FAX", "FACTURE", "DEVIS", "BON", "COMMANDE", "LOT", "REF",
}


def commune_postcode_matches(text: str) -> List[Match]:
    """Find a STANDALONE 'COMMUNE-NAME + 5-digit FR postcode' shape (fix #395).

    Catches the bare commune+postcode the full-address ADRESSE recognizer misses
    (it requires a leading street number). Emits ADRESSE so the span masks under
    the existing ADRESSE policy / #392 floor.

    Precision: only fires when a town-name token sits IMMEDIATELY before the
    postcode (a lone 5-digit number, or one preceded by lowercase prose like
    'montant'/'référence', is NOT matched), and the postcode is in the plausible
    FR range (floor 01000, rejecting 00xxx).
    """
    out: List[Match] = []
    for m in _COMMUNE_POSTCODE_RE.finditer(text):
        commune = m.group("commune").strip()
        postcode = m.group(2)
        # Postcode must be a plausible FR commune postcode. Floor 01000 rejects
        # 00xxx; ceiling 99999 keeps overseas/forces postcodes (98xxx) AND the
        # synthetic 99000 test fixtures in range (real metropolitan codes are
        # 01000–95999; 97/98xxx are DOM-TOM; 99xxx reserved/synthetic).
        pc = int(postcode)
        if pc < 1000:
            continue
        # The last commune token must be a real town-name word, not a heading/
        # boilerplate word that merely happens to precede a postcode.
        last_token = re.split(r"[ \-]", commune)[-1].upper()
        if last_token in _NOT_A_COMMUNE:
            continue
        # First token must be a town-name word too (defensive: skip if it's a
        # pure heading word like "MONTANT 45000" that slipped through as 1 token).
        first_token = re.split(r"[ \-]", commune)[0].upper()
        if first_token in _NOT_A_COMMUNE:
            continue
        # Require at least one alpha town word of length ≥ 2.
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", commune):
            continue
        start = m.start("commune")
        end = m.end(2)  # span covers commune + postcode
        out.append(Match(start=start, end=end,
                         entity_type="ADRESSE", value=text[start:end],
                         score=0.85, priority=56))  # > GLiNER, ~ form recognizers
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


def make_structured_detector(filename_basename: str = ""):
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

    fix #280 rev 2 — filename_basename parameter (corroboration + positional):
      When provided, two mechanisms work together:
      Layer 1 (positional): filename_footer_matches() directly emits NOM for
        the name tokens where they appear in footer boilerplate ("au document …").
        This handles the pure-footer case without body-wide seeding.
      Layer 2 (corroborated body-wide): doc_level_person_repetition_matches()
        promotes a filename candidate token to a body-wide seed ONLY if that token
        is also present in an existing NOM match (from civility/form/signataire/
        or the footer NOM from Layer 1). Prevents insurer/brand names from seeding.
    """
    # fix #280 rev 2: extract filename person candidates once at creation time.
    _filename_seeds: Optional[List[str]] = None
    if filename_basename:
        try:
            tokens = extract_person_tokens_from_filename(filename_basename)
            if tokens:
                _filename_seeds = tokens
        except Exception:
            pass  # fail-open: if extraction fails, proceed without filename seeds

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
        # ── fix #395: standalone commune + postcode ('TOWN 99000') ───────────
        try:
            matches += commune_postcode_matches(text)
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
        # ── fix #280 rev 2 Layer 1: positional footer masking ────────────────
        # Emits NOM matches for filename name-tokens exactly where they appear in
        # footer boilerplate ("au document …"). Must run BEFORE the repetition
        # pass so independent body NOMs (not footer NOMs) can corroborate Layer 2.
        # fix #280 rev3: capture footer_nom_spans to exclude from corroboration pool.
        _footer_nom_spans: frozenset = frozenset()
        if _filename_seeds:
            try:
                _footer_matches, _footer_nom_spans = filename_footer_matches(
                    text, _filename_seeds)
                matches += _footer_matches
            except Exception:
                pass
        # ── fix #266+#280 rev 2+3 Layer 2: corroborated body-wide repetition ─
        # Must run LAST — after all primary detectors AND both post-passes above
        # so it has full RAISON_SOCIALE + NOM context to corroborate against.
        # fix #280 rev3: pass footer_nom_spans so footer-only NOMs are excluded
        # from the corroboration pool — preventing self-corroboration.
        try:
            matches += doc_level_person_repetition_matches(
                text, matches,
                filename_seeds=_filename_seeds,
                footer_nom_spans=_footer_nom_spans)
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
    # ── Top INSEE common French surnames (alphabetised, ~190 entries) ──────────
    # Source: INSEE patronymes; purpose: prevent lone-token loose passes from
    # over-masking these surnames when they appear as ordinary words in prose.
    # KEY SAFETY NOTE: this list only suppresses the LOOSE/lone-token passes.
    # A client surname that happens to be common (e.g. "MARTIN") is STILL masked
    # via the standard labeled/pair detection — the exclusion only blocks the
    # speculative lone-token repetition pass, not known-client detection.
    # (#267 expansion: added GARCIA, NGUYEN, PEREZ, LECLERC, CHEVALIER,
    #  FERNANDEZ, GONZALEZ, MARTINEZ, and ~120 more entries.)
    "ADAM", "ALBERT", "ALLARD", "ALMEIDA", "ANDRE", "ARNAUD",
    "AUBRY", "AUGER", "AUMONT", "AUBERT",
    "BARBIER", "BARON", "BAUDOIN", "BAZIN", "BERGER", "BERNARD",
    "BERTRAND", "BLANCHARD", "BLANC", "BLANCHET", "BLONDEL", "BODIN",
    "BOIS", "BONNET", "BOUCHARD", "BOUCHER", "BOULANGER", "BOURGEOIS",
    "BOYER", "BRUN", "BRUNEAU",
    "CARON", "CARPENTIER", "CHARLES", "CHARPENTIER", "CHEVALIER",
    "CLEMENT", "COLIN", "COLLIN",
    "DAVID", "DENIS", "DIALLO", "DUBOIS", "DUFOUR", "DUMAS",
    "DUMONT", "DUPONT", "DUPUIS", "DURAND",
    "FABRE", "FERNANDEZ", "FONTAINE", "FOURNIER", "FRANCOIS",
    "GARCIA", "GARNIER", "GAUTHIER", "GAUTIER", "GERARD", "GIRARD",
    "GIRAUD", "GONCALVES", "GONZALEZ", "GRANGE", "GROS", "GUERREIRO",
    "GUERIN", "GUICHARD", "GUILLAUME", "GUILLOT", "GUILLON",
    "HENRY", "HERVE", "HUMBERT",
    "JACQUET", "JEANNIN", "JOLY", "JOUBERT",
    "KLEIN",
    "LACROIX", "LAINE", "LAMBERT", "LANGLOIS", "LAPORTE", "LAVAL",
    "LAURENT", "LEBLANC", "LEBRUN", "LECLERC", "LECOMTE",
    "LEFEBVRE", "LEFEVRE", "LEGRAND", "LEMAIRE", "LEMOINE",
    "LENOIR", "LEROY", "LESAGE", "LESTRADE", "LEVY", "LION", "LOPEZ",
    "MARECHAL", "MARIE", "MARTEAU", "MARTIN", "MARTINEZ", "MATHIEU",
    "MAUREL", "MEUNIER", "MEYER", "MICHEL", "MOLINA", "MOREAU",
    "MOREL", "MORIN", "MOULIN", "MOUNIER", "MULLER",
    "NGUYEN", "NICOLAS", "NOEL",
    "OLIVIER",
    "PAGES", "PARENT", "PASCAL", "PELLETIER", "PEREZ", "PERRIN",
    "PETIT", "PICARD", "PICHON", "PIERRE", "PREVOST", "PRUNIER",
    "RENARD", "RENAUD", "RENAULT", "RICHARD", "RIVIERE", "ROBERT",
    "RODRIGUEZ", "ROLLAND", "ROUSSEAU", "ROUSSEL", "ROUX",
    "SALAZAR", "SANCHEZ", "SANTIAGO", "SCHMITT", "SCHNEIDER", "SIMON",
    "THOMAS", "TORRES", "TREMBLAY",
    "VALLET", "VASSEUR", "VIDAL", "VINCENT",
    "WEBER",
    # ── Colour/direction/geography words also used as surnames ─────────────────
    "BLEU", "BRUN", "GRIS", "GRAY", "NOIR", "ROUGE", "VERT", "VIOLET",
    "NORD", "SUD", "EST", "OUEST",
    "FRANCE", "PARIS",
    # ── Forenames frequently doubled as surnames ───────────────────────────────
    "ALAIN", "ANNE", "CLAUDE", "ERIC", "JEAN", "LUC", "MARC", "PAUL",
    # ── Short/ambiguous entries retained from original list ────────────────────
    "GRAND", "LEBRUN", "LEGRAND", "PAGE", "ROI", "ROSE", "SAGE",
})

# Common French forenames (len >= 6) that are also word-prefixes in French —
# e.g. CLAIRE→"clairement", JULIEN→"julienne", ANTOINE→"antoinette",
# ANDREA→"andréa" but also prefix of "ANDREAssistant"-like false collisions.
# Seeds in this set do NOT get the right-glue (loose-right-boundary) pass
# because the risk of over-masking a real French word prefix far outweighs the
# benefit — the left-glue (#273) and standard pass still fire for these names.
# (Same exclusion principle as _COMMON_FRENCH_SURNAMES for surname seeds.)
_COMMON_FRENCH_FORENAMES: frozenset = frozenset({
    # ── 6-letter forenames that are French word-prefixes ─────────────────────
    "CLAIRE", "JULIEN", "ANDREA", "PIERRE", "MARTIN", "PASCAL",
    "CLAUDE", "THIERRY", "PATRICE",
    # ── 7-letter ──────────────────────────────────────────────────────────────
    "ANTOINE", "MAXIME", "BRIGITTE", "VALERIE", "CECILE",
    "SYLVIE", "SOPHIE", "AURELIE",
    # ── 8-letter ──────────────────────────────────────────────────────────────
    "ISABELLE", "NATHALIE", "SANDRINE", "CAROLINE", "FREDERIC",
    "NICOLAS", "STEPHANE", "FLORENCE", "VIRGINIE", "LAURENCE",
    "BERTRAND", "CHRISTOPHE", "ALEXANDRE", "GUILLAUME",
    # ── 9-letter ──────────────────────────────────────────────────────────────
    "CORENTIN", "CATHERINE", "VERONIQUE", "DOMINIQUE", "EMMANUEL",
    "CHRISTELLE", "FRANCOISE", "GENEVIEVE", "JACQUELINE",
    # ── 10+ letter ────────────────────────────────────────────────────────────
    "CHRISTOPHE", "CHRISTELLE", "MAXIMILIANE",
    # ── Additional common forenames with known word-prefix collisions ─────────
    "MARGUERITE", "CHARLOTTE", "VALENTINA", "ANGELIQUE",
    "BENEDICTE", "CLEMENTINE", "JOSEPHINE", "MADELEINE",
    "NATHANAEL", "RAFFAELE", "THEOPHILE",
    # ── Forenames that are also adj/adv prefixes ──────────────────────────────
    "CHRISTIAN", "CHRISTIANE", "CLEMENCE", "CLEMENT",
    "AMANDINE", "ARMELLE", "ARNAUD",
    "BEATRICE", "BLANCHE",
    "DAMIEN", "DANIELA",
    "EDOUARD", "ELOISE", "ELODIE",
    "FABIENNE", "FABRICE",
    "GILLES",
    "HELENE", "HERVE",
    "JEROME",
    "LAETITIA", "LAURIE",
    "LUDOVIC", "LUCIE",
    "MARINE", "MARLENE", "MATHIEU", "MATTHIEU",
    "MELANIE", "MELANIE",
    "MONIQUE",
    "OCEANE", "OLIVIER",
    "PAULINE", "PHILIPPE",
    "RAPHAEL", "RENAUD",
    "SOLANGE", "SOLENE",
    "THIBAULT", "THIBAUD",
    "VALENTIN",
    "XAVIER", "XIMENA",
    "YANNICK", "YOANN",
    "ZEPHYRIN",
})

_PERSON_TOKEN_MIN_LEN = 4


# ── FILENAME PERSON-NAME EXTRACTION (fix #280) ───────────────────────────────
#
# CGP filenames embed the client's name:
#   "DURAND Théophile - DER 012026 - 2026-02-18.pdf"
#   "TESTONI Prénomtest - Convention RTO - signé.pdf"
#
# We strip doc-type/product/date tokens and return the residual person-name tokens.
# These are used as HIGH-CONFIDENCE seeds (bypass_common_surname_guard=True) in
# doc_level_person_repetition_matches() so every occurrence in body AND footer masks.
#
# STOP-LIST design:
#   - CGP product / document-type words (DER, RTO, DCC, DA, SCPI, CIF, etc.)
#   - File-state suffixes (signé, rempli, extrait, original, copie, etc.)
#   - Firm/product names (GEFINEO, CORUM, EURION, PRIMONIAL, etc.)
#   - Common legal/structural words that appear in filenames but aren't names
#   - Date patterns are stripped separately by regex (not needed in set)
#
# Precision: we only strip tokens that are IN the stop-list.  Unknown all-caps tokens
# (the actual client surname) are kept and become seeds.
# Company-only filenames ("SELARL … .pdf") yield no seeds here — the RAISON_SOCIALE
# path already handles them.

_FILENAME_STOP_TOKENS: frozenset = frozenset({
    # ── Document type words ───────────────────────────────────────────────────
    "DER", "RTO", "DCC", "DA", "CIF", "LM", "PP", "PROFIL",
    "CONVENTION", "CONTRAT", "AVENANT", "ANNEXE", "MANDAT",
    "LETTRE", "FICHE", "FORMULAIRE", "DOCUMENT", "DOSSIER",
    "ATTESTATION", "RAPPORT", "SYNTHESE", "SYNTHÈSE", "BILAN",
    "NOTE", "NOTICE", "DEVIS", "PROPOSITION", "OFFRE",
    "QUESTIONNAIRE", "DECLARATION", "DÉCLARATION", "COMPTE", "RENDU",
    "RECAPITULATIF", "RÉCAPITULATIF", "RELEVE", "RELEVÉ",
    # ── File-state / workflow suffixes ────────────────────────────────────────
    "SIGNE", "SIGNÉ", "REMPLI", "EXTRAIT", "ORIGINAL", "COPIE",
    "VALIDE", "VALIDÉ", "DEFINITIF", "DÉFINITIF", "ARCHIVE", "ARCHIVÉ",
    "DRAFT", "BROUILLON", "VERSION", "REV", "V1", "V2", "V3",
    # Reviewer BUG 3 + general
    "FINAL", "FINALE",
    # ── Firm / distributor / product names (non-exhaustive but common in CGP) ─
    "GEFINEO", "CORUM", "EURION", "PRIMONIAL", "SPIRICA", "CARDIF",
    "GENERALI", "AVIVA", "SWISS", "LIFE", "AFER", "CARAC", "MAIF",
    "APICIL", "SURAVENIR", "LINXEA", "FORTUNEO", "BOURSORAMA",
    "EPARGNISSIMO", "NALO", "YOMONI", "GRISBEE",
    # Reviewer BUG 1 — major FR insurers missing from original stop-list
    "PREDICA", "HELVETIA", "PREVOIR", "ARIAL", "AG2R", "ALLIANZ",
    "MONCEAU", "CNP", "SEQUOIA",
    # Other major FR insurers (belt-and-suspenders)
    "ABEILLE", "AXA", "ALLIANZ", "AVIVA", "BNP", "CREDIT", "AGRICOLE",
    "NATIXIS", "SOGECAP", "UNÉO", "UNEO", "MNPAF", "MUTAVIE", "GROUPAMA",
    "INTERÉPARGNISSIMO",
    # ── Financial product / category words ───────────────────────────────────
    "SCPI", "PERCO", "PERP", "PER", "PEA", "PERI", "PEROB", "PERECO",
    "ASSURANCE", "VIE", "RETRAITE", "CAPITALISATION", "PLACEMENT",
    "INVESTISSEMENT", "EPARGNE", "ÉPARGNE", "IMMOBILIER", "FONCIER",
    "PATRIMOINE", "GESTION", "PILOTEE", "PILOTÉE", "LIBRE",
    # Reviewer BUG 2 — BOURSE missing
    "BOURSE",
    # Reviewer BUG 4 — LIASSE / FISCALE missing
    "LIASSE", "FISCALE",
    # ── Legal / structural words ──────────────────────────────────────────────
    "PERSO", "CONJOINT", "CLIENT", "CLIENTS", "PROSPECT", "CONSEILLER",
    "CABINET", "ETUDE", "GROUPE", "SOCIETE", "SOCIÉTÉ",
    # ── Date/reference words that appear as filename segments ─────────────────
    "DATE", "REF", "REFERENCE", "RÉFÉRENCE", "NUM", "NUMERO", "NUMÉRO",
    # ── Pure numbers / short codes (these are stripped by regex; listed for safety) ─
    "PDF", "DOCX", "DOC", "XLSX",
})

# Date patterns to strip from filename tokens before name extraction.
# Matches: "012026", "2026", "012026", "2026-02-18", "18022026", etc.
_FILENAME_DATE_RE = re.compile(
    r"^\d{4,8}$"                    # pure numeric: months+year combos, years
    r"|^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$"   # DD/MM/YYYY or variants
    r"|^\d{4}[.\-]\d{2}[.\-]\d{2}$"            # YYYY-MM-DD
)

# Single-char tokens and pure-digit tokens are never names.
_FILENAME_TOKEN_MIN_LEN = 2


def extract_person_tokens_from_filename(basename: str) -> List[str]:
    """Extract person-name tokens from a CGP document filename (fix #280).

    Given "DURAND Théophile - DER 012026 - 2026-02-18.pdf", returns
    ["DURAND", "THÉOPHILE"] (normalised to upper-case).

    Algorithm:
      1. Strip extension.
      2. Tokenise on spaces, hyphens, underscores, dots.
      3. Discard tokens that match:
           - _FILENAME_DATE_RE (date/year patterns)
           - _FILENAME_STOP_TOKENS (doc-type/product/firm words)
           - _RAISON_SOCIALE_PREFIXES (forme-juridique type words)
           - _FORME_JURIDIQUE_SET (SELARL, SAS, etc.)
           - Pure digits or tokens shorter than _FILENAME_TOKEN_MIN_LEN
      4. What remains: person-name tokens (surname and/or forename).
      5. Returns them normalised to upper-case NFC.

    Precision contract:
      - If all residual tokens are company-type words (SELARL, etc.), returns [].
        Company-only filenames are handled by the RAISON_SOCIALE path; no person seeds.
      - Tokens that are common French words but NOT in the stop-list are kept —
        they're bypassed by the RAISON_SOCIALE-style bypass_common_surname_guard=True
        in the call site so even common surnames in filenames are seeded.
    """
    import unicodedata

    def _norm(s: str) -> str:
        """NFD → strip combining accents → NFC upper-case."""
        # We normalise accents for stop-list comparison but keep the original
        # accentuated form so token matching against the document text works.
        return unicodedata.normalize("NFC", s.upper())

    # Strip extension
    stem = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", basename)

    # Tokenise: split on whitespace, hyphens, underscores, dots
    raw_tokens = re.split(r"[\s\-_./]+", stem)

    person_tokens: List[str] = []
    for tok in raw_tokens:
        if not tok:
            continue
        tok_norm = _norm(tok)
        # Skip short tokens
        if len(tok_norm) < _FILENAME_TOKEN_MIN_LEN:
            continue
        # Skip pure-digit tokens (numbers, phone, reference codes)
        if re.match(r"^\d+$", tok_norm):
            continue
        # Skip date patterns
        if _FILENAME_DATE_RE.match(tok_norm):
            continue
        # Skip stop-list tokens (exact match after normalisation)
        if tok_norm in _FILENAME_STOP_TOKENS:
            continue
        # Skip forme-juridique / RAISON_SOCIALE prefix words (company names, not persons)
        if tok_norm in _FORME_JURIDIQUE_SET or tok_norm in _RAISON_SOCIALE_PREFIXES:
            continue
        # Keep this token as a person-name candidate
        person_tokens.append(tok_norm)

    return person_tokens


# ── POSITIONAL FOOTER MATCHING (fix #280 rev 2) ─────────────────────────────
#
# CGP footer boilerplate quotes the filename directly:
#   "Page de signatures complémentaire au document TESTONI Prénomtest - DER 012026"
#   "au fichier DURAND Théophile - Convention RTO - signé"
#
# This pass emits NOM matches for the person-name TOKENS right where they appear
# in that boilerplate — WITHOUT seeding those tokens body-wide. This cleanly
# covers the pure-footer case (name appears ONLY in the footer, no body occurrence)
# without risk of over-masking product/insurer names that happen to share the
# filename slot.
#
# Pattern: "au document" or "au fichier" followed by text. We re-run the
# candidate extraction on that fragment and emit NOM for each candidate token
# found there. We DON'T seed those tokens globally — the corroboration layer
# handles body-wide repetitions.
#
# Why this is safe for PREDICA DUPONT.pdf:
#   Footer "au document PREDICA DUPONT" → PREDICA and DUPONT both appear as footer
#   NOM matches. This correctly masks the footer reference.  But PREDICA is NOT
#   corroborated elsewhere (no NOM detection in the body), so it is NOT seeded
#   body-wide. Body occurrences of "PREDICA" (insurer name in content) are untouched.

_FOOTER_QUOTE_RE = re.compile(
    r"(?:au\s+(?:document|fichier)|Page\s+de\s+signatures?\s+compl[eé]mentaire\s+au\s+(?:document|fichier))"
    r"\s+(?P<fragment>[^\n]{3,120})",
    re.IGNORECASE,
)


def filename_footer_matches(
        text: str,
        filename_candidates: List[str],
) -> "tuple[List[Match], frozenset]":
    """Emit NOM matches for person-name tokens from the filename where they appear
    in footer boilerplate ("au document <fragment>").

    This is the positional layer of the #280 fix: it covers the case where the
    client's name appears ONLY in the footer (not anywhere else in the body), so
    no corroboration signal is available yet. The footer quote IS the evidence.

    Does NOT seed tokens globally — no body-wide over-mask risk.

    Returns (matches, footer_nom_spans) where footer_nom_spans is a frozenset of
    (start, end) tuples covering every NOM match emitted here.

    fix #280 rev3 — self-corroboration fix:
    The caller MUST pass footer_nom_spans to doc_level_person_repetition_matches()
    via the footer_nom_spans parameter so those footer-only NOMs are EXCLUDED from
    the corroboration pool (nom_detected_words). Without this exclusion, every
    filename token appearing in the footer self-corroborates → body-wide over-mask.
    """
    if not filename_candidates:
        return [], frozenset()

    out: List[Match] = []
    # Build case-insensitive patterns for each candidate token
    cand_patterns = [
        (tok, re.compile(r"(?<![A-Za-z\xc0-\xff])" + re.escape(tok) + r"(?![A-Za-z\xc0-\xff])", re.IGNORECASE | re.UNICODE))
        for tok in filename_candidates
        if len(tok) >= _FILENAME_TOKEN_MIN_LEN
    ]
    if not cand_patterns:
        return [], frozenset()

    seen_spans: set = set()
    for footer_m in _FOOTER_QUOTE_RE.finditer(text):
        frag_start = footer_m.start("fragment")
        frag_text = footer_m.group("fragment")
        for tok, pat in cand_patterns:
            for tok_m in pat.finditer(frag_text):
                start = frag_start + tok_m.start()
                end = frag_start + tok_m.end()
                span = (start, end)
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                out.append(Match(
                    start=start, end=end,
                    entity_type="NOM",
                    value=text[start:end],
                    score=0.87, priority=58,
                ))
    # Return both the match list and the frozenset of footer-sourced spans.
    # Caller passes this to doc_level_person_repetition_matches() (fix #280 rev3).
    footer_nom_spans = frozenset((m.start, m.end) for m in out)
    return out, footer_nom_spans


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


def _person_name_seeds(tokens: List[str],
                       bypass_common_surname_guard: bool = False) -> List[str]:
    """Return seed strings for the doc-level person-name repetition pass (fix #266).

    Includes the full PAIR (both orderings) when >=2 tokens.
    Includes a lone token only if it is NOT a common word and has length >= _PERSON_TOKEN_MIN_LEN.

    bypass_common_surname_guard (fix #267-v2 recall regression):
      When True, the _COMMON_FRENCH_SURNAMES exclusion is NOT applied to lone-token
      seeds.  Use this for RAISON_SOCIALE-derived tokens — the company match already
      confirms the token IS the client's name, so the "common word in prose" concern
      does not apply.  The guard is still applied on the unanchored prose-repetition /
      NOM path (where a common surname might genuinely be an ordinary word).
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
        surname_ok = (bypass_common_surname_guard
                      or tok.upper() not in _COMMON_FRENCH_SURNAMES)
        if (len(tok) >= _PERSON_TOKEN_MIN_LEN
                and surname_ok
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


def doc_level_person_repetition_matches(
        text: str,
        found: List[Match],
        filename_seeds: Optional[List[str]] = None,
        footer_nom_spans: Optional[frozenset] = None,
) -> List[Match]:
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

    fix #275 — glued-token right-boundary (mirror of #273):
    The symmetric artifact: a known forename/surname is FOLLOWED by more letters
    with no space, e.g. "FAKENAMESignature". The standard right boundary
    ``(?![A-Za-z])`` fails because "S" IS a letter immediately after the seed.
    Real output seen on liasse: "\u27e6NOM_0002\u27e7 <FORENAME>Signature" — the surname
    was correctly masked, but the forename leaked because it was right-glued to the
    next word by a PDF extraction artifact.

    For the same raison_sociale_lone_seeds (KNOWN, length >= 6, not a common
    surname), we also compile a right_glued_pattern with a loose RIGHT boundary
    (no right-char restriction) but a strict LEFT boundary. We emit only when the
    FOLLOWING char IS alphabetic — confirming a genuine right-glue artifact, not
    a partial-word hit at the start of a longer token.

    Precision guard: lone-token seeds shorter than 6 chars or in
    _COMMON_FRENCH_SURNAMES are already excluded by _person_name_seeds(), so the
    loose right boundary never fires for "MARTIN", "PETIT", "DUBOIS", etc.
    The strict left boundary ``(?<![A-Za-z])`` prevents matching inside a longer
    word on the left (e.g. if some token ends with the seed letters).

    fix #280 rev 2 — filename_seeds parameter + CORROBORATION:
    Person-name tokens extracted from the file basename (e.g. ["DURAND", "THÉOPHILE"])
    are injected into the body-wide repetition pass ONLY when CORROBORATED — i.e. only
    when the token also appears in an existing NOM match detected elsewhere in the
    document by an INDEPENDENT body recognizer (civility, form-label, signataire, etc.).

    WHY: the original approach seeded ALL filename tokens body-wide, causing over-mask
    when insurer/product names (PREDICA, HELVETIA, etc.) appeared in the filename
    before the client's name. "PREDICA DUPONT.pdf" → PREDICA was seeded → body-wide
    PREDICA matches → every mention of the insurer was masked.

    CORROBORATION rule: a filename candidate token T is promoted to a body-wide seed
    only if T.upper() is a substring of some INDEPENDENT NOM match value in `found`
    (i.e. a NOM whose span is NOT in footer_nom_spans).
    Token DUPONT → "M. DUPONT" → independent NOM → corroborated → body-wide seed.
    Token ZEPHYRA/PREDICA → footer NOM only (span excluded) → NOT corroborated → NOT seeded.

    fix #280 rev 3 — footer_nom_spans parameter (self-corroboration fix):
    The footer_nom_spans frozenset contains (start, end) tuples for every NOM emitted
    by filename_footer_matches() (Layer 1). These MUST be EXCLUDED from the
    corroboration pool. Without this exclusion, any filename token that appears in the
    footer self-corroborates via its own Layer-1 NOM, defeating the entire guard:
      "ZEPHYRA DUPONT - DER.pdf" → Layer 1 emits NOM("ZEPHYRA") at footer offset N
      → Layer 2 sees ZEPHYRA in nom_detected_words (self-corroboration)
      → ZEPHYRA seeded body-wide → insurer name masked throughout.
    With footer_nom_spans excluded, only body recognizer NOMs (not footer NOMs) count.
    """
    if not found and not filename_seeds:
        return []

    # Track which seeds came from RAISON_SOCIALE derivation (fix #273: loose left
    # boundary for PDF-glued lone tokens).
    seeds: set = set()
    raison_sociale_lone_seeds: set = set()   # lone tokens only (no space in seed)

    # fix #280 rev 2+3: inject filename-derived person-name seeds — but ONLY for
    # tokens that are corroborated by an INDEPENDENT body NOM detection in `found`.
    # This prevents insurer/brand names (PREDICA, ZEPHYRA, etc.) from being
    # seeded body-wide just because they appear in the filename footer.
    #
    # fix #280 rev3: EXCLUDE footer-sourced NOM spans from the corroboration pool.
    # footer_nom_spans contains the (start, end) tuples of every NOM emitted by
    # filename_footer_matches() (Layer 1). Without this exclusion, a filename token
    # T that appears in the footer self-corroborates via its own Layer-1 NOM → over-mask.
    _footer_spans: frozenset = footer_nom_spans if footer_nom_spans else frozenset()
    if filename_seeds:
        # Build a set of NOM value tokens already detected by INDEPENDENT (non-footer)
        # recognizers (uppercased words). Footer-sourced NOMs are excluded.
        nom_detected_words: set = set()
        for m in found:
            if m.entity_type == "NOM":
                # Exclude footer-sourced NOMs from the corroboration pool (rev3 fix).
                if (m.start, m.end) in _footer_spans:
                    continue
                for w in re.split(r"[\s\-'']", m.value.upper()):
                    w = w.strip()
                    if len(w) >= 3:
                        nom_detected_words.add(w)

        for cand in filename_seeds:
            cand_upper = cand.upper()
            # Corroboration: the candidate must appear in a NOM match word
            # (exact match against words extracted from NOM values above).
            # We also allow substring matching for accented variants:
            # e.g. "PRÉNOMTEST" corroborated by NOM containing "PRENOMTEST".
            corroborated = (
                cand_upper in nom_detected_words
                or any(cand_upper in w or w in cand_upper for w in nom_detected_words if len(w) >= 4)
            )
            if not corroborated:
                continue  # skip: not independently confirmed as a name
            for seed in _person_name_seeds([cand], bypass_common_surname_guard=True):
                seeds.add(seed)
                if " " not in seed and len(seed) >= 6:
                    raison_sociale_lone_seeds.add(seed)

    for m in found:
        if m.entity_type == "RAISON_SOCIALE":
            c = _canonical_company_name(m.value)
            tokens = extract_person_name_from_raison_sociale(c)
            if not tokens:
                continue
            # fix #267-v2 recall regression: bypass _COMMON_FRENCH_SURNAMES guard
            # for RAISON_SOCIALE-derived tokens.  The company match already anchors
            # the token as the client's name — "common surname in prose" concern
            # does NOT apply here.  An unanchored common surname in prose (no
            # matching company) still uses the guard (NOM path below).
            for seed in _person_name_seeds(tokens, bypass_common_surname_guard=True):
                seeds.add(seed)
                # A lone token seed has no space and comes from a known company name
                # -> eligible for the glued-token loose-left-boundary pass (#273)
                # AND the loose-right-boundary pass (#275).
                # Fix #275 ship-blocker: exclude forenames from the right-glue pass —
                # common French forenames (len>=6) are frequent word-prefixes in French
                # (CLAIRE→clairement, JULIEN→julienne, ANDREA→ANDREAssistant) so the
                # loose right boundary would over-mask legitimate French words.
                # The left-glue (#273) and standard pass still fire for these seeds.
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
        # fix #275 -- loose-right-boundary pattern for RAISON_SOCIALE lone-token
        # seeds (mirror of #273): the seed may be immediately followed by a letter
        # with no space (right-glue artifact, e.g. "FAKENAMESignature").  The LEFT
        # boundary stays strict so we don't match inside a longer token on the left.
        #
        # Ship-blocker guards (fix #275 v2):
        #   1. Forename exclusion: seeds in _COMMON_FRENCH_FORENAMES do NOT get the
        #      right-glue pass — forenames are common French word-prefixes
        #      (CLAIRE→clairement, ANDREA→ANDREAssistant, JULIEN→julienne).
        #   2. Uppercase-next-char guard: only emit when the FOLLOWING char is
        #      UPPERCASE (CamelCase glue = real PDF artifact: "FAKENAMESignature",
        #      "AMELSignature"). A lowercase continuation ("CLAIREment", "ANTOINEtte",
        #      "TESTONImania") is a legitimate French inflected word — never mask.
        right_glued_pattern = None
        if (seed in raison_sociale_lone_seeds
                and seed.upper() not in _COMMON_FRENCH_FORENAMES):
            right_glued_pattern = re.compile(
                r"(?<![A-Za-z\xc0-\xff])"
                + re.escape(seed),
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

        # fix #275 -- right-glued-token pass (mirror of #273): scan with the
        # loose-right pattern and emit only occurrences where the FOLLOWING char IS
        # an UPPERCASE letter — confirming a genuine CamelCase PDF-glue artifact
        # (e.g. "FAKENAMESignature", "AMELSignature" — capital S).
        # A lowercase continuation ("CLAIREment", "ANTOINEtte", "TESTONImania")
        # signals a real inflected French word — never mask those.
        # (Ship-blocker fix #275 v2: uppercase-next-char guard.)
        # The covered set is already updated by the standard + left-glue passes above,
        # so spans already found are automatically skipped by _collect().
        if right_glued_pattern is not None:
            for occ in right_glued_pattern.finditer(text):
                end = occ.end()
                if end < len(text) and re.match(r"[A-Z\xc0-\xd6\xd8-\xde]", text[end]):
                    _collect(occ)

    return extra
