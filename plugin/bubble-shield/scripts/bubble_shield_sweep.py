#!/usr/bin/env python3
"""Bubble Shield background sweep — CLI entrypoint (launchd runs this).

WHY THIS EXISTS (the shadow-index redesign, Phase 3)
----------------------------------------------------
The read path (bubble_shield_read → _read_with_shadow) is now ZERO-model: a
cache HIT serves the pre-anonymised shadow, a MISS serves raw extracted text
and QUEUES the file (shadow_store.mark_pending). The heavy anonymisation — the
FULL model pipeline (extract_file + GLiNER + Gemma) that the old read path used
to run inline — moved HERE, off the read path, into a scheduled background
sweep where latency doesn't matter.

This CLI is what the launchd job invokes:

    python3 bubble_shield_sweep.py --root <folder>

It builds the REAL anonymize_fn (the plugin's _anonymise_file — the same full
pipeline the old read path ran), then runs shadow_index.run_sweep over `root`,
indexing every new/changed file into the shadow store.

TWO HARD SAFETY GATES before any indexing happens:

  1. REFUSE-PLAINTEXT (Task 4 encryption review). The shadow store falls back to
     PLAINTEXT-at-rest when BUBBLE_SHIELD_STORE_PASSPHRASE is unset (the store
     only WARNS there — Task 4 deferred the hard refuse to here). The SWEEP is
     the prod WRITER: it would write the whole document base's real client names
     into a plaintext SQLite file. So if the passphrase is unset/empty we REFUSE
     to run at all (exit 1, nothing written) — this is the hard guard the Task 4
     review required.

  2. SINGLETON LOCK (no concurrent sweeps). MLX/Metal is NOT concurrency-safe;
     two sweeps at once crash. acquire_lock() makes an overlapping launchd fire
     a safe no-op (exit 0) instead of a second, crashing run.

Python 3.9 floor, pure stdlib + the vendored bubble_shield engine.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(os.environ.get(
    "CLAUDE_PLUGIN_ROOT", _HERE.parent))

# Env var carrying the machine-local passphrase for the shadow store at rest.
# Must match shadow_store._PASSPHRASE_ENV — if unset/empty, the store writes
# plaintext.
_PASSPHRASE_ENV = "BUBBLE_SHIELD_STORE_PASSPHRASE"

# v1 DECISION (2026-07-13): encryption-at-rest for the shadow store is PARKED;
# v1 ships accepting a PLAINTEXT store (chmod-600). The sweep is the store's prod
# writer, so it historically REFUSED to run without a passphrase (would write real
# client names to a plaintext SQLite file). With plaintext accepted for v1, that
# refuse now BLOCKS indexing entirely — nothing ever gets swept. So the refuse
# becomes OPT-OUT: set BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE=1 to let the sweep run
# against a plaintext store (the v1 default the installer sets). Without either a
# passphrase OR this flag, the sweep still refuses — so a deployment that WANTS
# encryption isn't silently downgraded.
_ALLOW_PLAINTEXT_ENV = "BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE"

_NO_PASSPHRASE_ERROR = (
    "Bubble Shield — le coffre chiffré n'est pas configuré "
    "(BUBBLE_SHIELD_STORE_PASSPHRASE absent) et le mode clair n'est pas autorisé "
    "(BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE≠1). Le balayage est annulé pour ne pas "
    "écrire les données en clair sans consentement explicite."
)

_PLAINTEXT_ACCEPTED_NOTE = (
    "Bubble Shield — coffre en clair accepté pour cette version "
    "(BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE=1) : le balayage indexe en clair, "
    "protégé par les permissions du fichier (chmod 600)."
)


def _vendor() -> Path:
    """Resolve the vendored bubble_shield package dir. Mirrors
    bubble_shield_mcp._vendor() so the sweep imports the SAME vendored engine
    (shadow_store / shadow_index with the pending table + sweep fns)."""
    for cand in (PLUGIN_ROOT / "vendor", _HERE / "vendor", _HERE.parent / "vendor"):
        if (cand / "bubble_shield").is_dir():
            return cand
    return PLUGIN_ROOT / "vendor"


def _scripts_dir() -> Path:
    """Resolve the scripts dir (holds bubble_shield_mcp / bubble_shield_extract).
    Mirrors bubble_shield_mcp._scripts_dir()."""
    for cand in (PLUGIN_ROOT / "scripts", _HERE):
        if (cand / "bubble_shield_extract.py").is_file():
            return cand
    return _HERE


def _wire_paths() -> None:
    """Put the vendor + scripts dirs on sys.path (same pattern the MCP server
    uses) so `from bubble_shield import ...` and `import bubble_shield_mcp`
    both resolve to the vendored/plugin copies."""
    sys.path.insert(0, str(_vendor()))
    sys.path.insert(0, str(_scripts_dir()))


def _passphrase_configured() -> bool:
    """True iff BUBBLE_SHIELD_STORE_PASSPHRASE is set to a non-empty value —
    mirrors shadow_store._passphrase()'s truthiness check exactly."""
    return bool(os.environ.get(_PASSPHRASE_ENV))


