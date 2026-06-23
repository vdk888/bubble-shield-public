"""custom_recognizers.py — load user-defined regex/checksum patterns from custom_fields.json.

Configuration is read from custom_fields.json (PII-free by construction — it
holds *patterns and category labels*, never PII instances). Path resolution:
  1. env BUBBLE_SHIELD_CUSTOM_FIELDS (explicit override)
  2. <vendor>/bubble_shield/custom_fields.json
  3. ~/.config/bubble_shield/custom_fields.json

Fail-soft throughout: a bad regex, missing file, or invalid entry is skipped
and logged; it never crashes the engine. The safe-by-default posture is
"skip anything dubious and keep running".
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

from bubble_shield.recognizers import Recognizer, _iban_valid, _isin_valid, _luhn_ok

logger = logging.getLogger(__name__)

# ── Named validator whitelist (never eval user code) ──────────────────────────
_VALIDATORS = {
    "luhn": _luhn_ok,
    "iban": _iban_valid,
    "isin": _isin_valid,
    "mod97": _iban_valid,
}

# ── ReDoS guard limits ────────────────────────────────────────────────────────
_MAX_PATTERN_LEN = 500
# Heuristic: reject nested quantifiers like (something+)+ or (.+)+
_REDOS_RE = re.compile(r'\([^)]*[+*][^)]*\)[+*{]')
# Probe string for quick compile+match test
_PROBE = "abc123XYZ"


def _is_safe_pattern(pattern: str) -> bool:
    """Return False if the pattern looks catastrophically backtracking (ReDoS)."""
    if len(pattern) > _MAX_PATTERN_LEN:
        logger.warning("custom_recognizers: pattern too long (%d > %d), skipped",
                       len(pattern), _MAX_PATTERN_LEN)
        return False
    if _REDOS_RE.search(pattern):
        logger.warning("custom_recognizers: nested quantifiers detected, skipped: %r",
                       pattern[:80])
        return False
    return True


def _config_locations() -> tuple:
    """Paths to search for custom_fields.json (in priority order)."""
    override = os.environ.get("BUBBLE_SHIELD_CUSTOM_FIELDS")
    if override:
        return (override,)
    # vendor dir = parent of this file
    vendor_path = Path(__file__).resolve().parent / "custom_fields.json"
    user_path = Path(os.path.expanduser("~/.config/bubble_shield/custom_fields.json"))
    return (str(vendor_path), str(user_path))


def load_custom_fields_config(path: Optional[str] = None) -> dict:
    """Return the parsed custom_fields.json or {} if not found/readable."""
    if path:
        locations = (path,)
    else:
        locations = _config_locations()
    for loc in locations:
        if not loc:
            continue
        p = Path(loc)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("custom_recognizers: could not parse %s: %s", loc, exc)
    return {}


def load_custom_recognizers(path: Optional[str] = None) -> List[Recognizer]:
    """Load user-defined regex recognizers from custom_fields.json.

    Returns an empty list if no config found or on any error (fail-soft).
    Each entry in cfg["regex_fields"] produces a Recognizer if valid.
    """
    cfg = load_custom_fields_config(path)
    entries = cfg.get("regex_fields", [])
    if not entries:
        return []

    recognizers: List[Recognizer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        # ── entity_type validation ────────────────────────────────────────────
        etype = entry.get("entity_type", "")
        if not etype or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", etype):
            logger.warning("custom_recognizers: invalid entity_type %r, skipped", etype)
            continue

        # ── pattern compilation with ReDoS guard ─────────────────────────────
        raw_pattern = entry.get("pattern", "")
        if not raw_pattern:
            logger.warning("custom_recognizers: empty pattern for %r, skipped", etype)
            continue
        if not _is_safe_pattern(raw_pattern):
            continue
        flags = re.I if entry.get("ignore_case") else 0
        try:
            compiled = re.compile(raw_pattern, flags)
        except re.error as exc:
            logger.warning("custom_recognizers: bad regex for %r: %s, skipped", etype, exc)
            continue

        # ── validator (named whitelist only) ──────────────────────────────────
        raw_validator = entry.get("validator")
        validator = None
        if raw_validator and raw_validator not in (None, "none", "null", ""):
            validator = _VALIDATORS.get(str(raw_validator).lower())
            if validator is None:
                logger.warning(
                    "custom_recognizers: unknown validator %r for %r, ignored (no eval)",
                    raw_validator, etype)

        # ── priority (clamped below the structured-PII floor of 100) ─────────
        try:
            priority = min(int(entry.get("priority", 65)), 99)
        except (TypeError, ValueError):
            priority = 65

        score_if_unvalidated = float(entry.get("score_if_unvalidated", 0.6))

        recognizers.append(Recognizer(
            entity_type=etype,
            pattern=compiled,
            priority=priority,
            validator=validator,
            score_if_unvalidated=score_if_unvalidated,
        ))
        logger.debug("custom_recognizers: loaded %r (priority=%d)", etype, priority)

    return recognizers
