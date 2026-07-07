"""
review_queue.py — Phase 1: HITL review queue store + Phase-0 sidecar feeder.

PURPOSE
-------
Holds candidate PII items surfaced by the Phase-0 sidecar, dedup'd by
normalized token, pending human resolution via the future Phase-2/3 desktop UI.

DESIGN PRINCIPLES (from spec, non-negotiable)
---------------------------------------------
1. ADVISORY ONLY — the queue is not a safety gate.  Nothing here may slow,
   break, or change the agent-facing anonymize output.
2. STORE IS HOST-SIDE ONLY — real PII values live at
   ~/.bubble_shield/review_queue.json.  Never committed, chmod 600, same
   discipline as the vault and gazetteer.
3. FAIL-OPEN everywhere that touches the queue from live paths (feeder).
4. ATOMIC WRITES — tmp→chmod 600→os.replace, corrupt JSON → empty (graceful).

BOUNDING MECHANISMS (spec #1/#2/#3)
------------------------------------
#1 GAZETTEER-SKIP: if a token is ALREADY in the gazetteer → skip add_candidate
   (it will be masked deterministically; no review needed).
#1 CONFIRM DRAIN:  confirm() writes to the gazetteer → future occurrences are
   masked and will never re-queue (tested end-to-end in test suite).
#2 DEDUP:          N occurrences of the same normalized token across N docs →
   one queue item.  occurrence_count + doc_refs updated.
#3 BACKSTOP EXPIRE: expire_old(max_age_days) auto-dismisses stale pending
   items to the dismissed LOG (not deleted — auditable), with reason
   "auto-expired".  Auto-expire = "not PII" (most sub-threshold flags are
   false positives; default 30 days).

QUEUE FILE FORMAT
-----------------
~/.bubble_shield/review_queue.json

{
  "version": 1,
  "items": [
    {
      "normalized":       "<UPPER + strip-accents key>",
      "value":            "<real local string, host-only>",
      "entity_type":      "<NOM | EMAIL | ...>",
      "occurrence_count": <int>,
      "doc_refs":         ["<basename>", ...],
      "first_seen":       "<ISO-8601 UTC>",
      "last_seen":        "<ISO-8601 UTC>",
      "status":           "pending" | "confirmed" | "dismissed",
      "dismiss_reason":   "<str | null>"
    },
    ...
  ],
  "dismissed_log": [   ← confirmed/dismissed items move here (auditable, never deleted)
    { ...same shape + "resolved_at": "<ISO-8601 UTC>" },
    ...
  ]
}

WHEN THE FEEDER RUNS
--------------------
Design decision: the feeder (feed_from_sidecar) is a STANDALONE drain called
by the Phase-2/3 app on open (or on demand).  It is NOT wired into the live
_anonymise_text path.

Rationale:
  - The live anonymize path is fail-closed.  Wiring any extra code there,
    even fail-open, increases the surface for unexpected slowdowns or side
    effects (file I/O on every call).
  - The sidecar accumulates between app opens.  Draining on open is the right
    granularity for a human HITL loop (the client opens the app → sees what
    accumulated → resolves).
  - Simpler to test and verify in isolation.

A periodic background drain (e.g. a launchd timer) or an explicit CLI call
are also valid; they just call feed_from_sidecar(mission).
"""
from __future__ import annotations

import json
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bubble_shield.known_pii_store import add_confirmed_pii, is_known_pii

# ── location ──────────────────────────────────────────────────────────────────


