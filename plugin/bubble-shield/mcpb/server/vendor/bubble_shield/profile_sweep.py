"""
profile_sweep.py — self-improving second pass (Joris's idea, 2026-06-01).

THE IDEA
--------
Pass 1 (regex + GLiNER + structured) DISCOVERS the client's identifiers with high
confidence — full name, IBAN, e-mail, phone, passport, family-member names. But a
context-blind first pass MISSES *detached* references to those same people: a lone
a lone first name far from the full name, a surname in a relations table
, a name split across a form layout.

Pass 2 is a SELF-IMPROVING SWEEP: take the high-confidence entities found in pass 1,
build a "known client profile", then sweep the WHOLE document (and, across a
dossier, every other document of the same client) for any occurrence of those known
values OR their components (each token of a known full name). Recall-first: a missed
PII is the real risk, and a name we already KNOW belongs to the client is safe to
redact everywhere it appears.

Two compounding wins:
  1. Within a doc: catches detached / partial references the first pass missed.
  2. Across a dossier (8 docs of one client): discover the profile once, reuse it on
     all docs → near-total consistency AND a coherent vault (same token for the same
     person in every document).

Safety: we only ADD occurrences of values ALREADY validated as client PII in pass 1.
We never invent new identities. Name COMPONENTS are swept only if they're
"distinctive" (length ≥ 4, not a common word) to avoid redacting "de"/"la"/"louis"
as bare stopwords — though for surnames we accept the risk (recall-first).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Set

from bubble_shield.recognizers import Match

# Entity types whose VALUE we trust enough to sweep verbatim across the text.
_SWEEPABLE_TYPES = {
    "NOM", "EMAIL", "IBAN", "TEL", "PIECE_IDENTITE", "SECU", "NUM_FISCAL",
    "ADRESSE", "DATE_NAISSANCE", "LIEU_NAISSANCE",
}

# Name tokens we DON'T sweep as standalone components (particles, titles, fillers).
# Includes common FR-KYC FORM words that sit next to names in the detached layout
# ("Partenaire", "Conjoint", "Pleine propriété") so they're never learned/swept.
_NAME_STOPWORDS = {
    "de", "la", "le", "du", "des", "et", "von", "van", "el", "da", "di",
    "monsieur", "madame", "mlle", "m", "mme", "dr", "me", "saint",
    # form-layout fillers
    "partenaire", "conjoint", "conjointe", "pleine", "propriété", "usufruit",
    "vous", "votre", "fille", "fils", "enfant", "relation", "civilité",
    "nom", "prénom", "adresse", "profession", "société", "contrat", "compte",
    # financial-form value words that sit on name value-lines in KYC layouts
    "emprunt", "livret", "total", "traitements", "salaire", "revenu", "revenus",
    "patrimoine", "épargne", "epargne", "crédit", "credit", "assurance",
    "montant", "capital", "versement", "cotisation", "rente", "pension",
    # product / account / fund-type words observed gluing onto names in real
    # dossiers (the regex NOM over-extends onto the next product line)
    "europe", "fcpi", "lifinity", "lmnp", "ldds", "taxes", "impots", "impôts",
    "terres", "entrepreneurs", "financement", "emploi", "pret", "prêt",
    "cardif", "generali", "contrats", "comptes", "fonds", "parts", "scpi",
}


_CIVILITY = re.compile(r"\b(m|mr|mme|mlle|monsieur|madame|mademoiselle|me|dr)\b\.?",
                       re.IGNORECASE)


def _looks_like_person(value: str) -> bool:
    """Heuristic: does this NOM look like a real person (vs a fund/heading the
    regex NOM over-fired on)? True if it carries a civility title or contains a
    gazetteer first name. Used only to gate LOW-trust (regex) name learning."""
    if _CIVILITY.search(value):
        return True
    try:
        from bubble_shield.gazetteer import FRENCH_FIRST_NAMES
        fn = {n.lower() for n in FRENCH_FIRST_NAMES}
    except Exception:
        fn = set()
    return any(tok.lower() in fn for tok in re.split(r"[\s\-]+", value))


def _distinctive_tokens(value: str) -> List[str]:
    """Split a name into its distinctive (sweepable) component tokens.

    FORM-LAYOUT GUARD (real-data fix): the regex NOM over-extends across line
    breaks ("Monsieur\\nJeremy LOUIS\\nVotre conjoint", "M. NAME\\nContrat EUROPE"),
    mixing the real name with civility titles, the next product line, or trailing
    form words. We CAN'T just take the first line — the real name often sits on
    line 2 after a lone "Monsieur" label. Instead we flatten the lines and rely on
    the stopword list (titles, particles, form fillers AND product/heading words
    like "contrat"/"compte") to drop the noise. Cap the scan so a runaway
    multi-line match can't pull in a whole paragraph."""
    flat = re.sub(r"[\n\r]+", " ", value)
    toks = re.split(r"[\s\-]+", flat.strip())
    out = []
    for t in toks[:8]:
        tl = t.lower().strip(".,")
        if len(tl) >= 4 and tl not in _NAME_STOPWORDS:
            out.append(t.strip(".,"))
        if len(out) >= 6:
            break
    return out


