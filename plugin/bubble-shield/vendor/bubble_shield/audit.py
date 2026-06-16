"""
audit.py — append-only processing record (RGPD art. 5-2 / 30, accountability).

This is the "registre des activités de traitement" in miniature: a local,
append-only log that proves WHAT was processed — which mission, what kind of
entities, how many of each, and the fail-closed verdict — WITHOUT ever storing
the PII values themselves.

    CRITICAL INVARIANT: counts & types IN, raw values OUT.

An audit log that leaked the names/emails/IBANs it audits would defeat the whole
privacy purpose. So an entry looks like

    {"timestamp": "2026-06-01T12:00:00Z", "mission": "dossier-x",
     "event": "anonymize", "counts": {"NOM": 4, "EMAIL": 2, "IBAN": 1},
     "total": 7, "safe_to_send": false}

— never a real name, e-mail or IBAN. The log is still a processing record, so we
keep the file private (0600) even though it holds no values.

The file is JSONL: one JSON object per line, opened in append ("a") mode only —
never rewritten — so the record is tamper-evident-by-convention and survives
concurrent appends without clobbering earlier entries.

Pure logic, no web dependency — the webapp, the dossier flow and the tests all
call into here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]


def _utc_now_iso() -> str:
    """ISO-8601 timestamp in UTC, with a trailing 'Z' (no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _ensure_private(path: Path) -> None:
    """Best-effort chmod 0600 — a processing record stays private even though it
    holds no PII values. POSIX-only; silently skipped where chmod is a no-op."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def append_entry(
    path: PathLike,
    *,
    mission: str,
    event: str,
    counts: Optional[Mapping[str, int]] = None,
    **metadata: Any,
) -> Dict[str, Any]:
    """Append ONE audit entry to the append-only JSONL log at `path`.

    Args:
        path:    log file (created if missing; parent dirs created too).
        mission: the mission / dossier name this processing belongs to.
        event:   processing event type, e.g. "anonymize", "dossier",
                 "forget", "purge".
        counts:  entity-type → count breakdown, e.g. {"NOM": 4, "EMAIL": 2}.
                 TYPES AND COUNTS ONLY — never a raw PII value.
        metadata: optional extra fields recorded verbatim, e.g.
                  total=7, safe_to_send=False, file_count=4.
                  Callers must pass counts/types here too, never raw values.

    Returns the entry dict that was written.

    The file is opened in append mode only — existing entries are never
    rewritten — and re-chmodded to 0600 after each write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    entry: Dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "mission": mission,
        "event": event,
        "counts": dict(counts or {}),
    }
    # Optional metadata (total, safe_to_send, file_count, …) recorded as-is.
    # `timestamp`/`mission`/`event`/`counts` are reserved and can't be shadowed.
    for key, value in metadata.items():
        if key not in entry:
            entry[key] = value

    # append-only: open in "a", write one compact JSON line, flush.
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _ensure_private(path)
    return entry


def counts_from_result(result: Any) -> Dict[str, int]:
    """Collapse an AnonymizationResult's `.entities` into a type → count map.

    Reads ONLY `entity.entity_type` from each detected entity — never the
    `.value`. The resulting dict is pure metadata (e.g. {"NOM": 4, "EMAIL": 2}),
    which is exactly what's safe to persist.
    """
    counts: Dict[str, int] = {}
    for ent in getattr(result, "entities", []) or []:
        etype = getattr(ent, "entity_type", None)
        if not etype:
            continue
        counts[etype] = counts.get(etype, 0) + 1
    return counts


def log_result(
    path: PathLike,
    result: Any,
    *,
    mission: str,
    event: str = "anonymize",
    **metadata: Any,
) -> Dict[str, Any]:
    """Build a type→count breakdown from an AnonymizationResult-like object and
    append it as an audit entry.

    `result` is any object exposing `.entities` (each with `.entity_type`),
    `.entity_count` and `.safe_to_send` — i.e. a `bubble_shield.engine.AnonymizationResult`.
    We record the per-type counts, the total entity count and the fail-closed
    `safe_to_send` verdict. We NEVER read `entity.value`, so no PII leaks in.

    Extra `metadata` (e.g. file_count for a dossier) is passed straight through.
    Returns the written entry dict.
    """
    counts = counts_from_result(result)
    meta: Dict[str, Any] = {
        "total": getattr(result, "entity_count", sum(counts.values())),
        "safe_to_send": bool(getattr(result, "safe_to_send", False)),
    }
    meta.update(metadata)
    return append_entry(path, mission=mission, event=event, counts=counts, **meta)


def read_audit(path: PathLike) -> List[Dict[str, Any]]:
    """Parse the JSONL audit log into a list of entries (oldest first).

    Returns [] if the file does not exist. Blank lines are skipped; a malformed
    line is skipped rather than aborting the whole read (the log is meant for
    display/inspection, so a single bad line never hides the rest).
    """
    path = Path(path)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries
