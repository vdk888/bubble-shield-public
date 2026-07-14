"""
bubble_shield/depollute_state.py — "already de-pollution-judged" memory.

WHY (2026-07-14): de-pollution now runs once per sweep (FIX 1). But
`depollute_gazetteer` re-judges the WHOLE gazetteer every run — each uncertain
NOM entry costs a ~6s Gemma call, EVERY sweep. On a 265-entry base that's ~11 min
per pass; on a client's large base it can exceed the 20-min sweep interval, so the
Mac grinds Gemma continuously.

A verdict is STABLE: a value Gemma judged NOM (real name, stays masked) is still a
name next sweep — re-judging it is pure waste. (A MOT verdict removes the value
from the gazetteer, so it never recurs anyway; only the NOM-kept entries pile up.)

Fix: remember which values were already judged and SKIP them next pass. First pass
judges the backlog (slow, once); every pass after judges only NEW entries (usually
zero → near-instant).

Store: `$BUBBLE_SHIELD_HOME/depollute_judged.json`
  {"version": 1, "judged": ["<sha256 of the value>", ...]}

We store SHA-256 HASHES of the values, never the raw values — this file must not
become another plaintext PII store. A hash is enough to answer "did we judge this
exact string before?" without holding the string.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Set


STATE_VERSION = 1


def _shield_home() -> Path:
    return Path(os.environ.get(
        "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))


def _state_path() -> Path:
    return _shield_home() / "depollute_judged.json"


def _h(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def load_judged() -> Set[str]:
    """The set of value-HASHES already de-pollution-judged. Best-effort: missing
    / unreadable / wrong-version → empty set (judge everything, correct but slow —
    fail-toward-doing-the-work, never fail-toward-skipping)."""
    try:
        p = _state_path()
        if not p.is_file():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return set()
        j = data.get("judged")
        return set(j) if isinstance(j, list) else set()
    except Exception:
        return set()


def was_judged(value: str, judged: Set[str] | None = None) -> bool:
    """True if this exact value was already judged. Pass a preloaded `judged` set
    (from load_judged) to avoid re-reading the file per value."""
    j = judged if judged is not None else load_judged()
    return _h(value) in j


def mark_judged(values: Iterable[str]) -> None:
    """Record `values` as judged (union into the store). Atomic write, chmod 600
    (hashes only, but keep the discipline). Best-effort: a write failure just
    means those values get re-judged next pass — wasteful, never wrong."""
    try:
        home = _shield_home()
        home.mkdir(parents=True, exist_ok=True)
        current = load_judged()
        current |= {_h(v) for v in values if v}
        payload = {"version": STATE_VERSION, "judged": sorted(current)}
        path = _state_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        pass


def forget(value: str) -> None:
    """Drop a value from the judged set so it will be RE-judged next pass. Used
    when a human overrides a verdict (confirm/dismiss) — the human changed the
    ground truth, so the old auto-verdict shouldn't be sticky-skipped."""
    try:
        current = load_judged()
        h = _h(value)
        if h not in current:
            return
        current.discard(h)
        home = _shield_home()
        home.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "judged": sorted(current)}
        path = _state_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        pass