@dataclass
class ClientProfile:
    """The known-PII set for one client, built from pass-1 detections and reused
    across all of that client's documents (dossier-level consistency)."""
    values: Set[str] = field(default_factory=set)         # exact known PII strings
    name_tokens: Set[str] = field(default_factory=set)    # distinctive name components
    type_of: Dict[str, str] = field(default_factory=dict) # value(lower) -> entity_type

    def learn(self, matches: Iterable[Match], min_score: float = 0.6,
              allowlist=None) -> None:
        """Absorb high-confidence pass-1 detections into the profile.

        CRITICAL (real-data bug, 2026-06-01): the NOM regex over-fires on fund
        names and form headings ("CARDIF", "Contrat", "FCPI"). If we learn those
        as client names we'd over-redact them across the whole dossier. So we:
          - drop anything the firm/fund ALLOWLIST matches (boilerplate, not client),
          - require NOM to look like a real person name (≥2 distinctive tokens, OR
            an explicit family-member label), not a single bare capitalised word.
        Structured PII (IBAN/EMAIL/TEL/passport) is trusted verbatim — checksums /
        format make it safe; only NAMES carry the pollution risk.

        fix (gliner-nom-span-dropped): soft ML detectors (GLiNER, OpenAI-PF) set
        priority=5 and are already pre-filtered by their own threshold before their
        spans reach the engine.  Their NOM predictions do NOT share the regex-NOM
        over-fire problem (a neural NER asked to find person names rarely returns
        "CARDIF" or "Contrat EUROPE").  Trust them regardless of score so we learn
        ALL-CAPS surname+forename blocks that appear in administrative-form fields
        with no civility title (e.g. "DUPONT  LUC" / "LEFEBVRE  HELENE") and
        sweep every occurrence in the document — including the separate address-block
        copy that GLiNER never saw because it was in a different chunk window.
        """
        for m in matches:
            if m.entity_type not in _SWEEPABLE_TYPES:
                continue
            if m.score < min_score:
                continue
            v = m.value.strip()
            if not v:
                continue
            if allowlist is not None and allowlist.is_allowlisted(v):
                continue  # firm / regulator / fund — never the client
            if m.entity_type == "NOM":
                # fix (gliner-nom-span-dropped): normalize internal whitespace and
                # strip newline-glued characters from soft-ML NOM values before
                # learning.  PDF extraction artifacts embed double-spaces and trailing
                # newline+char in GLiNER span boundaries (e.g. "DUPONT  MARC" or
                # "LEFEBVRE  CLAIRE\nO").  Normalising to a clean single-space string
                # ensures the sweep pattern and vault key match the clean document form.
                if m.priority <= 5:
                    # Soft ML span: collapse runs of whitespace (incl. newlines) to
                    # a single space, then trim.  Keep only the part before any newline
                    # that appears to be a glued-artifact (a lone char after \n is the
                    # start of the NEXT line captured by offset arithmetic errors).
                    v_parts = v.split("\n")
                    if len(v_parts) > 1 and len(v_parts[-1]) <= 2:
                        # trailing "O", "\nO", "\n3" etc. — the first non-whitespace
                        # character(s) of the next line; drop them.
                        v = " ".join(p.strip() for p in v_parts[:-1]).strip()
                    v = re.sub(r"[ \t]+", " ", v).strip()
                    if not v:
                        continue
                toks = _distinctive_tokens(v)
                if not toks:
                    continue
                # Pollution guard (real-data fix): the regex NOM fires on ANY
                # capitalised run, so "Contrat EUROPE" / "FCPI …" get mislabelled
                # NOM. Learning those as the client + sweeping them across the
                # dossier = catastrophic over-redaction. So a NOM is only learned
                # if it looks like a person (civility title / gazetteer first name)
                # OR a clean high-confidence source flagged it (score ≥ 0.85 — the
                # civility regex NOM is only 0.8).
                # fix (gliner-nom-span-dropped): also trust soft ML detectors
                # (priority ≤ 5 = GLiNER / OpenAI-PF) — they are neural NERs that
                # rarely over-fire on fund names; their pre-threshold filter is their
                # own confidence gate, not the 0.85 guard designed for regex NOM.
                trusted = _looks_like_person(v) or m.score >= 0.85 or m.priority <= 5
                if len(toks) < 2:
                    # A single bare token (a lone surname, or a heading like "Contrat") is only learned if
                    # it came from a TRUSTED source — a lone surname after "M." is
                    # real (a lone surname after a title); a lone heading word from the greedy regex is not.
                    if not trusted:
                        continue
                    if toks[0].lower() in _NAME_STOPWORDS:
                        continue
                elif not trusted:
                    continue
                self.values.add(v)
                self.type_of[v.lower()] = m.entity_type
                for tok in toks:
                    self.name_tokens.add(tok)
                    self.type_of[tok.lower()] = "NOM"
            else:
                self.values.add(v)
                self.type_of[v.lower()] = m.entity_type

    def sweep(self, text: str) -> List[Match]:
        """Find every occurrence in `text` of a known value or distinctive name
        token. Returns Match objects (recall-first; engine de-dups/overlaps)."""
        out: List[Match] = []
        targets = set(self.values) | set(self.name_tokens)
        for target in targets:
            if not target:
                continue
            etype = self.type_of.get(target.lower(), "NOM")
            # Word-boundary, case-insensitive. Whitespace in the target is made
            # flexible so a multi-word address matches across line breaks.
            pat = re.compile(
                r"\b" + r"\s+".join(re.escape(w) for w in target.split()) + r"\b",
                re.IGNORECASE,
            )
            for mm in pat.finditer(text):
                out.append(Match(start=mm.start(), end=mm.end(),
                                 entity_type=etype, value=mm.group(0),
                                 score=0.9, priority=40))  # known → confident, low prio
        return out


def two_pass_detect(text, base_engine, profile: ClientProfile | None = None,
                    allowlist=None):
    """Run pass-1 detection via base_engine, learn the profile, then sweep.

    Returns (result, profile). Pass an existing `profile` to carry knowledge
    across documents of the same dossier (cross-doc self-improvement). Pass the
    `allowlist` so the profile never learns firm/fund boilerplate as client PII."""
    profile = profile or ClientProfile()
    first = base_engine.anonymize(text)
    profile.learn(first.entities, allowlist=allowlist)
    # Re-detect with the profile sweep injected as an extra detector.
    sweep_detector = lambda t: profile.sweep(t)
    base_engine.extra_detectors = list(base_engine.extra_detectors) + [sweep_detector]
    try:
        second = base_engine.anonymize(text)
    finally:
        base_engine.extra_detectors = base_engine.extra_detectors[:-1]
    return second, profile
