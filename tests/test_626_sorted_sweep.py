"""
test_626_sorted_sweep.py — #626 item 3: composite "sorted sweep" ordering.

THE PROBLEM: run_sweep walked the tree ALPHABETICALLY (`sorted(root.rglob("*"))`).
For an 81,000-doc cold backfill that is pathological: heavy scanned PDFs early in
the alphabet stall the single serial worker while recent, light, high-value docs
wait weeks behind them.

THE FIX under test — a composite order that indexes what matters first:
  buckets by mtime recency (<30d, <1y, <3y, older) THEN cheap-first within each
  bucket (ascending cost = st_size * ext_factor; ext_factor 3.0 for scan-heavy
  image/pdf suffixes, 1.0 otherwise). Heavy docs sink to each bucket's tail; old
  heavy docs sink to the global tail ("heavy-LAST v1"). Path-alphabetical breaks
  ties for determinism. STAT-ONLY: never reads bytes (dataless placeholders raise
  on byte reads; st_size/st_mtime are metadata and safe). An unstatable file sinks
  to the very end and never crashes the walk.

ORDERING-ONLY: skip/resume, quarantine, dataless-defer, fail-closed, on_progress,
value_hashes threading are all unchanged — only iteration order moves. The e2e
test below re-asserts the counts are byte-identical to the alphabetical sweep.

Synthetic files only.
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))

import pytest

from bubble_shield import shadow_index as si
from bubble_shield import shadow_store as ss


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    return tmp_path


_NOW = time.time()
_DAY = 86400.0


def _mkfile(root: Path, name: str, *, size: int, age_days: float) -> Path:
    """Create a synthetic file of `size` bytes with an mtime `age_days` in the
    past. STAT-ONLY ordering reads st_size/st_mtime, so content is just padding."""
    p = root / name
    p.write_bytes(b"x" * size)
    mt = _NOW - age_days * _DAY
    os.utime(p, (mt, mt))
    return p


def _order_names(paths):
    return [p.name for p in si._sweep_order(paths)]


# ── 1. recency wins across buckets ────────────────────────────────────────────

def test_recency_beats_size(home, tmp_path):
    """An OLD small file sorts AFTER a RECENT large one — the recency bucket
    dominates the cheap-first cost within a bucket."""
    recent_big = _mkfile(tmp_path, "recent_big.txt", size=10_000, age_days=1)
    old_small = _mkfile(tmp_path, "old_small.txt", size=10, age_days=400)
    order = _order_names([old_small, recent_big])
    assert order == ["recent_big.txt", "old_small.txt"]


# ── 2. cheap-first within a bucket ────────────────────────────────────────────

def test_cheap_first_within_bucket(home, tmp_path):
    """Same recency bucket: the small .txt is indexed before the large .txt."""
    small = _mkfile(tmp_path, "small.txt", size=10, age_days=2)
    big = _mkfile(tmp_path, "big.txt", size=10_000, age_days=2)
    order = _order_names([big, small])
    assert order == ["small.txt", "big.txt"]


# ── 3. per-extension scan surcharge ───────────────────────────────────────────

def test_ext_surcharge_sinks_scans(home, tmp_path):
    """Same bucket, same byte size: the scan-heavy .pdf (ext_factor 3.0) sorts
    AFTER the .txt (ext_factor 1.0)."""
    txt = _mkfile(tmp_path, "doc.txt", size=1000, age_days=2)
    pdf = _mkfile(tmp_path, "scan.pdf", size=1000, age_days=2)
    order = _order_names([pdf, txt])
    assert order == ["doc.txt", "scan.pdf"]


def test_ext_surcharge_covers_image_types(home, tmp_path):
    """The surcharge applies to the documented image/scan suffixes, not just
    .pdf — a same-size .png sorts after a .txt."""
    txt = _mkfile(tmp_path, "a.txt", size=500, age_days=2)
    for ext in (".png", ".jpg", ".jpeg", ".tiff", ".heic"):
        img = _mkfile(tmp_path, f"scan{ext}", size=500, age_days=2)
        order = _order_names([img, txt])
        assert order == ["a.txt", f"scan{ext}"], ext


# ── 4. deterministic tiebreaker ───────────────────────────────────────────────

def test_determinism_path_tiebreaker(home, tmp_path):
    """Equal (bucket, cost) → path-alphabetical decides, so runs are reproducible
    regardless of input order."""
    a = _mkfile(tmp_path, "aaa.txt", size=100, age_days=2)
    b = _mkfile(tmp_path, "bbb.txt", size=100, age_days=2)
    c = _mkfile(tmp_path, "ccc.txt", size=100, age_days=2)
    assert _order_names([c, a, b]) == ["aaa.txt", "bbb.txt", "ccc.txt"]
    assert _order_names([b, c, a]) == ["aaa.txt", "bbb.txt", "ccc.txt"]


# ── 5. unstatable file sinks to end, never crashes ────────────────────────────

def test_unstatable_file_sinks_to_end(home, tmp_path, monkeypatch):
    """A file whose stat() raises (dataless-placeholder edge) must NOT crash the
    ordering: it sinks to the very tail (bucket=older, cost=+inf equivalent) so
    the existing per-file error handling deals with it only once every readable
    file ahead of it is done."""
    recent = _mkfile(tmp_path, "recent.txt", size=10, age_days=1)
    old = _mkfile(tmp_path, "old.txt", size=10, age_days=1000)
    bad = tmp_path / "dataless.txt"
    bad.write_bytes(b"x")

    real_stat = Path.stat
    def stat_or_raise(self, *a, **k):
        if self.name == "dataless.txt":
            raise OSError(11, "Resource deadlock avoided")
        return real_stat(self, *a, **k)
    monkeypatch.setattr(Path, "stat", stat_or_raise)

    order = _order_names([bad, old, recent])
    assert order[-1] == "dataless.txt"          # sinks below even the oldest file
    assert order == ["recent.txt", "old.txt", "dataless.txt"]


# ── 6. end-to-end: ordering-only, counts unchanged, resume still holds ─────────

def test_e2e_indexes_all_once_and_resumes(home, tmp_path):
    """run_sweep over a mixed tree indexes EVERY file exactly once (counts
    unchanged vs the alphabetical sweep), on_progress fires per indexed file, and
    a re-sweep skips them all — proving the change is ordering-only."""
    root = tmp_path / "docs"; root.mkdir()
    (root / "sub").mkdir()
    # All document types the sweep actually indexes (.png etc. are _IGNORE_SUFFIXES
    # media — skipped, not indexed — so the ordering surcharge on scans is proven
    # with .pdf, a real indexable scan-heavy type).
    _mkfile(root, "recent_small.txt", size=10, age_days=1)
    _mkfile(root, "recent_big.pdf", size=50_000, age_days=1)
    _mkfile(root, "old_small.txt", size=10, age_days=800)
    _mkfile(root / "sub", "mid.txt", size=200, age_days=200)
    _mkfile(root / "sub", "ancient_scan.pdf", size=9000, age_days=2000)

    seen = []
    calls = []
    def anon(p):
        calls.append(p)
        return f"clean:{Path(p).name}"

    r1 = si.run_sweep(str(root), anonymize_fn=anon,
                      on_progress=lambda n: seen.append(n))
    assert r1["indexed"] == 5
    assert r1["skipped"] == 0
    assert r1["deferred"] == 0 and r1["failed"] == 0
    assert len(calls) == 5                       # each file certified exactly once
    assert seen == [1, 2, 3, 4, 5]               # progress fired per indexed file

    # Re-sweep: content_hash of each unchanged file is already indexed → all skip.
    r2 = si.run_sweep(str(root), anonymize_fn=anon)
    assert r2["indexed"] == 0 and r2["skipped"] == 5
    assert len(calls) == 5                        # model fn never re-invoked


def test_e2e_order_matches_sweep_order_helper(home, tmp_path):
    """The order run_sweep actually processes files in equals _sweep_order — i.e.
    the loop swapped its source to the helper. Proven by capturing the sequence
    the anonymize_fn is called in and comparing to the helper's file order."""
    root = tmp_path / "docs"; root.mkdir()
    _mkfile(root, "recent_small.txt", size=10, age_days=1)
    _mkfile(root, "recent_big.pdf", size=50_000, age_days=1)
    _mkfile(root, "old_small.txt", size=10, age_days=800)

    processed = []
    def anon(p):
        processed.append(Path(p).name)
        return "clean"
    si.run_sweep(str(root), anonymize_fn=anon)

    # Expected = helper order, filtered the same way the loop filters (files that
    # actually get indexed): is_file and not ignorable. This tree has only plain
    # documents, but filter explicitly so the check can't pass by coincidence.
    root_p = Path(os.path.expanduser(str(root))).resolve()
    expected = [p.name for p in si._sweep_order(root_p.rglob("*"))
                if p.is_file() and not si._is_ignorable(p)]
    assert processed == expected
