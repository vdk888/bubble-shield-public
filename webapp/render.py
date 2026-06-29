"""
render.py — turn an AnonymizationResult into highlighted HTML for the demo.

Two views, side by side:
  - BEFORE: the clear text with each detected PII span highlighted by type.
  - AFTER: the anonymised text with each ⟦TYPE_n⟧ token highlighted.

All escaping is done here; templates mark the output |safe.
"""
from __future__ import annotations

import html
from typing import List

from bubble_shield.engine import AnonymizationResult
from bubble_shield.vault import TOKEN_RE


def _span(text: str, css: str, title: str = "") -> str:
    t = f' title="{html.escape(title, quote=True)}"' if title else ""
    return f'<mark class="pii pii--{css}"{t}>{html.escape(text)}</mark>'


def highlight_before(result: AnonymizationResult) -> str:
    """Clear text with detected PII spans wrapped in <mark> by type."""
    text = result.original
    out: List[str] = []
    cursor = 0
    for e in sorted(result.entities, key=lambda x: x.start):
        if e.start < cursor:        # safety: skip any overlap
            continue
        out.append(html.escape(text[cursor:e.start]))
        out.append(_span(text[e.start:e.end], e.entity_type.lower(),
                         f"{e.entity_type} · confiance {e.score:.2f}"))
        cursor = e.end
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def highlight_after(result: AnonymizationResult) -> str:
    """Anonymised text with each token wrapped in <mark>."""
    text = result.anonymized
    out: List[str] = []
    cursor = 0
    for m in TOKEN_RE.finditer(text):
        out.append(html.escape(text[cursor:m.start()]))
        etype = m.group(1).lower()
        out.append(f'<mark class="tok tok--{etype}">{html.escape(m.group(0))}</mark>')
        cursor = m.end()
    out.append(html.escape(text[cursor:]))
    return "".join(out)
