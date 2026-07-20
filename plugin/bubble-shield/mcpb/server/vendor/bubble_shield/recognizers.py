"""
recognizers.py — FR/finance PII detectors.

Each recognizer yields Match spans (start, end, type, value, score). The
engine resolves overlaps and replaces the spans with vault tokens.

Design notes:
  - Structured PII (email, IBAN, ISIN, SIRET, secu…) is detected by regex
    and, where a checksum exists, *validated* (mod-97, Luhn). A passing
    checksum gives score 1.0; a regex-only match gives a lower score. This
    is the signal the engine turns into the fail-closed confidence.
  - Names/addresses are the hard, lower-recall part. We use high-precision
    title cues (M./Mme/Monsieur…) plus a French first-name gazetteer for
    untitled "Prénom Nom". An optional Presidio/NER layer (presidio_ext)
    can be enabled to raise name recall; it's off by default so the tool
    runs anywhere with no ML.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from bubble_shield.gazetteer import (
    FRENCH_FIRST_NAMES,
    FRENCH_FIRST_NAMES_HOMOGRAPH,
    FRENCH_FIRST_NAMES_PLAIN,
)


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    entity_type: str
    value: str
    score: float = 1.0     # detection confidence (checksum-validated = 1.0)
    priority: int = 0      # recognizer priority (overlap resolution)

    @property
    def length(self) -> int:
        return self.end - self.start


# ─── checksum validators ─────────────────────────────────────────────────

def _iban_valid(iban: str) -> bool:
    # Strip group separators before mod-97: whitespace AND hyphens/dots, so that
    # hyphen- or dot-grouped forms ("FR76-1027-…", "FR76.1027.…") validate the
    # same as spaced/unspaced ones. Real forms use all three (fix: recall LEAK 1).
    s = re.sub(r"[\s.\-]+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _luhn_ok(number: str) -> bool:
    ds = [int(c) for c in number if c.isdigit()]
    if not ds:
        return False
    total, parity = 0, len(ds) % 2
    for i, d in enumerate(ds):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _isin_valid(isin: str) -> bool:
    s = isin.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", s):
        return False
    digits = "".join(str(int(c, 36)) if c.isalpha() else c for c in s)
    return _luhn_ok(digits)


def _siren_valid(num: str) -> bool:
    d = re.sub(r"\s+", "", num)
    return len(d) in (9, 14) and _luhn_ok(d)


def _num_fiscal_ref_valid(s: str) -> bool:
    """Validate a numéro fiscal / référence d'avis (13 digits, grouping
    ``NN NN NNNNNNN NN``). The trailing 2-digit control key is a mod-97 check on
    the leading 11 digits: ``key == 97 - (body11 % 97)`` (the DGFiP algorithm,
    same family as the NIR/SECU key). This checksum is the precision anchor — a
    random 13-digit run in this grouping almost never satisfies it, so we only
    mask when it passes. Separators (spaces) are stripped before the check."""
    d = re.sub(r"\s+", "", s)
    if len(d) != 13 or not d.isdigit():
        return False
    body, key = d[:11], int(d[11:])
    return (97 - (int(body) % 97)) == key


# ─── regex recognizers ───────────────────────────────────────────────────

@dataclass(frozen=True)
class Recognizer:
    entity_type: str
    pattern: re.Pattern
    priority: int                              # higher wins on overlap
    group: int = 0                             # which capture group is the PII
    validator: Optional[Callable[[str], bool]] = None
    score_if_unvalidated: float = 0.6
    # When True, a match whose validator FAILS is dropped entirely (not emitted),
    # instead of emitted at score_if_unvalidated. Used by checksum-gated recognizers
    # whose regex shape is not rare enough to fail-closed on (fix #319: the fiscal
    # reference grouping NN NN NNNNNNN NN would over-mask stray 13-digit runs, so we
    # only mask when the mod-97 control key validates). Keeps the fail-closed
    # behaviour of IBAN/ISIN/SIRET (drop_if_unvalidated=False) untouched.
    drop_if_unvalidated: bool = False
    # Context-aware gate (#477): receives (full_text, match_start, match_end) — unlike
    # `validator`, which only sees the captured value. Used by the homograph-forename
    # recognizer to require a civility cue (M./Mme/…) earlier in the SAME LINE before
    # masking a common-word/brand homograph forename ("Robert", "Colette") via the
    # untitled path. A `re` fixed-width lookbehind can't express "cue anywhere earlier
    # in the line" (variable length), so this runs as a plain post-match text check
    # instead. Always paired with drop_if_context_fails=True: no cue → no mask
    # (precision-only; never affects any other recognizer).
    context_validator: Optional[Callable[[str, int, int], bool]] = None
    drop_if_context_fails: bool = False

    def find(self, text: str) -> List[Match]:
        out: List[Match] = []
        for m in self.pattern.finditer(text):
            value = m.group(self.group)
            if not value or not value.strip():
                continue
            start, end = m.span(self.group)
            score = 1.0
            if self.validator is not None:
                if self.validator(value):
                    score = 1.0
                elif self.drop_if_unvalidated:
                    continue  # precision: no checksum, no mask
                else:
                    score = self.score_if_unvalidated
            if self.context_validator is not None:
                if not self.context_validator(text, start, end):
                    if self.drop_if_context_fails:
                        continue  # precision: no corroborating context cue, no mask
                    else:
                        score = min(score, self.score_if_unvalidated)
            out.append(Match(start, end, self.entity_type, value, score, self.priority))
        return out


# Street types for postal addresses.
_VOIE = (r"(?:rue|avenue|av\.?|bd|boulevard|impasse|all[ée]e|chemin|place|quai|"
         r"cours|route|r[ée]sidence|villa|square|passage)")

# Person-name title cues (high precision).
_TITRE = r"(?:M\.|Mme|Mlle|Monsieur|Madame|Mademoiselle|Me|Ma[îi]tre|Dr|Pr)"
# A name word: a capitalised token of LETTERS/marks only — never digits (a real
# surname has none), and never a domain keyword that sits next to structured PII
# in these docs (IBAN, EUR, ISIN, SIREN/SIRET, RIB, BIC, SPI…). Without these
# two guards a greedy name span swallowed "IBAN FR7630…" into a single ⟦NOM⟧,
# which DROPPED the IBAN and then reported "no residual PII" (false-confidence,
# recall bug found 2026-06-02). Letters-only stops the digits; the stopword
# lookahead stops the label. Note: \w/digits removed on purpose.
_NAME_STOPWORDS = (r"IBAN|EUR|ISIN|SIREN|SIRET|RIB|BIC|SWIFT|SPI|TVA|FR\d|"
                   r"PEA|PER|SCPI|RTO|DIC|DER|CIF|KYC|DCC")
_NAMEWORD = rf"(?!(?:{_NAME_STOPWORDS})\b)[A-ZÉÈÀÂÎÔÛ][A-Za-zÀ-ÿ'’\-]+"

# First-name gazetteer alternation (built once).
_FIRST = "|".join(sorted((re.escape(n) for n in FRENCH_FIRST_NAMES), key=len, reverse=True))
# #477: the untitled bare-name recognizer is split in two —
#   _FIRST_PLAIN      fires unconditionally (unchanged recall/precision).
#   _FIRST_HOMOGRAPH  fires only with a civility cue earlier in the line
#                     (see _homograph_title_nearby below).
_FIRST_PLAIN = "|".join(sorted((re.escape(n) for n in FRENCH_FIRST_NAMES_PLAIN), key=len, reverse=True))
_FIRST_HOMOGRAPH = "|".join(sorted((re.escape(n) for n in FRENCH_FIRST_NAMES_HOMOGRAPH), key=len, reverse=True))

# Same window discipline as context_boost.py: look back a bounded number of chars,
# never past a newline (a title on the previous line/field is not "this mention").
_HOMOGRAPH_TITLE_WINDOW = 60
_TITRE_RE = re.compile(rf"\b{_TITRE}\b", re.UNICODE)


def _homograph_title_nearby(text: str, start: int, end: int) -> bool:
    """True iff a civility title (M./Mme/Monsieur/…) appears earlier on the SAME
    LINE within _HOMOGRAPH_TITLE_WINDOW chars of a homograph-forename match.
    This is the "additional signal" #477 asks for: a homograph like "Robert" or
    "Colette" only masks via the untitled path when a title cue corroborates that
    it's being used as a person's name, not a dictionary/brand reference."""
    lo = max(0, start - _HOMOGRAPH_TITLE_WINDOW)
    line_start = text.rfind("\n", lo, start)
    lo = max(lo, line_start + 1)  # never cross a newline into a previous field/line
    return bool(_TITRE_RE.search(text, lo, start))

# Job title / profession. A poste in a named company identifies a person almost
# as well as a name ("directeur marketing chez TotalEnergies" → you can find who
# that is), so the advisor flagged it must be cloakable. Context-cued on a role
# word, an optional qualifier (marketing, des ventes…), and an optional
# "chez/au sein de <Company>". Cloaked by default via the POSTE policy row; the
# user can keep it if they decide it's not identifying in their context.
_ROLE = (r"(?:directeur|directrice|dirigeant|dirigeante|g[ée]rant|g[ée]rante|"
         r"pr[ée]sident|pr[ée]sidente|cadre|ing[ée]nieur|ing[ée]nieure|responsable|"
         r"chef|cheffe|manager|consultant|consultante|chargé|chargée|"
         r"associé|associée|fondateur|fondatrice|salarié|salariée|employé|employée|"
         r"avocat|avocate|notaire|expert-comptable|PDG|DG|DAF|DRH)")
_QUAL = r"(?:[^\S\n]+(?:de|des|du|d['’]|en|au|dans|à[^\S\n]+la|une?)?[^\S\n]*[a-zà-ÿ][\wà-ÿ\-]+){0,3}"
_COMP = r"(?:[^\S\n]+(?:chez|au[^\S\n]+sein[^\S\n]+de|de[^\S\n]+la|de|dans)[^\S\n]+[A-ZÉÈ][\wÀ-ÿ&\.\- ]{1,40})?"

RECOGNIZERS: List[Recognizer] = [
    # — structured / checksum-backed (high priority) —
    Recognizer("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), 100),
    # Separator class tolerates space, hyphen OR dot between groups — real forms
    # write "FR76 1027 …", "FR76-1027-…" and "FR76.1027.…"; the validator strips
    # the same separators before mod-97 (fix: recall LEAK 1).
    Recognizer("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ .\-]?[A-Z0-9]{2,4}){2,8}\b"),
               95, validator=_iban_valid, score_if_unvalidated=0.5),
    Recognizer("ISIN", re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b"),
               94, validator=_isin_valid, score_if_unvalidated=0.5),
    # SIRET: 14-digit company+établissement number (9-digit SIREN + 5-digit NIC).
    # Tolerates space OR hyphen between groups — real DCC forms use "123 456 789-00011"
    # (hyphen separates the NIC) which the previous space-only pattern missed, leaving
    # the 5-digit NIC suffix in clear after the SIREN was tokenised. Fix #259.
    Recognizer("SIRET", re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{3}[ -]?\d{5}\b"),
               93, validator=_siren_valid, score_if_unvalidated=0.5),
    Recognizer("SIREN", re.compile(r"\b\d{3}[ ]?\d{3}[ ]?\d{3}\b"),
               80, validator=_siren_valid, score_if_unvalidated=0.4),
    # French social security number (NIR): 13 digits + 2 key, sex 1/2 first.
    Recognizer("SECU", re.compile(r"\b[12][ ]?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?\d{3}[ ]?\d{3}(?:[ ]?\d{2})?\b"), 90),
    # French tax number (numéro fiscal / SPI): 13 digits, often "30 23 ...".
    Recognizer("NUM_FISCAL",
               re.compile(r"(?:num[ée]ro\s+fiscal|n[°o]\s*fiscal|r[ée]f[ée]rence\s+fiscale|SPI)\s*:?\s*(\d(?:[ .]?\d){12})",
                          re.I), 88, group=1),
    # UNLABELED numéro fiscal / référence d'avis (fix #319): the fiscal reference
    # printed on an avis d'impôt in its canonical grouping "NN NN NNNNNNN NN"
    # (2-2-7-2 = 13 digits) with NO "numéro fiscal :" label — e.g.
    # "Référence de l'avis : 25 92 0364665 70" or the bare "…numéro 25 92 0364665 70…".
    # The labeled NUM_FISCAL recognizer above misses these. PRECISION: this shape
    # is not extraordinarily rare, so we mask ONLY when the mod-97 control key
    # validates (_num_fiscal_ref_valid) — a checksum-failing lookalike (a stray
    # 13-digit run, a page/date group) is NOT emitted. Single-space separators only,
    # anchored on the exact 2-2-7-2 grouping. drop_if_unvalidated=True → a
    # checksum-failing span is dropped entirely, never masked (precision over recall).
    Recognizer("NUM_FISCAL",
               re.compile(r"\b\d{2}\s\d{2}\s\d{7}\s\d{2}\b"),
               87, validator=_num_fiscal_ref_valid, drop_if_unvalidated=True),
    # Télédéclarant alphanumeric block (fix #319): the identifier printed on some
    # fiscal forms in the grouping "NNN NN NN NNNNNNNNNN N A" (3-2-2-10-1 digits +
    # a trailing single letter) — e.g. "922 65 91 2768797789 3 A". No checksum is
    # published, so PRECISION rests entirely on the full structural anchor: the
    # exact digit-group counts AND the terminal single uppercase letter make this
    # shape (18 digits in 5 specific groups + a letter) collision-free against
    # dates, amounts, phones and generic refs (verified in the precision corpus).
    Recognizer("NUM_TELEDECLARANT",
               re.compile(r"\b\d{3}\s\d{2}\s\d{2}\s\d{10}\s\d\s[A-Z]\b"), 87),
    # Client number — O2S / dossier.
    # Client/dossier number. Tolerate an optional system label (e.g. "O2S")
    # between the cue and the value, and require the captured value to
    # contain a digit (so the label itself is never grabbed as the number).
    Recognizer("NUM_CLIENT",
               re.compile(r"(?:n[°o]\s*client|client\s+n[°o]|num[ée]ro\s+(?:de\s+)?client|n[°o]\s*dossier|r[ée]f[ée]rence\s+client)(?:\s+[A-Z][A-Z0-9]{1,4})?\s*:?\s*((?=(?:[\w\-]*\d){3})[A-Z0-9][\w\-]{2,})",
                          re.I), 86, group=1),
    # Date of birth — context-cued (high precision; we deliberately don't
    # redact every date, only the identifying one).
    # The n[eé](?:e|\(e\))? arm handles: "né le", "née le", "nee le" AND
    # the parenthetical form "Né(e) le" / "né(e) le" that appears in FR DCC
    # état-civil forms (the bare n[ée]e? pattern silently failed on it because
    # the literal parenthetical "(e)" broke the optional "e?"). Fix: #257-b.
    Recognizer("DATE_NAISSANCE",
               re.compile(r"(?:n[eé](?:e|\(e\))?\s+le|date\s+de\s+naissance|naissance)\s*:?\s*(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})",
                          re.I), 72, group=1),
    # French phone numbers.
    Recognizer("TEL", re.compile(r"(?:(?:\+33|0033)[ .\-]?[1-9]|0[1-9])(?:[ .\-]?\d{2}){4}\b"), 70),
    # Monetary amounts in euros.
    Recognizer("MONTANT",
               re.compile(r"\b\d{1,3}(?:[ . ]?\d{3})*(?:[.,]\d{1,2})?\s?(?:€|EUR\b|euros?\b)", re.I), 60),
    # — names / addresses (lower precision; bench measures recall) —
    Recognizer("ADRESSE",
               re.compile(rf"\b\d{{1,4}}(?:\s?(?:bis|ter|quater))?,?\s+{_VOIE}\s+[\wÀ-ÿ'’\-]+(?:\s+[\wÀ-ÿ'’\-]+){{0,4}}(?:,?\s+\d{{5}}\s+[A-ZÉÈ][\wÀ-ÿ'’\-]+(?:[ \-][A-ZÉÈ][\wÀ-ÿ'’\-]+)*)?",
                          re.I), 55, score_if_unvalidated=0.7),
    # NB: name words are joined by [^\S\n]+ (whitespace EXCEPT newline), not \s+,
    # so a name never spans a line break. In the detached KYC form layout a greedy
    # \s+ glued a client name on one value line onto the product/heading on the
    # next (a glued title+name+product span), which then polluted the allowlist, the
    # profile and the vault. Single-line names are the root fix (2026-06-01).
    Recognizer("NOM",
               re.compile(rf"\b{_TITRE}[^\S\n]+{_NAMEWORD}(?:[^\S\n]+{_NAMEWORD}){{0,3}}"), 50,
               score_if_unvalidated=0.8),
    # Untitled "Prénom Nom" via first-name gazetteer (the surname is the word(s) after).
    # Split #477: non-homograph forenames fire unconditionally (unchanged); the
    # small homograph subset (Robert, Colette…) additionally requires a civility
    # title cue earlier on the same line, or it's dropped (precision fix — over-
    # masking of common-word/brand homographs like "Le Petit Robert Illustré").
    Recognizer("NOM",
               re.compile(rf"\b(?:{_FIRST_PLAIN})[^\S\n]+{_NAMEWORD}(?:[^\S\n]+{_NAMEWORD}){{0,2}}"), 45,
               score_if_unvalidated=0.7),
    Recognizer("NOM",
               re.compile(rf"\b(?:{_FIRST_HOMOGRAPH})[^\S\n]+{_NAMEWORD}(?:[^\S\n]+{_NAMEWORD}){{0,2}}"), 45,
               score_if_unvalidated=0.7,
               context_validator=_homograph_title_nearby, drop_if_context_fails=True),
    # Job title / profession (lower priority than NOM so a name is typed as NOM,
    # not POSTE). Length-first overlap resolution still redacts the larger span.
    Recognizer("POSTE",
               re.compile(rf"\b{_ROLE}{_QUAL}{_COMP}", re.I), 40,
               score_if_unvalidated=0.65),
    # #678 — URL floor. URL masking was GLiNER-only (best-effort); a name in a URL
    # slug (LinkedIn /in/prenom-nom) leaks if the neural NER misses the link. This
    # conservative regex GUARANTEES the common shapes: an explicit scheme
    # (http/https/ftp), a www. prefix, or a bare host with a real TLD followed by a
    # /path (so a name-slug is captured). Deliberately narrow — a bare domain with
    # NO path (e.g. "exemple.fr" mid-sentence) is NOT matched, to avoid firing on
    # every dotted token. Priority 42 < EMAIL(100)/domains, so an email stays EMAIL.
    Recognizer("URL",
               re.compile(
                   r"\b(?:(?:https?|ftp)://|www\.)[^\s<>\"'`]+"          # scheme or www.
                   r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}/[^\s<>\"'`]+",      # host + /path
                   re.I),
               42, score_if_unvalidated=0.8),
]


def detect(text: str, recognizers: Optional[List[Recognizer]] = None) -> List[Match]:
    """Run all recognizers and return non-overlapping matches.

    Overlap resolution: keep the highest-priority match; ties broken by the
    longer span, then earliest start. This lets e.g. EMAIL win over a NOM
    that clipped part of the local-part, and SIRET (14) win over SIREN (9).
    """
    recs = recognizers if recognizers is not None else RECOGNIZERS
    raw: List[Match] = []
    for r in recs:
        raw.extend(r.find(text))
    return resolve_overlaps(raw)


def resolve_overlaps(raw: List[Match]) -> List[Match]:
    """Greedily keep non-overlapping matches from a raw (possibly mixed
    regex + NER) list. Shared by detect() and the engine's NER merge."""
    # Overlap resolution — LENGTH first, then score, then priority.
    #   - length-first: a short match (e.g. a phone) can never carve digits
    #     out of a longer structured PII it sits inside (e.g. an IBAN). We
    #     always cover the largest PII span — fail toward redaction, never
    #     toward a leak.
    #   - score then priority break ties on equal-length, same-span overlaps
    #     (e.g. a checksum-VALID ISIN 1.0 beats an INVALID IBAN 0.5 on the
    #     exact same 12 chars).
    raw.sort(key=lambda m: (-m.length, -m.score, -m.priority, m.start))
    accepted: List[Match] = []
    for m in raw:
        if any(not (m.end <= a.start or m.start >= a.end) for a in accepted):
            continue
        accepted.append(m)
    accepted.sort(key=lambda m: m.start)
    return accepted
