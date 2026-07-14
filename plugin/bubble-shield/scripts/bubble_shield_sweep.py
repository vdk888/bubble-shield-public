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
import time
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


# Daemon endpoints (mirror posttool_anonymize / bubble_shield_gemmad defaults).
_NERD_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
_GEMMAD_PORT = int(os.environ.get("BUBBLE_SHIELD_GEMMAD_PORT", "8724"))
_WARM_TIMEOUT_S = float(os.environ.get("BUBBLE_SHIELD_SWEEP_WARM_TIMEOUT", "180"))


def _http_json(url, payload=None, timeout=5):
    """Tiny stdlib POST/GET returning parsed JSON or None. Never raises."""
    import json as _json
    import urllib.request
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.load(r)
    except Exception:
        return None


def _spawn_gemmad() -> None:
    """Start the Gemma judge daemon detached if it isn't running. Mirrors its
    LaunchAgent exactly: <gemma-env python> <daemon>/scripts/bubble_shield_gemmad.py
    --no-warm, with BUBBLE_SHIELD_HOME + HF_HUB_OFFLINE=1. Best-effort, never
    raises.

    Why the sweep needs this: posttool._try_spawn_daemon only starts the NER
    daemon; gemmad otherwise depends on its LaunchAgent being alive. If the
    LaunchAgent isn't loaded (or died), a SCANNED financial doc that needs the
    Gemma second pass fail-closes on every sweep. Spawning gemmad here guarantees
    BOTH daemons are available when the sweep runs."""
    import subprocess
    home = Path(os.environ.get(
        "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
    vpy = home / "gemma-env" / "bin" / "python"
    script = home / "daemon" / "scripts" / "bubble_shield_gemmad.py"
    if not (vpy.exists() and script.exists()):
        return  # gemmad not installed at the stable paths → nothing to spawn
    try:
        env = dict(os.environ)
        env["BUBBLE_SHIELD_HOME"] = str(home)
        env["HF_HUB_OFFLINE"] = "1"
        subprocess.Popen(
            [str(vpy), str(script), "--no-warm"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    except Exception:
        pass


def _warm_one(name, port, warm_payload, warm_path):
    """Spawn (if needed) + warm ONE daemon, waiting until /health reports warm
    (or a bounded timeout). Best-effort: logs and returns, never raises.

    The daemons run `--no-warm` (lazy): the LaunchAgent opens the port fast but
    the model loads on the FIRST real request. So we: (1) make sure the port is
    up (spawn via the shared posttool helper if not), (2) fire one real request
    to trigger the model load, (3) poll /health until warm."""
    base = f"http://127.0.0.1:{port}"

    # 1) Ensure the daemon process is up (spawn if the port is dead). NER via the
    #    hardened posttool spawn (cooldown + ml-pack check); gemmad via our own
    #    _spawn_gemmad (mirrors its LaunchAgent). If a daemon can't be brought up,
    #    we bail fast below rather than block the whole warm timeout on a dead port.
    if _http_json(base + "/health", timeout=1) is None:
        try:
            if port == _NERD_PORT:
                import posttool_anonymize as _pt
                _pt._try_spawn_daemon()
            elif port == _GEMMAD_PORT:
                _spawn_gemmad()
        except Exception:
            pass

    # 2) Fire one real request to trigger the lazy model load, then 3) poll warm.
    #    Two bounded phases: PORT_WAIT for the port to answer /health at all, then
    #    the model-load wait. A dead port that never answers is abandoned in
    #    ~PORT_WAIT seconds (not the full warm timeout) — don't hang the sweep.
    port_deadline = time.monotonic() + 20.0  # port should bind within ~20s
    warm_deadline = time.monotonic() + _WARM_TIMEOUT_S
    fired = False
    while time.monotonic() < warm_deadline:
        h = _http_json(base + "/health", timeout=2)
        if h is None:
            if time.monotonic() > port_deadline:
                print(f"sweep warm -- {name} port down (skipped; retry next sweep)")
                return
            time.sleep(2)
            continue
        if h.get("warm") is True:
            print(f"sweep warm -- {name} ready")
            return
        if not fired:
            # Port is up but cold — send the warm-up request ONCE (loads model).
            _http_json(base + warm_path, payload=warm_payload, timeout=_WARM_TIMEOUT_S)
            fired = True
            continue
        time.sleep(2)
    print(f"sweep warm -- {name} not warm after {_WARM_TIMEOUT_S:.0f}s "
          "(proceeding; unindexed files retry next sweep)")


def _warm_daemons() -> None:
    """Warm the NER + Gemma daemons before the sweep processes files, so a file
    needing the model gets a LIVE pipeline instead of fail-closing every run.
    Best-effort, bounded, never fatal. Skips entirely if the ML pack isn't
    installed (nothing to warm — regex-only sweep)."""
    home = Path(os.environ.get(
        "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
    if not (home / "ml.json").is_file():
        return  # ML pack not installed → no daemons to warm
    try:
        # NER: a trivial detect warms GLiNER.
        _warm_one("nerd", _NERD_PORT,
                  {"text": "Monsieur Jean Dupont"}, "/detect")
        # Gemma: warm via /extract_pii — the MASKING second-pass path (the one
        # that fires on structured forms / scanned liasses). This matters two
        # ways: (1) the previous warm hit /classify with a MALFORMED payload
        # ({"text":...} where /classify wants {"tokens":[...]}) so it never ran a
        # real inference; (2) /classify and /extract_pii are DIFFERENT prompts —
        # priming one doesn't compile the other's MLX graph. Hitting /extract_pii
        # with a real {"text":...} triggers the daemon's warm_up (which now primes
        # BOTH classify + extract_pii inference) so the FIRST real form the sweep
        # processes doesn't pay the cold-compile cost and time out. The generous
        # warm timeout absorbs that one-time cost on this throwaway request.
        _warm_one("gemmad", _GEMMAD_PORT,
                  {"text": "Nom: DUPONT\nNé le: 01/01/1980"}, "/extract_pii")
    except Exception:
        # Warming is best-effort; a failure must never abort the sweep. Files that
        # needed the model just stay pending and retry next sweep.
        pass


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


# ── audit logging for indexed files ─────────────────────────────────────────
# The dashboard's stats cards read ~/.bubble_shield/audit.jsonl. Only the OLD
# interactive anonymise/write path logged there, so once work moved to the
# background sweep the cards froze at the last interactive run. Here the sweep
# records one audit entry per successfully-indexed file, with per-type counts
# derived from the cloaked text's ⟦TYPE_NNNN⟧ tokens (distinct IDs per type —
# the same client repeated is one token, counted once). No PII: we read only the
# token TYPE + numeric id, never a real value.
import re as _re
_MASK_TOKEN_RE = _re.compile(r"⟦([A-Z_]+)_(\d{4,}[a-z]?)⟧")


def _count_tokens_by_type(cloaked_text: str) -> dict:
    """Distinct ⟦TYPE_id⟧ tokens per TYPE, e.g. {'NOM': 2, 'IBAN': 1}. Distinct
    on the (type,id) pair so repeated mentions of the same masked entity count
    once — matching how the interactive audit counts entities."""
    seen = {}
    for m in _MASK_TOKEN_RE.finditer(cloaked_text or ""):
        etype, eid = m.group(1), m.group(2)
        seen.setdefault(etype, set()).add(eid)
    return {t: len(ids) for t, ids in seen.items()}


class _CountResult:
    """Minimal AnonymizationResult-shape for audit.log_result: exposes the
    per-type counts as `.entities` (type-only stand-ins), `.entity_count`, and
    `.safe_to_send`. We only ever expose the TYPE, never a value."""
    class _Ent:
        __slots__ = ("entity_type",)
        def __init__(self, t): self.entity_type = t
    def __init__(self, counts: dict):
        self.entities = [self._Ent(t) for t, n in counts.items() for _ in range(n)]
        self.entity_count = sum(counts.values())
        self.safe_to_send = True  # a swept+indexed file passed the fail-closed pipeline


def _audit_indexed(path: str, cloaked_text: str) -> None:
    """Append an audit entry for a file the sweep just indexed. Best-effort:
    audit failures must never affect indexing. Mission = the folder name, event
    = 'sweep_index' (distinct from interactive 'anonymize')."""
    try:
        from bubble_shield import audit as _audit
        home = Path(os.environ.get(
            "BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
        # log_result's FIRST arg is the AUDIT LOG path (where to append), not the
        # document being processed. Resolve it the same way the webapp does:
        # BUBBLE_SHIELD_AUDIT_LOG override, else ~/.bubble_shield/audit.jsonl.
        log_path = os.environ.get("BUBBLE_SHIELD_AUDIT_LOG") or str(home / "audit.jsonl")
        counts = _count_tokens_by_type(cloaked_text)
        mission = Path(path).parent.name or "sweep"
        _audit.log_result(log_path, _CountResult(counts),
                          mission=mission, event="sweep_index")
    except Exception:
        pass  # audit is display-only; never break the sweep


def _audit_wrapped(anonymize_fn):
    """Wrap an anonymize_fn so each successful call ALSO records an audit entry
    (per-type token counts) for the dashboard stats. Transparent: returns the
    exact cloaked text the inner fn produced, so run_sweep/index_one are
    unaffected. An audit failure is swallowed — indexing must not depend on it."""
    def _wrapped(path):
        cloaked = anonymize_fn(path)
        _audit_indexed(path, cloaked)
        return cloaked
    return _wrapped


# ── coverage snapshot (dashboard reads this — FDA-free) ──────────────────────

def _write_coverage_snapshot(roots) -> None:
    """Compute + persist the coverage snapshot the desktop app reads (the GUI
    can't self-scan under macOS TCC, so the sweep — which CAN read the folders —
    writes it here). Best-effort: a failed snapshot must never fail the sweep.
    Paths + counts only, no PII. Called at sweep START (folder appears at once),
    LIVE during indexing (throttled), and at END (authoritative)."""
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
        pass  # snapshot is best-effort; never let it break the sweep


# Minimum seconds between live progress snapshot writes during a sweep, so a
# large cold index doesn't rewrite the file on every single file.
_SNAPSHOT_MIN_INTERVAL_S = float(
    os.environ.get("BUBBLE_SHIELD_SNAPSHOT_INTERVAL", "2"))


def _throttled_snapshot(roots):
    """Return an on_progress(indexed) callback that rewrites the coverage snapshot
    at most once every _SNAPSHOT_MIN_INTERVAL_S seconds — so the dashboard %
    climbs live during a long index without thrashing the disk. Each call also
    recomputes coverage (a bounded rglob), which is why we throttle."""
    state = {"last": 0.0}

    def _cb(_indexed):
        now = time.monotonic()
        if now - state["last"] >= _SNAPSHOT_MIN_INTERVAL_S:
            state["last"] = now
            _write_coverage_snapshot(roots)
    return _cb


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
        # WARM THE DAEMONS FIRST (the sweep owns its dependencies). The NER +
        # Gemma daemons idle-shutdown after 10 min; the sweep runs every 20 min —
        # so left alone the daemons are ALWAYS dead when the sweep needs them, and
        # any file needing the model (a scanned liasse fiscale) fail-closes on
        # EVERY sweep and never indexes. Here the sweep spawns + warms them and
        # waits (bounded) before processing, so a hard file gets a live pipeline.
        # Best-effort: warming failures are non-fatal — those files just stay
        # pending and retry next sweep, exactly as before this fix.
        _warm_daemons()

        # SNAPSHOT AT START — write the coverage snapshot BEFORE indexing, so the
        # marked folder appears in the dashboard IMMEDIATELY (at 0%/pending)
        # instead of showing "no protected folder" for the whole first cold index.
        # The GUI app can't self-scan (TCC), so without this the panel is blank
        # from a fresh install until the first full pass finishes.
        _write_coverage_snapshot(roots)

        totals = {"indexed": 0, "skipped": 0, "deferred": 0, "failed": 0}
        for root in roots:
            if not Path(root).is_dir():
                print(f"sweep skip -- not a directory: {root}")
                continue
            # LIVE PROGRESS — rewrite the snapshot as files index so the dashboard
            # % climbs in real time instead of jumping 0→100 at the end. Throttled
            # (min interval) so a 100-file cold index doesn't thrash the disk.
            _prog = _throttled_snapshot(roots)
            # The REAL anonymize_fn: the plugin's full model pipeline
            # (extract_file + GLiNER + Gemma) — the same anonymisation the old
            # read path ran, now off the read path where latency doesn't matter.
            # Wrap it to also record an AUDIT entry per indexed file, so the
            # dashboard's stats cards (which read the audit log) reflect the REAL
            # background indexing. Counts come from the cloaked text's ⟦TYPE_NNNN⟧
            # tokens (distinct per type) — no PII, no result-object plumbing needed.
            result = shadow_index.run_sweep(
                root, anonymize_fn=_audit_wrapped(bubble_shield_mcp._anonymise_file),
                on_progress=_prog)
            for k in totals:
                totals[k] += result.get(k, 0)
        # deferred = dataless/online-only (Dropbox not yet hydrated, retried next
        # sweep); failed = readable but un-certifiable (models down / scanned
        # image / unreachable second pass) — fail-closed, no shadow, retried next
        # sweep. Both are marked pending so a later sweep converges.
        print("sweep done -- roots {} indexed {} skipped {} deferred {} failed {}".format(
            len(roots), totals["indexed"], totals["skipped"],
            totals["deferred"], totals["failed"]))

        # SNAPSHOT AT END — final authoritative write once every root is done.
        _write_coverage_snapshot(roots)

        return 0
    finally:
        shadow_index.release_lock()


if __name__ == "__main__":
    sys.exit(main())
