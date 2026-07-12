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
# plaintext, which the sweep (the prod writer) MUST refuse.
_PASSPHRASE_ENV = "BUBBLE_SHIELD_STORE_PASSPHRASE"

_NO_PASSPHRASE_ERROR = (
    "Bubble Shield — le coffre chiffré n'est pas configuré "
    "(BUBBLE_SHIELD_STORE_PASSPHRASE absent). Le balayage est annulé pour ne "
    "pas écrire les données en clair."
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


def main(argv=None) -> int:
    """Run one background sweep. Returns a process exit code.

    0  = success (sweep ran) OR safe no-op (another sweep already holds the lock)
    1  = REFUSED: encrypted store not configured (would write plaintext PII)
    """
    parser = argparse.ArgumentParser(
        prog="bubble_shield_sweep",
        description="Bubble Shield background shadow-index sweep.")
    parser.add_argument(
        "--root", required=True,
        help="Root folder to sweep (walked recursively; new/changed files indexed).")
    args = parser.parse_args(argv)

    # GATE 1 — refuse-plaintext (Task 4 review, hard guard). BEFORE anything
    # touches the store: the sweep is the prod writer and must not persist the
    # document base's real client names to a plaintext SQLite store. stderr +
    # nonzero exit, nothing written.
    if not _passphrase_configured():
        sys.stderr.write(_NO_PASSPHRASE_ERROR + "\n")
        return 1

    _wire_paths()

    # Import AFTER wiring paths so these resolve to the vendored engine + the
    # plugin's real anonymisation pipeline.
    from bubble_shield import shadow_index
    import bubble_shield_mcp

    # Path normalization matches index_one / run_sweep exactly (shadow_store
    # keys by exact string) — resolve here so the launchd-passed path is
    # consistent with what run_sweep/index_one store and clear.
    root = str(Path(os.path.expanduser(args.root)).resolve())

    # GATE 2 — singleton lock. An overlapping launchd fire becomes a safe no-op
    # (MLX/Metal is not concurrency-safe; two sweeps at once crash).
    if not shadow_index.acquire_lock():
        print("sweep already running -- skip")
        return 0
    try:
        # The REAL anonymize_fn: the plugin's full model pipeline (extract_file
        # + GLiNER + Gemma) — the same anonymisation the old read path ran,
        # now off the read path where latency doesn't matter.
        result = shadow_index.run_sweep(
            root, anonymize_fn=bubble_shield_mcp._anonymise_file)
        # deferred = dataless/online-only (Dropbox not yet hydrated, retried next
        # sweep); failed = readable but un-certifiable (models down / scanned
        # image / unreachable second pass) — fail-closed, no shadow, retried next
        # sweep. Both are marked pending so a later sweep converges.
        print("sweep done -- indexed {} skipped {} deferred {} failed {}".format(
            result.get("indexed", 0), result.get("skipped", 0),
            result.get("deferred", 0), result.get("failed", 0)))
        return 0
    finally:
        shadow_index.release_lock()


if __name__ == "__main__":
    sys.exit(main())
