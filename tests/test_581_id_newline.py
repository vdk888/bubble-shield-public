"""
tests/test_581_id_newline.py — ID-value regex must NOT cross a newline (#581).

The #577 LIEU_NAISSANCE bench surfaced 8 mis-tags: structured_ext's ID-value classes
used `\\s` (which INCLUDES `\\n`), so a PIECE_IDENTITE/ID number pattern crossed a line
break and swallowed a SIRET-shaped run + adjacent LIEU content onto the next line —
hurting LIEU_NAISSANCE precision. An ID number never spans a line break (same class as
the NOM/`_SP` line-break fix). Fix: intra-line whitespace only (space+tab), never `\\n`.

Synthetic data only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugin" / "bubble-shield" / "vendor"))

from bubble_shield import structured_ext as se  # noqa: E402


def test_id_value_does_not_cross_newline():
    rx = re.compile(se._ID_VALUE)
    m = rx.search("AB12\n34567CDE")
    assert m is None or "\n" not in m.group(0), \
        "the ID value must NOT match across a newline (was the #581 mis-tag)"


def test_same_line_id_still_matches():
    rx = re.compile(se._ID_VALUE)
    m = rx.search("AB12 34567 CDE")
    assert m is not None and m.group(0) == "AB12 34567 CDE", \
        "a same-line ID number must still match fully (no recall regression)"


def test_no_id_class_uses_bare_backslash_s():
    """Guard the whole file: no ID-value char class may use bare `\\s` (crosses
    newlines). Every intra-line whitespace must be the explicit ` \\t`."""
    src = (REPO / "plugin" / "bubble-shield" / "vendor" / "bubble_shield"
           / "structured_ext.py").read_text()
    assert r"[A-Z0-9\s" not in src, \
        "an ID char class still uses \\s (newline-crossing) — use [A-Z0-9 \\t...] instead"


def test_split_siret_not_captured_as_one_id():
    """The incident shape: a SIRET-shaped digit run split across a line break must NOT
    be captured as a single ID/value spanning both lines."""
    rx = re.compile(se._ID_VALUE)
    # "123 456 789" on line 1, "00011 PARIS" on line 2 — a real form layout
    doc = "123 456 789\n00011 PARIS"
    for m in rx.finditer(doc):
        assert "\n" not in m.group(0), \
            f"a value spanned the line break: {m.group(0)!r}"
