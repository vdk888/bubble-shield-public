"""
known_pii_store.py — persistent cross-session known-PII gazetteer (Phase 1, local).

WHY THIS EXISTS
---------------
GLiNER and every NER model have a structural limitation: bare proper nouns with
no context (an isolated surname in a table cell, a first name in a header) score
below the detection threshold because there is nothing to triangulate on.  The
confidence-threshold problem cannot be solved by lowering the threshold (too many
false positives) or by swapping models (fastino, CamemBERT — both evaluated, neither
wins on this specific pathology: see eval/325-camembert-bare-name branch).

The structural answer — used by SOTA systems (gregmos/PII-Shield, Presidio) — is:
once a name is CONFIRMED as belonging to a real person, add it to a deny-list; every
subsequent occurrence is masked DETERMINISTICALLY, with zero dependence on NER score.
v1.18.x profile_sweep does this WITHIN one document; this module persists it ACROSS
documents and sessions.

STORAGE DESIGN
--------------
Location: ~/.bubble_shield/gazetteer/known_pii.json

- Outside the repo → never committed accidentally.
- JSON object: {"version": 1, "entries": [{"value": ..., "entity_type": ...,
  "added_at": ...}, ...]}
- chmod 600 on write (same discipline as the vault).
- File is gitignored by location (it's outside the repo); the pre-commit pii-guard
  hook provides a secondary safety net.

ANTI-POISONING (CRITICAL)
--------------------------
A self-perpetuating false positive is the main risk.  An entity may only enter the
gazetteer via one of two gates:

  Gate A — HIGH-CONFIDENCE AUTO: a detection that came from a source with
           priority <= 5 (soft-ML: GLiNER / OpenAI-PF) — these neural NERs rarely
           hallucinate person names — AND has a score >= HIGH_CONF_THRESHOLD (0.80).
           OR a regex/structured NOM that has score >= 0.85 (civility-title match,
           the most precise regex NOM pattern).

  Gate B — EXPLICIT ADD: a caller invokes add_confirmed_pii() directly. This is
           the HITL path — a human (or the engine, after a policy decision) explicitly
           confirms that a string is PII for this entity type. No confidence check:
           explicit adds are always trusted.

Low-confidence auto-detections (GLiNER score < 0.80, or regex NOM without civility)
do NOT auto-enter the gazetteer. They are profile_sweep territory (within-doc),
not cross-session storage.

ADD / REMOVE API
----------------
  add_confirmed_pii(value, entity_type, *, path=None)   — Gate B explicit add
  remove_pii(value, *, path=None) -> bool               — un-poison an entry
  is_known_pii(value, *, path=None) -> bool             — membership test
  load_gazetteer(path=None) -> GazetteeredPII            — load for the recognizer

The path= parameter exists purely for testing: tests pass a tmp file path so the
real ~/.bubble_shield/gazetteer is never touched.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# ── location ──────────────────────────────────────────────────────────────────

_DEFAULT_GAZETTEER_DIR = Path.home() / ".bubble_shield" / "gazetteer"
_DEFAULT_GAZETTEER_FILE = _DEFAULT_GAZETTEER_DIR / "known_pii.json"

# ── anti-poisoning thresholds ─────────────────────────────────────────────────

# Minimum score for a soft-ML detection (priority <= 5: GLiNER / OpenAI-PF)
# to auto-enter the gazetteer.
HIGH_CONF_ML_THRESHOLD: float = 0.80

# Minimum score for a regex-NOM detection to auto-enter the gazetteer.
# 0.85 corresponds to the civility-cued NOM regex (Recognizer score_if_unvalidated=0.8)
# but to be safe we require even higher — only truly high-confidence regex NOM events
# feed the cross-session store.
HIGH_CONF_REGEX_NOM_THRESHOLD: float = 0.85

# Priority boundary: detections with priority <= this are from soft-ML (GLiNER / OpenAI-PF).
SOFT_ML_PRIORITY_THRESHOLD: int = 5


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class PiiEntry:
    value: str
    entity_type: str
    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class GazetteeredPII:
    """The in-memory snapshot of the persisted known-PII store."""
    entries: List[PiiEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def values(self) -> List[str]:
        return [e.value for e in self.entries]

    def entity_type_of(self, value: str) -> str:
        """Case-insensitive lookup; returns 'NOM' if not found."""
        vl = value.lower()
        for e in self.entries:
            if e.value.lower() == vl:
                return e.entity_type
        return "NOM"

    def contains(self, value: str) -> bool:
        vl = value.lower()
        return any(e.value.lower() == vl for e in self.entries)


# ── persistence ───────────────────────────────────────────────────────────────

def _resolve_path(path: Optional[str | Path]) -> Path:
    if path is not None:
        return Path(path)
    return _DEFAULT_GAZETTEER_FILE


def _load_raw(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "entries": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entries": []}


def _save_raw(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_gazetteer(path: Optional[str | Path] = None) -> GazetteeredPII:
    """Load the persisted known-PII store.  Returns an empty GazetteeredPII if
    the file does not exist yet — no file = no-op for the recognizer."""
    p = _resolve_path(path)
    raw = _load_raw(p)
    entries = []
    for item in raw.get("entries", []):
        if isinstance(item, dict) and "value" in item and "entity_type" in item:
            entries.append(PiiEntry(
                value=item["value"],
                entity_type=item["entity_type"],
                added_at=item.get("added_at", ""),
            ))
    return GazetteeredPII(entries=entries)


# ── add / remove / query API ──────────────────────────────────────────────────

def add_confirmed_pii(
    value: str,
    entity_type: str,
    *,
    path: Optional[str | Path] = None,
) -> bool:
    """Gate B — explicit add.

    Add `value` as a confirmed PII of `entity_type` to the persistent
    gazetteer.  Idempotent (duplicate values are deduped case-insensitively).
    Returns True if a new entry was inserted, False if the value was already
    present.

    This is the HITL path.  Any caller that invokes this function is asserting
    human or policy-level confirmation that the value IS genuine PII — no
    further confidence check is applied.
    """
    if not value or not value.strip():
        return False
    p = _resolve_path(path)
    raw = _load_raw(p)
    vl = value.strip().lower()
    existing = [e for e in raw.get("entries", []) if e.get("value", "").lower() == vl]
    if existing:
        return False
    raw.setdefault("entries", []).append({
        "value": value.strip(),
        "entity_type": entity_type,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_raw(raw, p)
    return True


def maybe_add_detection(
    value: str,
    entity_type: str,
    score: float,
    priority: int,
    *,
    path: Optional[str | Path] = None,
) -> bool:
    """Gate A — high-confidence auto-add.

    Called by the engine after a detection to conditionally add to the
    gazetteer based on the anti-poisoning criteria:

    - Soft-ML source (priority <= SOFT_ML_PRIORITY_THRESHOLD, i.e. GLiNER /
      OpenAI-PF): requires score >= HIGH_CONF_ML_THRESHOLD (0.80).
    - Regex NOM (priority > SOFT_ML_PRIORITY_THRESHOLD): requires
      score >= HIGH_CONF_REGEX_NOM_THRESHOLD (0.85).

    Returns True if the value was added (or was already present).
    Returns False if the detection did not meet the anti-poisoning bar.
    """
    if entity_type != "NOM":
        # For now, the gazetteer is name-focused.  Structured PII (IBAN,
        # EMAIL, SECU) already has checksums and profile_sweep handles
        # within-doc repeats just fine. Extend later if needed.
        return False
    if priority <= SOFT_ML_PRIORITY_THRESHOLD:
        qualifies = score >= HIGH_CONF_ML_THRESHOLD
    else:
        qualifies = score >= HIGH_CONF_REGEX_NOM_THRESHOLD
    if not qualifies:
        return False
    return add_confirmed_pii(value, entity_type, path=path)


def remove_pii(
    value: str,
    *,
    path: Optional[str | Path] = None,
) -> bool:
    """Remove a value from the gazetteer (un-poison an entry).

    Case-insensitive match.  Returns True if the entry was found and removed,
    False if it was not present.  Idempotent and safe to call on non-existent
    values.
    """
    p = _resolve_path(path)
    raw = _load_raw(p)
    before = raw.get("entries", [])
    vl = value.strip().lower()
    after = [e for e in before if e.get("value", "").lower() != vl]
    if len(after) == len(before):
        return False
    raw["entries"] = after
    _save_raw(raw, p)
    return True


def is_known_pii(
    value: str,
    *,
    path: Optional[str | Path] = None,
) -> bool:
    """Return True if `value` is present in the gazetteer (case-insensitive)."""
    g = load_gazetteer(path=path)
    return g.contains(value)
