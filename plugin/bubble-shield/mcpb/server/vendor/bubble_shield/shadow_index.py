from __future__ import annotations
import os
from pathlib import Path
from bubble_shield import shadow_store


# ---- singleton sweep lock (Task 9) -----------------------------------------
# MLX/Metal (the model pipeline the sweep runs) is NOT concurrency-safe: two
# sweeps in flight at once crash the process. The lock makes an overlapping
# launchd fire a safe no-op. A STALE lock (holder PID dead — sweep killed
# mid-run) is stolen, or a crash would wedge the sweep forever.

def _lock_path() -> Path:
    return shadow_store._shield_home() / "sweep.lock"

def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists. os.kill(pid, 0) sends no signal —
    it just probes existence: it raises if the process is gone."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def acquire_lock() -> bool:
    """Acquire the singleton sweep lock. Returns True on success (lockfile
    written with our PID), False if a LIVE lock is already held by another
    process. A stale lockfile (unparseable, or a PID that is no longer alive)
    is stolen and acquired."""
    lp = _lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    if lp.exists():
        try:
            held = int(lp.read_text().strip() or "0")
        except ValueError:
            held = 0
        if held and _pid_alive(held):
            return False        # live lock held by another process
        # stale (dead PID / garbage) → steal it
    lp.write_text(str(os.getpid()))
    return True

def release_lock() -> None:
    """Remove the lockfile. Best-effort — a missing lockfile is not an error."""
    lp = _lock_path()
    if lp.exists():
        try:
            lp.unlink()
        except OSError:
            pass


def index_one(path: str, *, anonymize_fn) -> str:
    p = Path(os.path.expanduser(path)).resolve()
    h = shadow_store.content_hash(p)
    clean = anonymize_fn(str(p))
    st = p.stat()
    shadow_store.put_shadow(h, clean, src_path=str(p), size=st.st_size, mtime=st.st_mtime)
    try:
        shadow_store.clear_pending(str(p))
    except Exception:
        pass
    return h


# ---- dataless / online-only file resilience (Task 13b) ---------------------
# Real client folders live in Dropbox. Files there are frequently "online-only"
# placeholders: the metadata (name, size, mtime) exists but the bytes are NOT on
# disk until Dropbox hydrates them. Any attempt to READ the bytes (content_hash,
# text extraction) then raises OSError — on macOS this surfaces as errno 11
# ("Resource deadlock avoided") while the file-provider blocks, or ENOENT-like
# errors. A sweep that walks such a folder must NOT crash on the first dataless
# file and abort the whole walk — one un-hydrated file would strand every other
# file behind it in the sort order. Instead we:
#   1. try to force materialization by reading a few bytes, with a short retry
#      (opening/reading is what nudges Dropbox to start hydrating);
#   2. if it still won't materialize, SKIP-AND-DEFER: mark the source path
#      pending so the NEXT sweep retries it once Dropbox has caught up, and
#      count it as "deferred" (never fatal). This keeps the sweep resumable and
#      lets a slow-hydrating folder converge over successive sweeps.

_MATERIALIZE_TRIES = 3
_MATERIALIZE_SLEEP = 0.5   # seconds between hydration probes


def _try_materialize(p: Path) -> bool:
    """Best-effort force-hydrate a possibly-dataless file by reading a few bytes,
    retrying a couple of times. Returns True if the bytes became readable, False
    if the file is still dataless/unreadable after the retries. Never raises."""
    import time
    for i in range(_MATERIALIZE_TRIES):
        try:
            with open(p, "rb") as fh:
                fh.read(1)          # touching the bytes nudges Dropbox to hydrate
            return True
        except OSError:
            if i < _MATERIALIZE_TRIES - 1:
                time.sleep(_MATERIALIZE_SLEEP)
    return False


