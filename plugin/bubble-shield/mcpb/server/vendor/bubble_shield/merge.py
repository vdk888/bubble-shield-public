"""
merge.py — Soft-span merger for "both" detector mode (Phase 2).

When detector.mode = "both", the daemon runs GLiNER + OpenAI Privacy Filter
and needs to deduplicate their outputs before returning {matches:[...]} to the
engine. The engine then merges the unified soft list with the regex/checksum
core via its own resolve_overlaps() — so this module only handles soft-vs-soft
deduplication.

MERGE RULE (per §B.4 of the scope):
  1. Compute pairwise character-span IoU between every GLiNER match and every
     OpenAI match.
  2. If IoU ≥ IOU_THRESHOLD (0.6) AND same canonical entity_type → merge into
     ONE match: union span (wider start/end), max(score), keep either match's
     entity_type (they agree). Result goes into the output once.
  3. If IoU ≥ IOU_THRESHOLD but DIFFERENT entity types → keep BOTH (let
     resolve_overlaps() arbitrate by length downstream; preserves recall).
  4. Any unmatched match from either source is kept as-is (recall-biased).

This is intentionally recall-biased: a missed PII is the real cost for a
privacy tool. The existing resolve_overlaps() in engine.py arbitrates final
overlaps against the regex core (checksum-valid IBAN etc. always win).

NOTE: This module does NOT replace resolve_overlaps. It only deduplicates
the two soft layers before they enter the engine.
"""
from __future__ import annotations

from typing import List, Tuple

from bubble_shield.recognizers import Match

IOU_THRESHOLD = 0.6


def _iou(a: Match, b: Match) -> float:
    """Intersection-over-Union of two character spans."""
    inter_start = max(a.start, b.start)
    inter_end = min(a.end, b.end)
    if inter_end <= inter_start:
        return 0.0
    intersection = inter_end - inter_start
    union = (a.end - a.start) + (b.end - b.start) - intersection
    if union <= 0:
        return 0.0
    return intersection / union


def _merge_pair(a: Match, b: Match) -> Match:
    """Merge two overlapping matches of the SAME entity_type into one.
    Union span, max score, priority=5 (soft layer).
    """
    return Match(
        start=min(a.start, b.start),
        end=max(a.end, b.end),
        entity_type=a.entity_type,  # same type guaranteed by caller
        value=a.value if len(a.value) >= len(b.value) else b.value,  # wider span text
        score=max(a.score, b.score),
        priority=5,
    )


def merge_soft(gliner: List[Match], openai: List[Match]) -> List[Match]:
    """Merge GLiNER + OpenAI soft matches per §B.4 recall-biased rule.

    Returns a deduplicated list ready to be fed to the engine as the combined
    soft-detector output. The HTTP response shape {matches:[...]} is UNCHANGED;
    the daemon serialises the result of this function.
    """
    if not gliner:
        return list(openai)
    if not openai:
        return list(gliner)

    # Track which matches have been consumed in a merge
    gliner_used = [False] * len(gliner)
    openai_used = [False] * len(openai)
    merged: List[Match] = []

    for gi, gm in enumerate(gliner):
        for oi, om in enumerate(openai):
            if openai_used[oi]:
                continue
            iou = _iou(gm, om)
            if iou < IOU_THRESHOLD:
                continue
            if gm.entity_type == om.entity_type:
                # Same type + high IoU → merge into one, recall-biased union span
                merged.append(_merge_pair(gm, om))
                gliner_used[gi] = True
                openai_used[oi] = True
                break  # one GLiNER match merges with at most one OpenAI match
            # else: different types, high IoU → keep both (handled below as unmatched)

    # Add all unmatched matches from both sources (recall-biased)
    for gi, gm in enumerate(gliner):
        if not gliner_used[gi]:
            merged.append(gm)
    for oi, om in enumerate(openai):
        if not openai_used[oi]:
            merged.append(om)

    return merged
