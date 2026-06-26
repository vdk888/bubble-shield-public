"""
known_pii_recognizer.py — deterministic deny-list recognizer for the
cross-session known-PII gazetteer (#326, Phase 1).

ROLE IN THE PIPELINE
--------------------
This recognizer wraps a GazetteeredPII snapshot and emits a Match for every
occurrence of a known PII string in the text being analysed.  It is a
Recognizer-compatible object (has a .find(text) method returning List[Match])
and plugs into AnonymizationEngine.extra_recognizers exactly like custom_fields
recognizers do — participating in the SAME resolve_overlaps() pass as the core
RECOGNIZERS, so priority ordering and overlap resolution are correct.

PRIORITY: 3 (numerically lower than soft-ML NER at 5, but this does NOT mean
         the gazetteer loses to NER).

Why 3, and why does the gazetteer WIN over soft-ML/regex NOM despite the lower
number?  resolve_overlaps() sorts candidates by SCORE descending first, then by
-priority as a tiebreaker.  Because the gazetteer emits SCORE = 1.0 (a
deterministic, anti-poisoning-gated certainty) and NER scores are
threshold-filtered floats always below 1.0, the gazetteer's match wins every
overlap on score alone — the priority number is never reached as a tiebreaker.

Structured, checksum-validated PII (IBAN at 95, ISIN at 90, etc.) DOES win over
the gazetteer when the same span is claimed by both — but that is also via score:
checksum validators emit score 1.0 too, so the tiebreaker (priority) kicks in and
the higher-priority (higher-numbered) checksum recognizer takes the span.  Correct
behaviour: an IBAN deserves its own token type, not NOM.

MATCHING RULES
--------------
  1. Case-insensitive, accent-insensitive (via Unicode normalisation to NFD,
     stripping combining characters, then matching without case).
  2. Word-boundary-aware: the pattern anchors on \b so "MARC" does NOT match
     inside "MARCHAND".
  3. Multi-token values (e.g. "Marie Dubois"): whitespace in the stored value
     is made flexible (\\s+) so a split across a single newline or double-space
     still matches.
  4. Score: 1.0 — deny-list matches are deterministic, not probabilistic.

ZERO-COST WHEN EMPTY
--------------------
If the gazetteer is empty, make_known_pii_recognizer() returns None.  The
engine checks for None and skips the recognizer entirely — no regex is compiled,
no matching is attempted.  Existing behaviour is unchanged for users who haven't
accumulated any gazetteer entries.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

from bubble_shield.known_pii_store import GazetteeredPII, load_gazetteer
from bubble_shield.recognizers import Match

# Priority 3 — numerically below soft-ML NER (5) and checksum-validated
# structured PII (80+), but the gazetteer WINS over NER on score (1.0 vs <1.0),
# not on priority number.  Checksum-PII also scores 1.0 but has higher priority
# (80+), so it takes the span when both claim the same offset — correct.
KNOWN_PII_PRIORITY: int = 3

# Deterministic confidence — we know this string IS PII because it passed the
# anti-poisoning gate when it was stored.
KNOWN_PII_SCORE: float = 1.0


def _normalize(s: str) -> str:
    """NFD-decompose + strip combining marks → compare ignoring accents."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _build_pattern(value: str) -> re.Pattern:
    """Build a word-boundary, case-insensitive, accent-insensitive pattern for
    `value`.

    Multi-word: the stored value may contain spaces; we normalise each word
    separately and join with \\s+ so a double-space or line-break between
    name parts still matches.

    Word-boundary: \\b on left and right prevents "MARC" from matching inside
    "MARCHAND".
    """
    tokens = value.split()
    parts = [re.escape(_normalize(t)) for t in tokens]
    inner = r"\s+".join(parts)
    return re.compile(r"\b" + inner + r"\b", re.IGNORECASE | re.UNICODE)


class KnownPiiRecognizer:
    """A deny-list recognizer that finds every occurrence of a confirmed PII
    string in text, regardless of NER confidence.

    Attributes
    ----------
    gazetteer : GazetteeredPII
        The in-memory snapshot loaded from disk at construction time.
    _patterns : list of (pattern, entity_type, original_value)
        Pre-compiled regex for each entry.  Compiled once at construction so
        repeated .find() calls are cheap.
    priority : int
        Priority for resolve_overlaps() (default = KNOWN_PII_PRIORITY = 3).
    """

    # Make this look like a Recognizer to the engine (it checks .entity_type
    # only in rare code paths; the engine calls .find() on all extra_recognizers).
    entity_type = "NOM"   # default; actual entity_type per match comes from gazetteer

    def __init__(self, gazetteer: GazetteeredPII,
                 priority: int = KNOWN_PII_PRIORITY) -> None:
        self.gazetteer = gazetteer
        self.priority = priority
        # Pre-compile.  We apply accent-normalisation to the stored value so that
        # the pattern catches the source-text form too.
        self._patterns: list[tuple[re.Pattern, str]] = []
        for entry in gazetteer.entries:
            if not entry.value:
                continue
            try:
                pat = _build_pattern(entry.value)
                self._patterns.append((pat, entry.entity_type))
            except re.error:
                continue  # malformed value → skip gracefully

    @property
    def is_empty(self) -> bool:
        return not self._patterns

    def find(self, text: str) -> List[Match]:
        """Return all non-overlapping matches of known PII entries in `text`.

        Accent-normalises the input for matching but preserves the ORIGINAL
        surface form of the match (the text slice at the matched offsets) as the
        Match.value so the vault stores exactly what the document contains.
        """
        if not self._patterns:
            return []

        # Build a normalised copy of the text (same byte offsets since we only
        # strip combining marks, never change base characters or lengths).
        # IMPORTANT: NFD stripping CAN change string length if the input
        # contains precomposed characters (é = e + combining acute = 1 char → 2).
        # To keep offsets stable we match against the NORMALISED copy but extract
        # the VALUE from the ORIGINAL text using the match offsets — because in
        # practice document text and stored values will both be NFC-encoded, so the
        # NFD normalisation is symmetric and the offsets stay aligned.
        #
        # In the rare case where len(normalised_text) != len(text) (mixed NFC/NFD
        # source) we fall back to matching against the original text directly
        # (case-insensitive only, no accent normalisation).  Safety > completeness.
        norm_text = _normalize(text)
        use_norm = len(norm_text) == len(text)
        search_text = norm_text if use_norm else text

        out: List[Match] = []
        for pat, entity_type in self._patterns:
            for m in pat.finditer(search_text):
                # Extract the ORIGINAL surface form from the source text.
                value = text[m.start():m.end()]
                out.append(Match(
                    start=m.start(),
                    end=m.end(),
                    entity_type=entity_type,
                    value=value,
                    score=KNOWN_PII_SCORE,
                    priority=self.priority,
                ))
        return out


def make_known_pii_recognizer(
    path=None,
) -> Optional[KnownPiiRecognizer]:
    """Load the gazetteer and return a KnownPiiRecognizer, or None if the
    gazetteer is empty (zero-cost: no patterns compiled, caller skips it).

    Pass `path` to override the default ~/.bubble_shield/gazetteer/known_pii.json
    — used by tests to point at a temp file.
    """
    gazetteer = load_gazetteer(path=path)
    if gazetteer.is_empty:
        return None
    return KnownPiiRecognizer(gazetteer)