def _index_one_resilient(path: str, *, anonymize_fn) -> str:
    """index_one wrapped so one problem file DEFERS/FAILS instead of aborting.

    Returns one of:
      "indexed"  — a masked shadow was stored.
      "deferred" — the file is dataless/unreadable (Dropbox online-only
                   placeholder): its bytes couldn't be read even after a
                   materialization retry. Marked pending for a later sweep.
      "failed"   — the file WAS readable, but anonymize_fn could not certify it
                   (e.g. the NER daemon is offline → NERDownError, a scanned PDF
                   → ExtractionError, a structured CERFA whose Gemma second pass
                   is unreachable → StructuredFormUnverifiedError). The
                   uncertifiable doc gets NO shadow (never cached as clean) and
                   is marked pending for the next sweep. NOTE: until a later
                   sweep succeeds, a read of this doc serves RAW extracted text
                   via the B1 accepted-gap miss path (bubble_shield_read does
                   NOT fail closed on a miss — that is a deliberate product
                   decision). The security guarantee here is "no false-clean
                   shadow is ever stored", NOT "reads fail closed".

    The whole point: NEITHER an un-hydrated file NOR an un-certifiable doc may
    crash the sweep and strand every file behind it in the walk order.
    """
    p = Path(os.path.expanduser(path)).resolve()
    if not _try_materialize(p):
        # Still dataless after retries → defer to a later sweep, do not fail.
        try:
            shadow_store.mark_pending(str(p))
        except Exception:
            pass
        return "deferred"
    try:
        index_one(str(p), anonymize_fn=anonymize_fn)
        return "indexed"
    except OSError:
        # Bytes vanished mid-index (Dropbox evicted it again) or an extraction
        # read hit the placeholder → defer, don't abort the sweep.
        try:
            shadow_store.mark_pending(str(p))
        except Exception:
            pass
        return "deferred"
    except Exception:
        # Readable file, but anonymisation could not certify it (models down,
        # scanned image needing OCR, unreachable second pass…). Do NOT store a
        # shadow — mark pending for retry, and keep sweeping. This guarantees
        # no false-clean shadow is ever cached; it does NOT mean reads fail
        # closed in the meantime — a read before the next successful sweep
        # serves RAW extracted text via the B1 accepted-gap miss path (see
        # bubble_shield_read._read_with_shadow). Aborting the whole sweep on
        # one un-certifiable doc would be worse than that gap.
        #
        # #646: mark it as a FAILURE (failed=True) so fail_count increments — a doc
        # that fails deterministically every sweep (un-extractable INPI, giant form)
        # is QUARANTINED after QUARANTINE_AFTER_FAILS instead of retried forever
        # (which burns the single serial Gemma worker on a doc that never completes).
        # pending_files() excludes quarantined docs; quarantined_files() surfaces them.
        try:
            shadow_store.mark_pending(str(p), failed=True)
        except Exception:
            pass
        return "failed"

# Files that are NOT documents and must never be treated as index candidates:
# OS/tooling junk + Bubble Shield's own control files. Before this, a `.DS_Store`
# (macOS folder-metadata junk) had no extractable text, so the sweep fail-closed
# it and counted it as `failed` — inflating the "pending/failed" count and making
# a folder look stuck (30/34) when the 4 "failures" were 3 `.DS_Store` + 1 real
# scan. Skipping them is correct: they carry no client PII and can never index.
_IGNORE_BASENAMES = frozenset({
    ".DS_Store",          # macOS folder metadata
    ".bubble-shield.json",  # our own protection marker
    ".localized",         # macOS localized-folder marker
    "Thumbs.db",          # Windows thumbnail cache
    "desktop.ini",        # Windows folder config
})
# Extensions that are never text documents (images/media/archives/binaries) —
# nothing to anonymise, so skip rather than fail-closed. OCR of scanned PDFs is
# handled in the extract path; bare image files here are not client documents.
_IGNORE_SUFFIXES = frozenset({
    ".ds_store", ".log", ".tmp", ".lock", ".part", ".crdownload",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".heic", ".webp",
    ".mp3", ".mp4", ".mov", ".wav", ".avi", ".mkv",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".dmg", ".pkg",
    ".app", ".exe", ".dll", ".so", ".dylib",
})


def _is_ignorable(p: Path) -> bool:
    """True for OS/tooling junk + non-document media/binaries that carry no
    client text — these must be skipped (not fail-closed) so they don't inflate
    the failed/pending count and make a folder look stuck."""
    if p.name in _IGNORE_BASENAMES:
        return True
    if p.suffix.lower() in _IGNORE_SUFFIXES:
        return True
    return False


