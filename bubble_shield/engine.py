"""
engine.py — orchestrate detect → anonymise / de-anonymise, with a
fail-closed residual-PII scan.

    result = engine.anonymize(text)        # text in the clear → tokenised
    result.anonymized                      # safe-to-send text
    result.safe_to_send                    # fail-closed verdict
    clear = engine.deanonymize(result.anonymized)   # tokens → real values

The same engine instance owns a Vault, so anonymise then de-anonymise round-
trips exactly. `safe_to_send` is False when (a) the residual scan still finds
PII-shaped strings in the anonymised text, (b) any accepted detection was
below the confidence threshold, or (c) a SUBSTANTIAL document yielded ZERO
detections — "found nothing" is not "safe", it's "nothing found", which on real
free text often means a name/address was MISSED (we'd rather over-flag than
falsely certify a leak as safe). See `verdict_state` for the canonical
per-surface state machine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from bubble_shield.recognizers import Match, Recognizer, detect, resolve_overlaps
from bubble_shield.vault import TOKEN_RE, Vault

# fix #273 — glued-token output normalisation.
# When a PDF extraction artifact omits a space between two adjacent tokens (e.g.
# a POSTE match immediately followed by the first letter of a surname), the
# anonymised output can contain "⟦POSTE_0003⟧ESURNAME" — the closing bracket of
# one token glued to the start of the next word.  Post-substitution we insert a
# single space between "⟧" and any immediately adjacent alphabetic character so
# that subsequent readers (human or automated) parse the token boundary correctly.
# This is a DISPLAY normalisation only — it does NOT change detection coverage
# (the detection pass already found the surname via the loose-left-boundary
# extension in structured_ext.doc_level_person_repetition_matches).
_GLUED_TOKEN_RE = re.compile(r"(⟧)([A-Za-z\xc0-\xff])")


@dataclass
class DetectedEntity:
    entity_type: str
    value: str
    token: str
    score: float
    start: int
    end: int
    # fix (gliner-nom-span-dropped): carry the source recognizer priority so that
    # profile_sweep.ClientProfile.learn() can distinguish soft-ML NOM detections
    # (priority ≤ 5, already threshold-filtered) from over-promiscuous regex NOM
    # (priority 45/50, needs the 0.85 score trust gate).  Default=0 is conservative
    # (treated as a high-priority source, i.e. trusted) for backward compatibility
    # with any callers that construct DetectedEntity directly without this field.
    priority: int = 0


# Zero-detection honesty (product-integrity fix, 2026-07-02).
# "Detected nothing" is NOT "safe" — on a real free-text document (especially in
# the regex-only / no-ML config) it very often means a name or address was simply
# MISSED. `safe_to_send` is computed by the same recognizers that missed the
# entity, so it structurally cannot catch its own false negatives. We therefore
# refuse to show a green "safe" verdict on a SUBSTANTIAL document where zero
# entities were found — the honest state is CAUTION (human review required),
# distinct from both the confident "everything found was masked" case and the
# trivially-empty "nothing to do" case (empty/whitespace/one-liner with no prose).
_WORD_RE = re.compile(r"\w+", re.UNICODE)
# A document is "substantial" (i.e. a zero-detection result is meaningfully
# suspicious) when it carries real prose. Threshold is deliberately low so short
# real KYC snippets still trip caution, but trivial inputs (an empty string, a
# whitespace blob, "ok", a bare date) do not. Tuned against the review battery:
# every leaking free-text doc there clears both bars; genuine no-op inputs do not.
_SUBSTANTIAL_MIN_WORDS = 8
_SUBSTANTIAL_MIN_CHARS = 40


@dataclass
class AnonymizationResult:
    original: str
    anonymized: str
    entities: List[DetectedEntity] = field(default_factory=list)
    residual: List[Match] = field(default_factory=list)   # PII still visible after
    min_score: float = 1.0
    threshold: float = 0.6

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    @property
    def has_residual(self) -> bool:
        return bool(self.residual)

    @property
    def low_confidence(self) -> bool:
        return self.entity_count > 0 and self.min_score < self.threshold

    @property
    def substantial_text(self) -> bool:
        """True when the source doc carries enough prose that finding ZERO
        identifying entities is meaningfully suspicious (vs. a trivially empty or
        one-word input where "nothing found" is genuinely "nothing to do")."""
        words = _WORD_RE.findall(self.original or "")
        return (len(words) >= _SUBSTANTIAL_MIN_WORDS
                and len((self.original or "").strip()) >= _SUBSTANTIAL_MIN_CHARS)

    @property
    def zero_detection(self) -> bool:
        """A substantial document in which the engine found and masked NOTHING.
        This is NOT "safe" — it's "nothing FOUND", which on free text very often
        means a name/address was MISSED. The caution state, not a green verdict."""
        return (self.entity_count == 0
                and not self.has_residual
                and self.substantial_text)

    @property
    def verdict_state(self) -> str:
        """Canonical, single source of truth for every human-facing surface.
        One of: 'leak' | 'low_confidence' | 'zero_detection' | 'nothing_to_do'
        | 'masked_ok'. Ordered by severity so the most serious state wins."""
        if self.has_residual:
            return "leak"
        if self.low_confidence:
            return "low_confidence"
        if self.zero_detection:
            return "zero_detection"
        if self.entity_count == 0:
            return "nothing_to_do"
        return "masked_ok"

    @property
    def safe_to_send(self) -> bool:
        """Fail-closed verdict. False when (a) residual PII remains, (b) a
        detection was sub-threshold, OR (c) a SUBSTANTIAL document yielded ZERO
        detections — because "found nothing" cannot be certified "safe" by the
        very recognizers that would have missed a name. A True here means the
        closest we honestly get to safe: things WERE found and all were masked
        (or the input carried no real prose to worry about) — still 'revue
        conseillée', never an absolute guarantee. See `verdict_state` /
        `verdict_fr` for the human-facing distinction; consumers that only see a
        bool must treat True as 'no red flag' and NEVER render '✓ sûr' text off
        it alone (a zero-detection doc is correctly False, so the boolean is a
        safe gate for callers that ignore the richer state)."""
        return (not self.has_residual
                and not self.low_confidence
                and not self.zero_detection)

    @property
    def verdict_fr(self) -> str:
        state = self.verdict_state
        if state == "leak":
            return "⚠️ PII résiduelle détectée — NE PAS envoyer"
        if state == "low_confidence":
            return "⚠️ Détection peu fiable sous le seuil — à revoir avant envoi"
        if state == "zero_detection":
            # THE FIX: never a green "safe" on a substantial zero-detection doc.
            return ("⚠️ Aucune donnée identifiante détectée — cela ne garantit PAS "
                    "l'absence de PII. Une relecture humaine est requise avant envoi.")
        if state == "nothing_to_do":
            return "✓ Aucune donnée identifiante détectée — rien à anonymiser"
        return "✓ Données identifiantes masquées — revue conseillée avant envoi"


class AnonymizationEngine:
    def __init__(
        self,
        vault: Optional[Vault] = None,
        recognizers: Optional[List[Recognizer]] = None,
        threshold: float = 0.6,
        use_ner: bool = False,
        use_llm: bool = False,
        extra_detectors: Optional[List[Callable[[str], List[Match]]]] = None,
        extra_recognizers: Optional[List[Recognizer]] = None,
        match_filter: Optional[Callable[[List[Match]], List[Match]]] = None,
        context_boost: bool = True,
    ) -> None:
        self.vault = vault if vault is not None else Vault()
        self.recognizers = recognizers
        self.threshold = threshold
        # Context-word confidence boosting (Presidio-inspired). On by default;
        # only ever RAISES scores of already-detected spans near PII cue words.
        self.context_boost = context_boost
        # Optional post-detection filter: receives the resolved match list and
        # returns the subset to actually anonymise. The firm/regulator allowlist
        # plugs in here to DROP "not the client" detections (firm boilerplate)
        # before substitution — the precision half of the client-vs-firm problem.
        self.match_filter = match_filter
        # Optional detection layers, both OFF by default and both fail-open to
        # the pure-regex build (a no-op with zero cost when their backend
        # isn't present):
        #   use_ner  → Presidio/spaCy NER for names/locations (presidio_ext)
        #   use_llm  → a local LLM via Ollama for prose PII (llm_ext)
        # `extra_detectors` lets a caller plug any text→[Match] function in too
        # (e.g. a domain gazetteer). Every layer is merged with the regex
        # matches and overlaps are resolved together, so a checksum-validated
        # structured PII always wins over a soft ML/LLM guess on the same span.
        self.use_ner = use_ner
        self.use_llm = use_llm
        self.extra_detectors: List[Callable[[str], List[Match]]] = list(
            extra_detectors or [])
        # User-defined regex Recognizer objects (custom PII field patterns from
        # custom_fields.json). These participate in the SAME resolve_overlaps()
        # pass as the core RECOGNIZERS, so a custom pattern never steals a span
        # from a higher-priority checksum-valid IBAN/ISIN (correct behaviour).
        self.extra_recognizers: List[Recognizer] = list(extra_recognizers or [])

    def _extra_matches(self, text: str) -> List[Match]:
        extra: List[Match] = []
        if self.use_ner:
            from bubble_shield import presidio_ext
            extra.extend(presidio_ext.ner_matches(text))
        if self.use_llm:
            from bubble_shield import llm_ext
            extra.extend(llm_ext.llm_matches(text))
        for detector in self.extra_detectors:
            try:
                extra.extend(detector(text))
            except Exception:    # a flaky optional layer never breaks anonymisation
                continue
        return extra

    def _recognizer_list(self) -> List[Recognizer]:
        """Return the full recognizer list: core + user-defined custom fields."""
        from bubble_shield.recognizers import RECOGNIZERS
        recs = list(self.recognizers if self.recognizers is not None else RECOGNIZERS)
        recs.extend(self.extra_recognizers)
        return recs

    def _detect(self, text: str) -> List[Match]:
        extra = self._extra_matches(text)
        recs = self._recognizer_list()
        if not extra and not self.extra_recognizers:
            # Fast path: no extras at all — delegate to detect() directly.
            return detect(text, self.recognizers)
        # Merge regex (incl. custom) + extra-layer raw matches, then resolve
        # overlaps together so a validated structured PII still wins over a soft
        # name guess AND a custom pattern that overlaps a core IBAN loses to the
        # checksum-valid IBAN (correct: FR-finance is source of truth).
        raw: List[Match] = []
        for r in recs:
            raw.extend(r.find(text))
        raw.extend(extra)
        resolved = resolve_overlaps(raw)

        # fix (gliner-nom-span-dropped): soft-ML NOM sweep pass.
        #
        # A neural NER (GLiNER, OpenAI-PF, priority=5) may detect a name at one
        # location in the document but miss a SECOND occurrence that has different
        # surrounding whitespace or falls in a different chunk window.  The resolved
        # match list at this point covers the detected offset but NOT the duplicate.
        #
        # This pass builds a mini ClientProfile from soft-ML NOM spans (priority ≤ 5),
        # sweeps the text for uncovered occurrences, and adds them to the raw list
        # before a second resolve_overlaps call.  It is:
        #   - ADD-ONLY: only new spans (not already covered) are appended.
        #   - RECALL-BIASED: same philosophy as doc_level_person_repetition_matches.
        #   - CHEAP: sweep runs in O(n * m) where n = text length and m = name tokens,
        #     both small for real KYC documents.
        #   - SAFE: the profile.learn() call uses the same trust gate as two_pass_detect;
        #     it refuses to learn common-word NOM tokens from low-confidence regex NOM
        #     (those have priority 45-50, above the soft-ML ≤5 gate).
        soft_ml_noms = [m for m in resolved
                        if m.entity_type == "NOM" and m.priority <= 5]
        if soft_ml_noms:
            try:
                from bubble_shield.profile_sweep import ClientProfile
                profile = ClientProfile()
                profile.learn(soft_ml_noms, min_score=0.0)  # threshold already applied by detector
                sweep_matches = profile.sweep(text)
                if sweep_matches:
                    # Add sweep hits that are not already covered and re-resolve.
                    raw2 = list(resolved) + sweep_matches
                    resolved = resolve_overlaps(raw2)
            except Exception:
                pass  # fail-open: sweep failure never breaks anonymisation

        return resolved

    def anonymize(self, text: str) -> AnonymizationResult:
        matches = self._detect(text)
        # Context-word boosting: a detection near a PII cue ("Client:", "né le",
        # "demeurant"…) is more likely real → raise its confidence so a genuine
        # low-score name crosses the fail-closed threshold, while isolated form-
        # label guesses stay low. Only raises scores; never adds/removes spans.
        if self.context_boost:
            try:
                from bubble_shield.context_boost import boost_by_context
                matches = boost_by_context(text, matches)
            except Exception:
                pass
        if self.match_filter is not None:
            try:
                matches = self.match_filter(matches)
            except Exception:   # a flaky filter never breaks anonymisation
                pass
        # Replace from the end so earlier spans keep their offsets.
        out = text
        entities: List[DetectedEntity] = []
        min_score = 1.0
        for m in sorted(matches, key=lambda x: x.start, reverse=True):
            token = self.vault.token_for(m.value, m.entity_type)
            out = out[:m.start] + token + out[m.end:]
            entities.append(DetectedEntity(
                entity_type=m.entity_type, value=m.value, token=token,
                score=m.score, start=m.start, end=m.end,
                priority=m.priority))
            min_score = min(min_score, m.score)
        entities.sort(key=lambda e: e.start)

        # fix #273 — insert a space between ⟧ and any immediately adjacent
        # alphabetic char (PDF glued-token output normalisation).
        out = _GLUED_TOKEN_RE.sub(r"\1 \2", out)

        # CONSISTENCY (#589-E, 2026-07-15): the residual scan runs the SAME detector
        # as masking, but on the OUTPUT — which the mask step rearranged (token
        # replacement shifts offsets; the #273 glue-fix inserts spaces). That
        # rearrangement can make a real PII value MATCHABLE on the output that was
        # not matchable on the input (e.g. a SIREN whose grouping only becomes a
        # clean `\d{3} \d{3} \d{3}` after an adjacent token fixed the spacing). The
        # OLD behaviour just REPORTED that as "residual → fail-closed", stranding a
        # doc for a leak it could actually mask. The consistent fix: if the residual
        # scan finds maskable PII, MASK IT (same vault) and re-scan — so everything
        # the detector sees gets masked, whether found in pass 1 or on the output.
        # Only PII that genuinely SURVIVES masking stays residual (a real fail-close).
        # Bounded loop (rearrangement can reveal a new match once more); 3 iters is
        # ample and guarantees termination.
        for _ in range(3):
            residual = self._residual_scan(out)
            if not residual:
                break
            applied_any = False
            for m in sorted(residual, key=lambda x: x.start, reverse=True):
                # Only mask a residual we can cleanly locate + tokenise; a match we
                # can't apply (shouldn't happen post-scan) is left as a real residual.
                if not (m.value and 0 <= m.start < m.end <= len(out)):
                    continue
                token = self.vault.token_for(m.value, m.entity_type)
                out = out[:m.start] + token + out[m.end:]
                entities.append(DetectedEntity(
                    entity_type=m.entity_type, value=m.value, token=token,
                    score=m.score, start=m.start, end=m.end, priority=m.priority))
                min_score = min(min_score, m.score)
                applied_any = True
            out = _GLUED_TOKEN_RE.sub(r"\1 \2", out)
            if not applied_any:
                # residual found but none were maskable → a genuine leak. Stop and
                # let it be reported (fail-closed) rather than loop forever.
                break
        else:
            # loop exhausted without going empty → final scan is authoritative.
            residual = self._residual_scan(out)
        entities.sort(key=lambda e: e.start)
        return AnonymizationResult(
            original=text, anonymized=out, entities=entities,
            residual=residual, min_score=min_score if entities else 1.0,
            threshold=self.threshold)

    def deanonymize(self, text: str) -> str:
        """Restore real values from the vault (tokens → clear text)."""
        return self.vault.restore(text)

    def _residual_scan(self, anonymized: str) -> List[Match]:
        """Re-run detection on the anonymised text; anything still matching
        (that isn't one of our own tokens) is residual PII — a leak risk.

        CRITICAL: apply the SAME match_filter (the firm/regulator allowlist) the
        main pipeline uses. An allowlisted entity left in clear is INTENTIONAL
        (the advisory firm's own people/address are not client PII), so it must
        NOT be reported as residual — otherwise the fail-closed verdict says
        "ne pas envoyer" for a document that is actually safe to send, because
        the two code paths disagreed. (Real-data bug, 2026-06-01: an advisor's
        name + their firm-domain e-mail tripped the verdict.)

        Also includes custom recognizers so a custom pattern that was detected in
        the main pass is also checked for residual — consistent detection.

        CONSISTENCY FIX (#589-E, 2026-07-15): the residual scan MUST use the EXACT
        same detection pipeline as the masking pass — `self._detect` — not a
        bare-regex `detect(anonymized, self._recognizer_list())`. They diverged:
        the masking pass runs regex + extra-layer (GLiNER/neural) + `resolve_overlaps`
        + the soft-ML NOM sweep, while the old residual scan ran regex ONLY, with no
        overlap resolution. So a match the masking pass's `resolve_overlaps` had
        ABSORBED (a lower-priority overlap losing to a higher one) could reappear as
        raw-regex "residual" on the shifted post-mask text — a structured form then
        FAIL-CLOSED for a "leak" its OWN masker never considered a distinct entity
        (observed live: a mangled-spacing SIREN on a liasse the masker didn't detect
        as SIREN, so it couldn't mask it, yet the raw-regex re-scan flagged it →
        stuck at 96% forever). By running `self._detect` here, the residual scan can
        only flag what the masker's own full detector ALSO sees on the output: if the
        masker missed it (couldn't detect → couldn't mask), the residual scan misses
        it too — no more phantom fail-closes from detector disagreement. A REAL leak
        (the masker DID detect a value but a bug left it in clear) is still caught,
        because `_detect` on the output would re-detect that same value.
        """
        leftover: List[Match] = []
        for m in self._detect(anonymized):
            # Ignore matches that fall entirely inside one of our tokens
            # (e.g. a recognizer firing on the digits of ⟦IBAN_0001⟧).
            if any(t.start() <= m.start and m.end <= t.end()
                   for t in TOKEN_RE.finditer(anonymized)):
                continue
            leftover.append(m)
        if self.match_filter is not None:
            try:
                leftover = self.match_filter(leftover)
            except Exception:
                pass
        return leftover