def _shield_home() -> Path:
    """The Bubble Shield store root, resolved AT CALL TIME from BUBBLE_SHIELD_HOME.

    Resolving here (not at import) lets the autouse test fixture point the review
    queue at a per-test tmp dir, so a test can never write review_queue.json to
    the real ~/.bubble_shield/ store (#382)."""
    return Path(os.environ.get("BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))


# ── normalization (same algorithm as candidate_sidecar._normalize) ─────────────

def _normalize(value: str) -> str:
    """Uppercase + strip combining accents (NFD decompose, drop Mn category)."""
    nfd = unicodedata.normalize("NFD", value.upper())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


# ── path resolution ───────────────────────────────────────────────────────────

def _resolve_path(path: Optional[str | Path]) -> Path:
    if path is not None:
        return Path(path)
    return _shield_home() / "review_queue.json"


# ── UTC timestamp ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── raw load / save (atomic, corrupt-safe, chmod 600) ─────────────────────────

def _load_raw(path: Path) -> dict:
    """Load the queue JSON.  Returns an empty-but-valid dict on any error."""
    if not path.is_file():
        return {"version": 1, "items": [], "dismissed_log": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "items": [], "dismissed_log": []}
        data.setdefault("items", [])
        data.setdefault("dismissed_log", [])
        return data
    except Exception:
        return {"version": 1, "items": [], "dismissed_log": []}


def _save_raw(data: dict, path: Path) -> None:
    """Atomic write: tmp → chmod 600 → os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # chmod before rename so the file is never world-readable, even momentarily.
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def add_candidate(
    value: str,
    entity_type: str,
    doc: str,
    *,
    path: Optional[str | Path] = None,
    gaz_path: Optional[str | Path] = None,
) -> str | None:
    """Add a candidate value to the pending queue (or update if already pending).

    Bounding rule #1 GAZETTEER-SKIP:
        If `value` is ALREADY in the gazetteer → SKIP entirely.  It will be
        masked deterministically on the next doc; no human review needed.

        `gaz_path` lets a caller operating on a NON-default gazetteer (e.g.
        depollute_gazetteer, which un-masks a value from a custom gaz_path)
        make this check honor the SAME store it's operating on, instead of
        always reading the default gazetteer. When gaz_path is None (all
        existing callers), behavior is UNCHANGED — the check still reads the
        default gazetteer, exactly as before (#568 T5/T9 root-cause fix:
        the hardcoded path=None here silently dropped audit-log entries
        whenever a value un-masked from a non-default store also happened to
        exist in the default one).

    Bounding rule #2 DEDUP:
        If the normalized token is ALREADY a PENDING item → increment
        occurrence_count, append doc_ref (deduplicated), update last_seen.
        If NEW → create a pending item with occurrence_count=1.

    Items that are confirmed or dismissed are NOT updated — they have been
    resolved and should not reappear in the active queue.

    Returns the normalized key if the item was created or updated, None if
    it was skipped (already in gazetteer or already resolved).
    """
    if not value or not value.strip():
        return None

    normalized = _normalize(value.strip())

    # --- #1 GAZETTEER-SKIP ---------------------------------------------------
    if is_known_pii(value.strip(), path=gaz_path):
        # Already in the (caller-relevant) gazetteer: will be masked
        # deterministically.  Skip.
        return None

    p = _resolve_path(path)
    raw = _load_raw(p)
    now = _now_iso()

    # Search active items for matching normalized key.
    for item in raw["items"]:
        if item.get("normalized") == normalized:
            status = item.get("status", "pending")
            if status == "pending":
                # --- #2 DEDUP: update existing pending item -------------------
                item["occurrence_count"] = item.get("occurrence_count", 1) + 1
                refs = item.get("doc_refs", [])
                if doc and doc not in refs:
                    refs.append(doc)
                item["doc_refs"] = refs
                item["last_seen"] = now
                _save_raw(raw, p)
                return normalized
            else:
                # Already confirmed or dismissed — leave alone.
                return None

    # Check dismissed_log: a previously dismissed item should not be re-queued.
    for log_item in raw.get("dismissed_log", []):
        if log_item.get("normalized") == normalized:
            return None

    # New pending item.
    raw["items"].append({
        "normalized": normalized,
        "value": value.strip(),
        "entity_type": entity_type,
        "occurrence_count": 1,
        "doc_refs": [doc] if doc else [],
        "first_seen": now,
        "last_seen": now,
        "status": "pending",
        "dismiss_reason": None,
    })
    _save_raw(raw, p)
    return normalized


def confirm(
    normalized: str,
    *,
    path: Optional[str | Path] = None,
) -> Optional[str]:
    """HITL Gate B — human confirms the token IS genuine PII.

    Actions:
      1. Writes the value to the gazetteer via add_confirmed_pii (so future
         occurrences are masked deterministically and never re-queue — #1).
      2. Marks the item confirmed + moves it to the dismissed_log (auditable).
      3. Removes it from the active items list.

    Returns the real value string (so the caller can log/act), or None if the
    normalized key was not found in pending.
    """
    p = _resolve_path(path)
    raw = _load_raw(p)
    now = _now_iso()

    for i, item in enumerate(raw["items"]):
        if item.get("normalized") == normalized and item.get("status") == "pending":
            value = item["value"]
            entity_type = item.get("entity_type", "NOM")

            # Write to the gazetteer BEFORE modifying the queue so that even if
            # the queue save fails, the masking is in effect.
            add_confirmed_pii(value, entity_type)

            # Move to dismissed_log (confirmed, auditable).
            resolved = {**item, "status": "confirmed", "resolved_at": now}
            raw["dismissed_log"].append(resolved)
            raw["items"].pop(i)
            _save_raw(raw, p)
            return value

    return None


def dismiss(
    normalized: str,
    *,
    path: Optional[str | Path] = None,
    reason: str = "user-dismissed",
) -> bool:
    """Human dismisses the item — not PII, no action needed.

    Moves the item to the dismissed_log (auditable, NOT deleted).
    Returns True if found + dismissed, False if not found.
    """
    p = _resolve_path(path)
    raw = _load_raw(p)
    now = _now_iso()

    for i, item in enumerate(raw["items"]):
        if item.get("normalized") == normalized and item.get("status") == "pending":
            resolved = {
                **item,
                "status": "dismissed",
                "dismiss_reason": reason,
                "resolved_at": now,
            }
            raw["dismissed_log"].append(resolved)
            raw["items"].pop(i)
            _save_raw(raw, p)

            # #348 Task 4: a dismissed candidate is "not PII" → feed it to the
            # self-improving safe-list so it's never masked again. FAIL-OPEN: a
            # safe-list write failure must NEVER break the dismiss.
            try:
                from bubble_shield import safe_words as _sw
                _sw.add_safe(item.get("value", ""))
            except Exception:
                pass

            return True

    return False


def list_pending(
    *,
    path: Optional[str | Path] = None,
) -> list[dict]:
    """Return pending items, most-recurring first (occurrence_count desc)."""
    p = _resolve_path(path)
    raw = _load_raw(p)
    pending = [it for it in raw["items"] if it.get("status") == "pending"]
    return sorted(pending, key=lambda x: x.get("occurrence_count", 0), reverse=True)


def list_dismissed(
    *,
    path: Optional[str | Path] = None,
) -> list[dict]:
    """Return the full dismissed/confirmed audit log (never deleted)."""
    p = _resolve_path(path)
    raw = _load_raw(p)
    return list(raw.get("dismissed_log", []))


def expire_old(
    max_age_days: int = 30,
    *,
    path: Optional[str | Path] = None,
) -> int:
    """Bounding backstop #3: auto-dismiss pending items older than max_age_days.

    Any pending item whose first_seen is older than max_age_days is moved to the
    dismissed_log with status="dismissed" and reason="auto-expired".
    The item is NOT deleted — it stays auditable in the log.

    Returns the count of items that were auto-expired.
    """
    p = _resolve_path(path)
    raw = _load_raw(p)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    now_iso = _now_iso()

    expired_count = 0
    surviving = []
    for item in raw["items"]:
        if item.get("status") != "pending":
            surviving.append(item)
            continue
        first_seen_str = item.get("first_seen", "")
        try:
            first_seen = datetime.fromisoformat(first_seen_str)
        except (ValueError, TypeError):
            surviving.append(item)
            continue
        if first_seen < cutoff:
            resolved = {
                **item,
                "status": "dismissed",
                "dismiss_reason": "auto-expired",
                "resolved_at": now_iso,
            }
            raw["dismissed_log"].append(resolved)
            expired_count += 1
        else:
            surviving.append(item)

    raw["items"] = surviving
    _save_raw(raw, p)
    return expired_count


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT 5 — FEEDER
# Bridges Phase-0 sidecar → review queue.
#
# Design choice: standalone drain (not wired into the live anonymize path).
# See module docstring "WHEN THE FEEDER RUNS" for rationale.
# ═══════════════════════════════════════════════════════════════════════════════

def _candidates_dir() -> Path:
    """Return the candidates dir, respecting BUBBLE_SHIELD_HOME env override."""
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
    return home / "candidates"


def _sidecar_path_for(mission: str) -> Path:
    """Resolve sidecar path for a mission (mirrors candidate_sidecar logic)."""
    safe = "".join(c for c in mission if c.isalnum() or c in "-_.") or "default"
    return _candidates_dir() / f"{safe}.candidates.json"


def feed_from_sidecar(
    mission: str,
    *,
    path: Optional[str | Path] = None,
) -> int:
    """Drain the Phase-0 sidecar for `mission` into the review queue.

    Reads ~/.bubble_shield/candidates/<mission>.candidates.json and calls
    add_candidate() for each entry, applying dedup (#2) and gazetteer-skip (#1).

    FAIL-OPEN: any error (missing sidecar, corrupt JSON, I/O failure) is
    silently swallowed — this function never raises.

    Returns the count of candidates processed (0 on any error or empty sidecar).
    """
    try:
        return _feed_from_sidecar_inner(mission, path=path)
    except Exception:
        return 0


def feed_from_sidecar_all(
    *,
    path: Optional[str | Path] = None,
) -> int:
    """Drain EVERY candidate sidecar in the candidates dir into the review queue.

    In real use there are MANY missions / dossiers — the plugin writes
    sub-threshold candidates under whatever BUBBLE_SHIELD_SESSION was active
    (default 'mcp-session'), NOT 'demo'.  This globs every
    ~/.bubble_shield/candidates/*.candidates.json and drains each, so the
    reviewer sees pending candidates from ALL dossiers in one File de révision.

    Reuses feed_from_sidecar() per mission, so all existing dedup (#2) and
    gazetteer-skip (#1) bounding logic is preserved — nothing reinvented.

    FAIL-OPEN: any error (missing dir, corrupt sidecar) is swallowed; a bad
    sidecar for one mission must NOT prevent draining the others or break
    /review.

    Returns the total count of candidates processed across all sidecars.
    """
    try:
        cand_dir = _candidates_dir()
        if not cand_dir.is_dir():
            return 0
        total = 0
        for sidecar in sorted(cand_dir.glob("*.candidates.json")):
            mission = sidecar.name[: -len(".candidates.json")]
            # feed_from_sidecar is itself fail-open, so one bad sidecar can't
            # stop the loop over the remaining missions.
            total += feed_from_sidecar(mission, path=path)
        return total
    except Exception:
        return 0


def _feed_from_sidecar_inner(
    mission: str,
    *,
    path: Optional[str | Path] = None,
) -> int:
    """Inner (may raise) — called exclusively from feed_from_sidecar's try/except."""
    sidecar = _sidecar_path_for(mission)
    if not sidecar.is_file():
        return 0

    try:
        raw_text = sidecar.read_text(encoding="utf-8")
        candidates = json.loads(raw_text)
    except Exception:
        return 0

    if not isinstance(candidates, list):
        return 0

    count = 0
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value", "")
        entity_type = entry.get("entity_type", "NOM")
        doc = entry.get("source_doc", "")
        if not value:
            continue
        result = add_candidate(value, entity_type, doc, path=path)
        if result is not None:
            count += 1

    return count