def run_sweep(root: str, *, anonymize_fn, exts=None, on_progress=None) -> dict:
    """Resumable folder walk: index new/changed files, skip already-indexed ones.

    Walks `root` recursively. For each file, if its content_hash is already in
    shadow_store.list_indexed() it is SKIPPED (never reprocessed) — this is what
    makes the sweep resumable: a run killed mid-index continues where it left off
    instead of restarting from zero. Otherwise the file is indexed via index_one.

    Optional `exts` (a set/collection of lowercase suffixes, e.g. {".txt",".pdf"})
    filters which files are considered.

    Path normalization matches index_one and the Task 5 read-miss caller EXACTLY:
    Path(os.path.expanduser(path)).resolve() → str. shadow_store keys pending/
    shadows by exact string match with no internal normalization, so a resolved
    path passed here stays consistent with the resolved path index_one clears
    from `pending` — no file can strand as permanently "pending".

    Dataless resilience (Task 13b): a Dropbox online-only placeholder raises
    OSError the moment its bytes are read (content_hash, extraction). Such a file
    is DEFERRED (marked pending for a later sweep once Dropbox hydrates it), never
    fatal — one un-hydrated file must not abort the walk and strand every file
    behind it.

    Anonymise resilience (Task 13b): a READABLE file whose anonymisation cannot
    be certified (NER daemon offline, scanned image needing OCR, structured CERFA
    whose Gemma second pass is unreachable) is counted as FAILED and marked
    pending — fail-closed (no shadow stored) but never fatal to the sweep. The
    returned dict carries `deferred` and `failed` counts alongside indexed/skipped.

    Optional `on_progress(indexed_so_far)` is called after EACH file the sweep
    successfully indexes — the caller uses this to rewrite the coverage snapshot
    live, so the dashboard % climbs during a long cold index instead of jumping
    from 0 to 100 only when the whole pass finishes. Best-effort: a callback that
    raises is swallowed (progress reporting must never break indexing).
    """
    root_p = Path(os.path.expanduser(root)).resolve()
    already = shadow_store.list_indexed()
    # #646: docs that failed to certify QUARANTINE_AFTER_FAILS+ times are SKIPPED —
    # re-sweeping them just re-fails and burns the serial Gemma worker. Keyed by the
    # resolved src_path (same normalisation the walk + mark_pending use). Load once.
    try:
        quarantined = {str(Path(os.path.expanduser(x)).resolve())
                       for x in shadow_store.quarantined_files()}
    except Exception:
        quarantined = set()
    indexed = skipped = deferred = failed = quarantined_skipped = 0
    for p in sorted(root_p.rglob("*")):
        try:
            if not p.is_file():
                continue
            if _is_ignorable(p):
                # OS junk / media / our own marker — not a document. Skip WITHOUT
                # counting as failed AND clear any stale pending row from a
                # pre-fix sweep that fail-closed it, so the count self-heals.
                try:
                    shadow_store.clear_pending(str(
                        Path(os.path.expanduser(str(p))).resolve()))
                except Exception:
                    pass
                continue
            if exts and p.suffix.lower() not in exts:
                continue
            # content_hash READS the file bytes → a dataless placeholder raises
            # OSError here. Guard it: materialize-or-defer instead of aborting.
            try:
                h = shadow_store.content_hash(p)
            except OSError:
                if not _try_materialize(p):
                    try:
                        shadow_store.mark_pending(str(p))
                    except Exception:
                        pass
                    deferred += 1
                    continue
                h = shadow_store.content_hash(p)
            if h in already:
                skipped += 1
                continue
            # #646: a quarantined doc (failed N+ times) is NOT re-swept — it can't
            # certify and would just burn the serial worker again. It stays surfaced
            # via quarantined_files() for the operator, not retried here.
            if str(Path(os.path.expanduser(str(p))).resolve()) in quarantined:
                quarantined_skipped += 1
                continue
            outcome = _index_one_resilient(str(p), anonymize_fn=anonymize_fn)
            if outcome == "indexed":
                indexed += 1
                if on_progress is not None:
                    try:
                        on_progress(indexed)
                    except Exception:
                        pass  # progress reporting must never break indexing
            elif outcome == "failed":
                failed += 1
            else:
                deferred += 1
        except OSError:
            # Belt-and-suspenders: ANY per-file OSError (a placeholder that
            # errors even on is_file/stat) defers that one file; the walk goes on.
            try:
                shadow_store.mark_pending(str(p))
            except Exception:
                pass
            deferred += 1
    return {"indexed": indexed, "skipped": skipped,
            "deferred": deferred, "failed": failed,
            "quarantined": quarantined_skipped}
