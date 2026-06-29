"""
candidate_sidecar.py — Phase 0 local candidate-signal writer (Tier-2 desktop app prep).

PURPOSE
-------
When the engine produces a sub-threshold or unsafe verdict, it knows WHICH tokens
were suspicious (entity_type, value, score, offset).  Today that information is
intentionally hidden from the agent-facing MCP output (the notice says only
"une donnée potentiellement sensible" without naming the value — that protection
stays intact).

Phase 0 exposes those candidate spans LOCALLY ONLY, writing them to a host-side
sidecar file under ~/.bubble_shield/candidates/<mission>.candidates.json.  A future
Phase-1 queue feeder will read this file to populate the HITL review queue.

WHAT IS A "CANDIDATE"
---------------------
A candidate span is any DetectedEntity that meets at least one criterion:
  - score < threshold  (low-confidence detection — the value WAS masked, but the
    model wasn't sure; these are the classic sub-threshold suspects).
  - unsafe residual flag — entity is part of a result where has_residual is True
    (PII left visible even after masking — this should never happen in normal flow
    but is surfaced here for completeness).

Candidates that WERE detected and masked but with low confidence are the primary
target: they represent name-shaped tokens the model barely caught — exactly the
signal the HITL queue needs.

SIDECAR FORMAT
--------------
~/.bubble_shield/candidates/<mission>.candidates.json
chmod 600 (same discipline as vaults and gazetteer).
Atomic write: tmp → rename (same discipline as gazetteer/vault).

Schema: a JSON list of candidate objects, APPENDED per call.  Each item:
  {
    "value":          "<real local string — host-only, never transmitted>",
    "normalized":     "<UPPER + NFD-strip-accents form for dedup>",
    "entity_type":    "<NOM | EMAIL | IBAN | …>",
    "score":          <float>,
    "threshold":      <float>,
    "char_start":     <int>,
    "char_end":       <int>,
    "source_doc":     "<basename of file, or '' for text-only calls>",
    "mission":        "<session mission id>",
    "is_residual":    <bool>,
    "ts":             "<ISO-8601 UTC timestamp>"
  }

SAFETY GUARANTEE
----------------
This module NEVER influences what the MCP tool returns to the agent.
The write is wrapped in try/except — ANY error is silently swallowed, so a
disk-full, permissions issue, or JSON error NEVER breaks or slows anonymization.

The values written here are the real PII strings.  This file is:
  - Host-side only (never transmitted, never returned to the agent).
  - Under ~/.bubble_shield/ (outside the repo, outside the Cowork VM).
  - chmod 600 on write.
  - Intended audience: the future Phase-1 desktop app feeder (running host-side).
"""
from __future__ import annotations

import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bubble_shield.engine import AnonymizationResult

BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
CANDIDATES_DIR = BUBBLE_SHIELD_HOME / "candidates"


def _normalize(value: str) -> str:
    """Uppercase + strip accents (NFD decompose → drop combining marks)."""
    nfd = unicodedata.normalize("NFD", value.upper())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _sidecar_path(mission: str) -> Path:
    """Return the sidecar path for a given mission.  Does NOT create dirs."""
    safe = "".join(c for c in mission if c.isalnum() or c in "-_.") or "default"
    return CANDIDATES_DIR / f"{safe}.candidates.json"


def _load_existing(path: Path) -> list:
    """Load existing candidates list from sidecar, returning [] on any error."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def write_candidates(
    result: "AnonymizationResult",
    mission: str = "mcp-session",
    source_doc: str = "",
) -> None:
    """Write candidate spans from *result* to the host-side sidecar file.

    Candidates are:
      - entities with score < result.threshold (sub-threshold — masked but uncertain)
      - all entities when result.has_residual (unsafe verdict — belt-and-suspenders)

    The MCP-facing output is NOT touched here.  Fail-open: any exception is swallowed.
    """
    try:
        _write_candidates_inner(result, mission=mission, source_doc=source_doc)
    except Exception:
        pass  # fail-open: sidecar write never breaks anonymization


def _write_candidates_inner(
    result: "AnonymizationResult",
    mission: str,
    source_doc: str,
) -> None:
    """Inner (may raise) — called exclusively from write_candidates's try/except."""
    from bubble_shield.engine import DetectedEntity  # local import to avoid circular

    # Determine which entities qualify as candidates.
    candidates: list[DetectedEntity] = []
    for entity in result.entities:
        if entity.score < result.threshold:
            candidates.append(entity)
        elif result.has_residual:
            # When the result is unsafe (residual PII visible), surface ALL entities
            # as candidates — we want the feeder to know everything that was detected.
            candidates.append(entity)

    if not candidates:
        return  # nothing to write — clean doc, no sidecar entry needed

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items = [
        {
            "value": e.value,
            "normalized": _normalize(e.value),
            "entity_type": e.entity_type,
            "score": round(e.score, 4),
            "threshold": result.threshold,
            "char_start": e.start,
            "char_end": e.end,
            "source_doc": source_doc,
            "mission": mission,
            "is_residual": result.has_residual,
            "ts": ts,
        }
        for e in candidates
    ]

    # Atomic write: load existing + append + tmp → rename.
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = _sidecar_path(mission)
    existing = _load_existing(path)
    merged = existing + new_items

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    # chmod 600 before rename so the file is never world-readable, even momentarily.
    tmp.chmod(0o600)
    os.replace(tmp, path)
