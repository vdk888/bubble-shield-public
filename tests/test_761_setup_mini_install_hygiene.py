"""
test_761_setup_mini_install_hygiene.py — #761 re-install install hygiene.

BUG (#761): on a RE-install over a prior version, minid loaded STALE engine code
because setup_mini installed the engine to daemon/scripts/bubble_shield while
minid's own _vendor() resolves daemon/vendor — a path setup_mini never created.
If a stale daemon/vendor/bubble_shield existed (older layout), minid loaded THAT
(pre-fix) engine → _passphrase()=None → plaintext branch → 0 shadows → silent
read-tier failure. A stale *.pyc newer than fresh source could shadow it too.

This test drives _install_to_stable_path() against a tmp BUBBLE_SHIELD_HOME with
a pre-seeded stale engine AND a stale *.pyc, and asserts:
  1. The engine lands at the SAME path minid's _vendor() resolves (alignment).
  2. The old mismatched daemon/scripts/bubble_shield location is gone.
  3. No *.pyc / __pycache__ survives anywhere under the daemon tree.
  4. The stale daemon/vendor/bubble_shield content is replaced by fresh source.

launchctl is stubbed (no launchd in the test env); we exercise the copy + purge
+ path logic only, which is where #761 lives.
"""
import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "plugin" / "bubble-shield" / "scripts"
sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture()
def setup_mini(tmp_path, monkeypatch):
    """Import setup_mini with its module-level paths repointed under tmp_path,
    and launchctl stubbed to a no-op."""
    import bubble_shield_setup_mini as sm
    importlib.reload(sm)

    home = tmp_path / ".bubble_shield"
    monkeypatch.setattr(sm, "BUBBLE_SHIELD_HOME", home)
    monkeypatch.setattr(sm, "DAEMON_ROOT", home / "daemon")
    monkeypatch.setattr(sm, "DAEMON_SCRIPTS_DIR", home / "daemon" / "scripts")
    monkeypatch.setattr(sm, "DAEMON_VENDOR_DIR", home / "daemon" / "vendor")
    monkeypatch.setattr(sm, "DAEMON_ENGINE_DIR",
                        home / "daemon" / "vendor" / "bubble_shield")

    # launchctl → no-op (no launchd, and we must not touch the real user domain).
    def _fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(sm.subprocess, "run", _fake_run)
    return sm


def _minid_vendor_for(minid_path: Path) -> Path:
    """Replicate minid's own _vendor() = __file__.parent.parent / 'vendor'.
    This is the ground-truth path minid will sys.path.insert(0) at runtime."""
    return minid_path.resolve().parent.parent / "vendor"


def test_engine_lands_where_minid_vendor_resolves(setup_mini):
    dst_minid = setup_mini._install_to_stable_path()
    # minid's _vendor() → daemon/vendor; engine must be its bubble_shield child.
    vendor = _minid_vendor_for(dst_minid)
    engine = vendor / "bubble_shield"
    assert engine.is_dir(), f"engine not at minid's _vendor() path: {engine}"
    assert engine == setup_mini.DAEMON_ENGINE_DIR
    # sanity: the fresh engine actually has the module minid imports
    assert (engine / "shadow_store.py").is_file()


def test_reinstall_purges_stale_pyc_and_replaces_stale_engine(setup_mini):
    sm = setup_mini
    # --- seed a PRIOR install with the #761 traps ---
    # (a) a stale engine at minid's _vendor() path with sentinel content + a .pyc
    #     newer than any fresh source.
    stale_engine = sm.DAEMON_ENGINE_DIR
    stale_engine.mkdir(parents=True, exist_ok=True)
    (stale_engine / "shadow_store.py").write_text("STALE = True\n", encoding="utf-8")
    cache = stale_engine / "__pycache__"
    cache.mkdir()
    stale_pyc = cache / "shadow_store.cpython-39.pyc"
    stale_pyc.write_bytes(b"\x00stale-bytecode")
    # (b) a stale engine at the OLD mismatched location (daemon/scripts/bubble_shield)
    legacy = sm.DAEMON_SCRIPTS_DIR / "bubble_shield"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "shadow_store.py").write_text("LEGACY = True\n", encoding="utf-8")

    # --- re-install ---
    sm._install_to_stable_path()

    # 1. fresh engine replaced the stale one (sentinel gone).
    fresh = (sm.DAEMON_ENGINE_DIR / "shadow_store.py").read_text(encoding="utf-8")
    assert "STALE = True" not in fresh
    assert "def " in fresh  # real source, not the sentinel stub

    # 2. NO *.pyc / __pycache__ anywhere under the daemon tree.
    leftover_pyc = list(sm.DAEMON_ROOT.rglob("*.pyc"))
    leftover_cache = [p for p in sm.DAEMON_ROOT.rglob("__pycache__") if p.is_dir()]
    assert leftover_pyc == [], f"stale .pyc survived: {leftover_pyc}"
    assert leftover_cache == [], f"__pycache__ survived: {leftover_cache}"

    # 3. the OLD mismatched location is swept.
    assert not legacy.exists(), "legacy daemon/scripts/bubble_shield not removed"


def test_purge_pyc_helper_is_thorough(setup_mini):
    sm = setup_mini
    root = sm.DAEMON_ROOT
    (root / "a" / "__pycache__").mkdir(parents=True)
    (root / "a" / "__pycache__" / "m.cpython-39.pyc").write_bytes(b"x")
    (root / "b").mkdir()
    (root / "b" / "loose.pyc").write_bytes(b"x")
    n = sm._purge_pyc(root)
    assert n >= 1
    assert list(root.rglob("*.pyc")) == []
    assert [p for p in root.rglob("__pycache__") if p.is_dir()] == []


def test_self_check_flags_761_signature(setup_mini, capsys):
    sm = setup_mini
    # encrypted store WITH content but minid sees 0 shadows == the #761 bug.
    enc = sm.BUBBLE_SHIELD_HOME / "shield.db.enc"
    enc.parent.mkdir(parents=True, exist_ok=True)
    enc.write_bytes(b"E" * 8192)  # > 4096 floor → "populated"
    sm._self_check_engine_loaded({"ok": True, "shadow_count": 0})
    out = capsys.readouterr().out
    assert "#761 SIGNATURE" in out


def test_self_check_quiet_on_empty_store(setup_mini, capsys):
    sm = setup_mini
    # no enc store at all → 0 shadows is legitimate, must NOT raise the alarm.
    sm._self_check_engine_loaded({"ok": True, "shadow_count": 0})
    out = capsys.readouterr().out
    assert "#761 SIGNATURE" not in out


def test_self_check_quiet_when_shadows_present(setup_mini, capsys):
    sm = setup_mini
    enc = sm.BUBBLE_SHIELD_HOME / "shield.db.enc"
    enc.parent.mkdir(parents=True, exist_ok=True)
    enc.write_bytes(b"E" * 8192)
    sm._self_check_engine_loaded({"ok": True, "shadow_count": 5})
    out = capsys.readouterr().out
    assert "#761 SIGNATURE" not in out
