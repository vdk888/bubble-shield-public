"""dashboard.py — turn the append-only audit log into risk-control stats.

This is the "contrôle des risques post-envoi" view: from the processing record
(bubble_shield/audit.py — counts & verdicts IN, raw values OUT), compute how the
anonymiser has been used over recent runs:

  - how many times it ran (webapp + the Cowork skill, which logs to the same file)
  - how many runs were flagged NOT safe to send (residual-PII risk)
  - the error runs (event == "error")
  - which entity types showed up and how often (so a human can sanity-check the
    policy: are € amounts being cloaked when they should be kept? are job titles
    slipping through?)

Pure logic, no web/PII dependency. The webapp route renders these; tests pin the
maths. We never surface a raw value — only counts, types, verdicts, timestamps.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping

# Verdict buckets we care about for risk control.
EVENT_ERROR = "error"


def _is_unsafe(entry: Mapping[str, Any]) -> bool:
    """A run is a risk if it processed entities but wasn't safe to send.

    `safe_to_send` is the engine's fail-closed verdict. We treat a missing key as
    unsafe (fail-closed in the dashboard too — never flatter an unknown run)."""
    if entry.get("event") == EVENT_ERROR:
        return False  # errors are counted separately, not as "unsafe sends"
    return not bool(entry.get("safe_to_send", False))


def summarize(entries: List[Mapping[str, Any]], *, recent: int = 50) -> Dict[str, Any]:
    """Compute risk-control stats over the audit entries.

    `recent` caps the "last N runs" detail list (newest first) so the page stays
    readable; the headline totals still cover everything passed in.
    """
    runs = [e for e in entries if e.get("event") != EVENT_ERROR]
    errors = [e for e in entries if e.get("event") == EVENT_ERROR]

    total_runs = len(runs)
    unsafe = [e for e in runs if _is_unsafe(e)]
    safe_runs = total_runs - len(unsafe)

    # Aggregate entity-type counts across all runs (what the engine cloaked).
    entity_totals: Counter = Counter()
    for e in runs:
        for etype, n in (e.get("counts") or {}).items():
            try:
                entity_totals[etype] += int(n)
            except (TypeError, ValueError):
                continue

    total_entities = sum(entity_totals.values())
    safe_rate = (safe_runs / total_runs) if total_runs else 0.0

    # Newest-first slice for the detail table.
    recent_runs = list(reversed(entries))[:recent]

    return {
        "total_runs": total_runs,
        "safe_runs": safe_runs,
        "unsafe_runs": len(unsafe),
        "error_runs": len(errors),
        "safe_rate": round(safe_rate, 3),
        "total_entities": total_entities,
        "entity_totals": dict(entity_totals.most_common()),
        "recent": recent_runs,
        "has_data": bool(entries),
    }
