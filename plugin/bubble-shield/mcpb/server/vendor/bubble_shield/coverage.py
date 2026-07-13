# bubble_shield/coverage.py
from __future__ import annotations
import json
import os
from pathlib import Path
from bubble_shield import shadow_store

MARKER_NAME = ".bubble-shield.json"


def _config_protected_roots() -> list:
    """Folders registered in the guard config's `protected_folders`. This is the
    fast/explicit registry; on Cowork the MCP server writes it host-side, and on
    the Mac app the installer/CLI can. Best-effort: missing/bad config → []."""
    cfg_path = os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG") or \
        os.path.expanduser("~/.config/bubble_shield/bubble-shield.json")
    out = []
    try:
        p = Path(cfg_path)
        if p.is_file():
            cfg = json.loads(p.read_text(encoding="utf-8")) or {}
            for raw in (cfg.get("protected_folders") or []):
                if raw:
                    out.append(Path(os.path.expanduser(str(raw))).resolve())
    except Exception:
        return []
    return out


# Where a CGP/advisor typically keeps client dossiers. We scan these (bounded)
# for markers so a folder marked from Cowork — which can write the in-folder
# marker but NOT the host ~/.config — is still discovered by the native app +
# sweep. Not the whole disk (unbounded/slow): just the likely roots.
def _likely_scan_bases() -> list:
    home = Path.home()
    cands = [
        home / "Documents",
        home / "Desktop",
        home,  # top level only (depth-limited below)
        home / "Library" / "CloudStorage",  # Dropbox/iCloud/OneDrive/Box mounts
        home / "Dropbox",
        home / "OneDrive",
    ]
    seen, out = set(), []
    for c in cands:
        try:
            if c.is_dir() and c not in seen:
                seen.add(c)
                out.append(c)
        except OSError:
            continue
    return out


def _scan_markers(base: Path, max_depth: int = 4, cap: int = 4000) -> list:
    """Bounded shallow scan under `base` for folders carrying a marker. Depth-
    and entry-capped so a huge tree can never stall this. Mirrors the guard's
    _discover_marker_roots descent."""
    found = []
    try:
        stack = [(base, 0)]
        seen = 0
        while stack and seen < cap:
            d, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                seen += 1
                if e.name == MARKER_NAME and e.is_file():
                    found.append(Path(d).resolve())
                elif (e.is_dir(follow_symlinks=False)
                      and not e.name.startswith(".")):
                    stack.append((Path(e.path), depth + 1))
    except Exception:
        pass
    return found


def discover_protected_roots() -> list:
    """The protected folders the app + sweep should act on: folders carrying a
    `.bubble-shield.json` marker (discovered by a bounded scan of the likely
    client-dossier roots) UNION the config `protected_folders` registry.

    Marker-discovery makes the marker sufficient on its own — a folder marked
    from Cowork (which can write the in-folder marker but not host ~/.config) is
    still found. Config catches folders in unusual locations the bounded scan
    misses. Returns resolved paths, de-duplicated, order-stable."""
    roots = list(_config_protected_roots())
    for base in _likely_scan_bases():
        roots.extend(_scan_markers(base))
    seen, out = set(), []
    for r in roots:
        rs = str(r)
        if rs not in seen:
            seen.add(rs)
            out.append(rs)
    return out


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
