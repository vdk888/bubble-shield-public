"""tests/test_386_model_gc.py — #386 model disk dedup / gc.

Bubble Shield stored models REDUNDANTLY: snapshot_download staged a full copy in
the HF hub cache (~3.6GB of every onnx variant) while the daemon only loads a
~349MB subset localized under ~/.bubble_shield/models/. Dropped benchmark models
(fastino, #348) also piled up in the hub cache. This verifies the two-part fix:

  1. download_model purges the redundant hub-cache copy after localizing.
  2. gc() reclaims existing pile-up: hub-cache copies of localized models +
     known-dropped models (fastino) — while NEVER touching the local store, the
     live model, the load-bearing urchade PyTorch fallback, or unrelated
     third-party models sharing the HF cache.

EVERYTHING runs against a TMP FAKE structure (tmp hub cache + tmp local store).
The real ~/.cache/huggingface and the real ~/.bubble_shield are never touched —
no multi-GB model is ever pulled. All bytes are synthetic.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# Live model (ml.json) — must SURVIVE gc.
LIVE_MODEL = "onnx-community/gliner_multi_pii-v1"
LIVE_ONNX = "onnx/model_quantized.onnx"
# Daemon PyTorch GLiNER fallback — load-bearing, must SURVIVE gc.
URCHADE = "urchade/gliner_multi_pii-v1"
# Dropped benchmark model (#348) — must be REMOVED by gc.
FASTINO = "fastino/gliner2-privacy-filter-PII-multi"
# Unrelated third-party model sharing the HF cache — must SURVIVE gc.
UNRELATED = "black-forest-labs/FLUX.2-klein-4B"


def _reload_under_home(monkeypatch, home: Path):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    import bubble_shield_setup_ml as ml
    importlib.reload(ml)
    return ml


def _make_local(home: Path, model_id: str, onnx_rel: str, mb: int = 1) -> None:
    """Localized model under ~/.bubble_shield/models/<org>__<name>/<onnx_rel>."""
    f = home / "models" / model_id.replace("/", "__") / onnx_rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * (mb * 1024 * 1024))
    (f.parent.parent / "config.json").write_text("{}", encoding="utf-8")


def _make_hub(hub_cache: Path, model_id: str, files: dict[str, int]) -> Path:
    """Fake hub-cache models--<org>--<name> dir with a blobs + snapshots layout.

    `files` maps a snapshot-relative filename → size in MB. Returns the model dir.
    Also writes a matching .locks/ entry to prove gc removes it too."""
    org_name = f"models--{model_id.replace('/', '--')}"
    model_dir = hub_cache / org_name
    snap = model_dir / "snapshots" / "deadbeef"
    snap.mkdir(parents=True, exist_ok=True)
    for rel, mb in files.items():
        p = snap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"y" * (mb * 1024 * 1024))
    (hub_cache / ".locks" / org_name).mkdir(parents=True, exist_ok=True)
    return model_dir


def _build_fake_world(tmp_path: Path):
    """Stand up a realistic fake: local store + hub cache mirroring Joris's Mac."""
    home = tmp_path / "bubble_shield_home"
    hub = tmp_path / "hf_hub_cache"
    home.mkdir(parents=True, exist_ok=True)
    hub.mkdir(parents=True, exist_ok=True)

    # Local store: the live model is localized (subset).
    _make_local(home, LIVE_MODEL, LIVE_ONNX, mb=2)

    # Hub cache:
    #  - onnx-community: the redundant staging copy of the localized live model
    #    (all the onnx variants — dead weight). REMOVE.
    _make_hub(hub, LIVE_MODEL, {
        "onnx/model.onnx": 4, "onnx/model_fp16.onnx": 3,
        "onnx/model_quantized.onnx": 2, "config.json": 1,
    })
    #  - urchade: the daemon's PyTorch fallback (pytorch_model.bin). KEEP.
    _make_hub(hub, URCHADE, {"pytorch_model.bin": 5, "gliner_config.json": 1})
    #  - fastino: dropped #348. REMOVE.
    _make_hub(hub, FASTINO, {"model.safetensors": 4, "config.json": 1})
    #  - unrelated third-party model. KEEP.
    _make_hub(hub, UNRELATED, {"flow.safetensors": 6})
    return home, hub


def _hub_model_dir(hub: Path, model_id: str) -> Path:
    return hub / f"models--{model_id.replace('/', '--')}"


