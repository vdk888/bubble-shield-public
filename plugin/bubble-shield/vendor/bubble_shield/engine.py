"""
engine.py — orchestrate detect → anonymise / de-anonymise, with a
fail-closed residual-PII scan.

    result = engine.anonymize(text)        # text in the clear → tokenised
    result.anonymized                      # safe-to-send text
    result.safe_to_send                    # fail-closed verdict
    clear = engine.deanonymize(result.anonymized)   # tokens → real values

The same engine instance owns a Vault, so anonymise then de-anonymise round-
trips exactly. `safe_to_send` is False when (a) the residual scan still finds
PII-shaped strings in the anonymised text, or (b) any accepted detection was
below the confidence threshold (we'd rather over-flag than leak).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from bubble_shield.recognizers import Match, Recognizer, detect, resolve_overlaps
from bubble_shield.vault import TOKEN_RE, Vault


@dataclass
class DetectedEntity:
    entity_type: str
    value: str
    token: str
    score: float
    start: int
    end: int


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
    def safe_to_send(self) -> bool:
        """Fail-closed verdict: no residual PII AND no sub-threshold detection."""
        return not self.has_residual and not self.low_confidence

    @property
    def verdict_fr(self) -> str:
        if self.has_residual:
            return "⚠️ PII résiduelle détectée — NE PAS envoyer"
        if self.low_confidence:
            return "⚠️ Détection peu fiable sous le seuil — à revoir avant envoi"
        if self.entity_count == 0:
            return "✓ Aucune PII détectée — rien à anonymiser"
        return "✓ Aucune PII résiduelle — sûr à envoyer"


class AnonymizationEngine:
    def __init__(
        self,
        vault: Optional[Vault] = None,
        recognizers: Optional[List[Recognizer]] = None,
        threshold: float = 0.6,
        use_ner: bool = False,
        use_llm: bool = False,
        extra_detectors: Optional[List[Callable[[str], List[Match]]]] = None,
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

    def _detect(self, text: str) -> List[Match]:
        extra = self._extra_matches(text)
        if not extra:
            return detect(text, self.recognizers)
        # Merge regex + extra-layer raw matches, then resolve overlaps together
        # so a validated structured PII still wins over a soft name guess.
        from bubble_shield.recognizers import RECOGNIZERS
        recs = self.recognizers if self.recognizers is not None else RECOGNIZERS
        raw: List[Match] = []
        for r in recs:
            raw.extend(r.find(text))
        raw.extend(extra)
        return resolve_overlaps(raw)

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
                score=m.score, start=m.start, end=m.end))
            min_score = min(min_score, m.score)
        entities.sort(key=lambda e: e.start)

        residual = self._residual_scan(out)
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
        """
        leftover: List[Match] = []
        for m in detect(anonymized, self.recognizers):
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