def _plaintext_store_allowed() -> bool:
    """True iff the operator explicitly opted into a plaintext store at rest via
    BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE=1 (the v1 accepted-plaintext decision).
    Only exact '1' counts — no accidental truthiness from an arbitrary value."""
    return os.environ.get(_ALLOW_PLAINTEXT_ENV, "").strip() == "1"


def _configured_protected_roots() -> list:
    """The folders the user actually marked, from the guard config's
    `protected_folders`. This is the SINGLE source of truth the sweep, the guard
    and the dashboard coverage panel all read — so the sweep indexes exactly the
    folder the user protected (not a fixed placeholder root). Best-effort: a
    missing / unreadable config yields [] (a no-op sweep, not a crash).

    Config path resolution mirrors the guard: BUBBLE_SHIELD_GUARD_CONFIG env
    override, else ~/.config/bubble_shield/bubble-shield.json."""
    import json
    cfg_path = os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG") or \
        os.path.expanduser("~/.config/bubble_shield/bubble-shield.json")
    roots = []
    try:
        p = Path(cfg_path)
        if p.is_file():
            cfg = json.loads(p.read_text(encoding="utf-8")) or {}
            for raw in (cfg.get("protected_folders") or []):
                if raw:
                    roots.append(str(Path(os.path.expanduser(str(raw))).resolve()))
    except Exception:
        return []
    # De-dup while preserving order.
    seen = set()
    out = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def main(argv=None) -> int:
    """Run one background sweep. Returns a process exit code.

    0  = success (sweep ran) OR safe no-op (another sweep already holds the lock)
    1  = REFUSED: no passphrase AND plaintext not explicitly allowed
         (BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE≠1) — would write plaintext PII
         without consent.
    """
    parser = argparse.ArgumentParser(
        prog="bubble_shield_sweep",
        description="Bubble Shield background shadow-index sweep.")
    parser.add_argument(
        "--root", default=None,
        help="Root folder to sweep. If omitted, the sweep reads the protected "
             "folders the user actually marked from the guard config "
             "(protected_folders) — that is the single source of truth so the "
             "sweep, the guard and the coverage panel all agree on WHAT is "
             "protected. --root is kept for a one-off manual sweep of a path.")
    args = parser.parse_args(argv)

    # GATE 1 — plaintext-store policy. The sweep is the store's prod writer. If a
    # passphrase is set the store is encrypted (best). If not, v1 accepts a
    # plaintext store ONLY when the operator opted in via
    # BUBBLE_SHIELD_ALLOW_PLAINTEXT_STORE=1 — then the sweep proceeds and logs a
    # clear note. With neither, it still refuses (exit 1, nothing written) so an
    # encryption-intending deployment isn't silently downgraded.
    if not _passphrase_configured():
        if _plaintext_store_allowed():
            sys.stderr.write(_PLAINTEXT_ACCEPTED_NOTE + "\n")
        else:
            sys.stderr.write(_NO_PASSPHRASE_ERROR + "\n")
            return 1

    _wire_paths()

    # Import AFTER wiring paths so these resolve to the vendored engine + the
    # plugin's real anonymisation pipeline.
    from bubble_shield import shadow_index
    import bubble_shield_mcp

    # Resolve WHAT to sweep. Path normalization matches index_one / run_sweep
    # exactly (shadow_store keys by exact string) — resolve here so the path is
    # consistent with what run_sweep/index_one store and clear.
    if args.root:
        roots = [str(Path(os.path.expanduser(args.root)).resolve())]
    else:
        # Discover marked folders (bounded scan for .bubble-shield.json markers)
        # UNION the config protected_folders registry. Marker-discovery means a
        # folder marked from Cowork (marker written, but host ~/.config not
        # reachable) is still swept — the app/sweep don't depend on the config
        # write that Cowork can't do.
        from bubble_shield import coverage as _cov
        roots = _cov.discover_protected_roots()
        if not roots:
            # No folder marked yet — nothing to sweep. NOT an error: the user
            # simply hasn't protected a folder. (This is the state that used to
            # silently sweep a nonexistent placeholder root and index nothing.)
            print("sweep no-op -- no protected folders configured")
            return 0

    # GATE 2 — singleton lock. An overlapping launchd fire becomes a safe no-op
    # (MLX/Metal is not concurrency-safe; two sweeps at once crash).
    if not shadow_index.acquire_lock():
        print("sweep already running -- skip")
        return 0
    try:
        totals = {"indexed": 0, "skipped": 0, "deferred": 0, "failed": 0}
        for root in roots:
            if not Path(root).is_dir():
                print(f"sweep skip -- not a directory: {root}")
                continue
            # The REAL anonymize_fn: the plugin's full model pipeline
            # (extract_file + GLiNER + Gemma) — the same anonymisation the old
            # read path ran, now off the read path where latency doesn't matter.
            result = shadow_index.run_sweep(
                root, anonymize_fn=bubble_shield_mcp._anonymise_file)
            for k in totals:
                totals[k] += result.get(k, 0)
        # deferred = dataless/online-only (Dropbox not yet hydrated, retried next
        # sweep); failed = readable but un-certifiable (models down / scanned
        # image / unreachable second pass) — fail-closed, no shadow, retried next
        # sweep. Both are marked pending so a later sweep converges.
        print("sweep done -- roots {} indexed {} skipped {} deferred {} failed {}".format(
            len(roots), totals["indexed"], totals["skipped"],
            totals["deferred"], totals["failed"]))

        # Persist a coverage SNAPSHOT the desktop app can read WITHOUT Full Disk
        # Access. The sweep (this launchd process) has already read every root to
        # index it, so it can compute coverage here; the GUI app — which runs
        # through Apple's shared Python and can't get FDA to CloudStorage — then
        # reads this snapshot instead of re-scanning the disk. Best-effort: a
        # failed snapshot must not fail the sweep. Paths + counts only, no PII.
        try:
            from bubble_shield import coverage as _covmod
            from bubble_shield import coverage_state as _cst
            snap = []
            for root in roots:
                try:
                    c = _covmod.coverage(root)
                    snap.append({
                        "root": root,
                        "total": c.get("total", 0),
                        "indexed": c.get("indexed", 0),
                        "pct": round(c.get("pct", 0.0), 1),
                        "pending": len(c.get("pending_files", [])),
                    })
                except Exception:
                    snap.append({"root": root, "total": 0, "indexed": 0,
                                 "pct": 0.0, "pending": 0, "error": True})
            _cst.write_state(snap)
        except Exception:
            pass  # snapshot is best-effort; the sweep itself already succeeded

        return 0
    finally:
        shadow_index.release_lock()


if __name__ == "__main__":
    sys.exit(main())