def test_gc_removes_duplicate_and_dropped_keeps_live_and_fallback(tmp_path, monkeypatch):
    home, hub = _build_fake_world(tmp_path)
    ml = _reload_under_home(monkeypatch, home)

    summary = ml.gc(hub_cache=hub, dry_run=False)

    removed_ids = {r["model_id"] for r in summary["removed"]}
    # The redundant staging copy of the localized live model → gone.
    assert LIVE_MODEL in removed_ids
    # The dropped benchmark model → gone.
    assert FASTINO in removed_ids
    # Load-bearing PyTorch fallback + unrelated model → never targeted.
    assert URCHADE not in removed_ids
    assert UNRELATED not in removed_ids

    # On-disk reality matches the summary.
    assert not _hub_model_dir(hub, LIVE_MODEL).exists()
    assert not _hub_model_dir(hub, FASTINO).exists()
    assert _hub_model_dir(hub, URCHADE).is_dir()
    assert _hub_model_dir(hub, UNRELATED).is_dir()

    # .locks entries for the removed models are cleaned too.
    assert not (hub / ".locks" / f"models--{LIVE_MODEL.replace('/', '--')}").exists()
    assert not (hub / ".locks" / f"models--{FASTINO.replace('/', '--')}").exists()

    # CRITICAL: the local store / live model is completely untouched.
    live_onnx = home / "models" / LIVE_MODEL.replace("/", "__") / LIVE_ONNX
    assert live_onnx.is_file()
    assert live_onnx.stat().st_size == 2 * 1024 * 1024
    assert ml.model_present(LIVE_MODEL, LIVE_ONNX) is True

    # Reclaimed bytes = duplicate (10MB) + fastino (5MB) = 15MB.
    assert summary["bytes_freed"] == 15 * 1024 * 1024


def test_gc_dry_run_deletes_nothing(tmp_path, monkeypatch):
    home, hub = _build_fake_world(tmp_path)
    ml = _reload_under_home(monkeypatch, home)

    summary = ml.gc(hub_cache=hub, dry_run=True)

    # Reports the same targets…
    removed_ids = {r["model_id"] for r in summary["removed"]}
    assert removed_ids == {LIVE_MODEL, FASTINO}
    assert summary["dry_run"] is True
    # …but deletes nothing.
    assert _hub_model_dir(hub, LIVE_MODEL).is_dir()
    assert _hub_model_dir(hub, FASTINO).is_dir()
    assert _hub_model_dir(hub, URCHADE).is_dir()
    assert _hub_model_dir(hub, UNRELATED).is_dir()


def test_gc_never_targets_unlocalized_model(tmp_path, monkeypatch):
    """A hub-cache model that is NOT localized and NOT dropped is never removed.

    Guards the 'only delete a hub copy if confirmed-localized OR dropped' rule —
    e.g. a model present only in the hub cache (no local subset) is kept."""
    home = tmp_path / "bubble_shield_home"
    hub = tmp_path / "hf"
    home.mkdir(parents=True, exist_ok=True)
    hub.mkdir(parents=True, exist_ok=True)
    # Live model NOT localized (no local store entry), only in hub cache.
    _make_hub(hub, LIVE_MODEL, {"onnx/model_quantized.onnx": 2})
    ml = _reload_under_home(monkeypatch, home)

    summary = ml.gc(hub_cache=hub, dry_run=False)
    assert summary["removed"] == []
    assert _hub_model_dir(hub, LIVE_MODEL).is_dir()


def test_localized_requires_onnx_weight_not_just_config(tmp_path, monkeypatch):
    """A half-written local dir (config only, no .onnx) does NOT license deleting
    the hub-cache copy — prevents data loss on an interrupted download."""
    home = tmp_path / "bubble_shield_home"
    hub = tmp_path / "hf"
    home.mkdir(parents=True, exist_ok=True)
    hub.mkdir(parents=True, exist_ok=True)
    # Local dir exists but has NO onnx weight (only config).
    cfg = home / "models" / LIVE_MODEL.replace("/", "__") / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}", encoding="utf-8")
    _make_hub(hub, LIVE_MODEL, {"onnx/model_quantized.onnx": 2})
    ml = _reload_under_home(monkeypatch, home)

    assert ml._localized_model_ids() == set()
    summary = ml.gc(hub_cache=hub, dry_run=False)
    assert summary["removed"] == []
    assert _hub_model_dir(hub, LIVE_MODEL).is_dir()


def test_download_model_purges_hub_cache_copy(tmp_path, monkeypatch):
    """download_model purges the redundant hub-cache copy after localizing.

    We stub the subprocess that runs snapshot_download so no real model is
    pulled: the stub writes the local onnx subset AND simulates the legacy
    double-store by leaving a hub-cache copy behind. download_model must then
    remove that hub-cache copy and report bytes freed."""
    home = tmp_path / "bubble_shield_home"
    hub = tmp_path / "hf_hub"
    home.mkdir(parents=True, exist_ok=True)
    hub.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    ml = _reload_under_home(monkeypatch, home)

    local = ml._local_dir(LIVE_MODEL)

    def fake_run(cmd, *a, **k):
        # Simulate snapshot_download: localize the subset…
        (local / "onnx").mkdir(parents=True, exist_ok=True)
        (local / LIVE_ONNX).write_bytes(b"z" * (2 * 1024 * 1024))
        # …and the LEGACY double-store side effect (old hub behavior).
        _make_hub(hub, LIVE_MODEL, {"onnx/model.onnx": 4, "onnx/model_quantized.onnx": 2})

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(ml.subprocess, "run", fake_run)

    state = ml.download_model(Path("/fake/py"), LIVE_MODEL, LIVE_ONNX)
    assert state == "done"
    # Local subset is present…
    assert (local / LIVE_ONNX).is_file()
    # …and the redundant hub-cache copy is gone.
    assert not _hub_model_dir(hub, LIVE_MODEL).exists()
