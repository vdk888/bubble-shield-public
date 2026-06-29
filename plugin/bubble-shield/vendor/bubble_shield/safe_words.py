# bubble_shield/safe_words.py
"""safe_words.py — self-improving "never mask these" list (#348 3c).

The symmetric OPPOSITE of the known-PII gazetteer. Fed by the reviewer un-hiding
a wrongly-masked word. The gazetteer (always-mask) ALWAYS takes precedence: a
value in the gazetteer is masked even if it's here (see the composed match_filter
ordering in bubble_shield_mcp.py:_composed_match_filter — the _safe_keep guard
checks is_known_pii FIRST and keeps the match masked when it fires).

Store: $BUBBLE_SHIELD_HOME/safe_words.json  {"version":1,"words":["..."]}
Atomic write (tmp->os.replace), chmod 600, corrupt JSON -> empty (fail toward masking).
Case-insensitive membership.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Iterable, List, Set


def _path() -> Path:
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
    return home / "safe_words.json"


def load_safe() -> Set[str]:
    p = _path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(w).strip().lower() for w in data.get("words", [])}
    except Exception:
        return set()  # corrupt -> empty -> fail toward masking


def _save(words: Set[str]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "words": sorted(words)}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)
    try:
        p.chmod(0o600)
    except OSError:
        pass


def is_safe(value: str) -> bool:
    return str(value).strip().lower() in load_safe()


def add_safe(value: str) -> None:
    w = load_safe()
    w.add(str(value).strip().lower())
    _save(w)


def remove_safe(value: str) -> bool:
    w = load_safe()
    v = str(value).strip().lower()
    if v not in w:
        return False
    w.discard(v)
    _save(w)
    return True


def filter_matches(matches: Iterable) -> List:
    """Drop NOM matches that are safe-listed. (Caller MUST run gazetteer masking
    first; gazetteer-sourced matches are not passed here, or win regardless.)"""
    out = []
    for m in matches:
        if getattr(m, "entity_type", "") == "NOM" and is_safe(getattr(m, "value", "")):
            continue
        out.append(m)
    return out
