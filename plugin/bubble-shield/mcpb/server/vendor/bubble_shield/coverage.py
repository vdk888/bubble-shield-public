# bubble_shield/coverage.py
from __future__ import annotations
import os
from pathlib import Path
from bubble_shield import shadow_store


def coverage(root: str) -> dict:
    root_p = Path(os.path.expanduser(root)).resolve()
    indexed = shadow_store.list_indexed()
    files = [p for p in root_p.rglob("*") if p.is_file()]
    done, pending = 0, []
    for p in files:
        if shadow_store.content_hash(p) in indexed:
            done += 1
        else:
            pending.append(str(p))
    total = len(files)
    return {"total": total, "indexed": done,
            "pct": (100.0 * done / total) if total else 100.0,
            "pending_files": pending, "last_sweep": None}
