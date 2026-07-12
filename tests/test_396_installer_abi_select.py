"""#396b — install-app.sh must pick the interpreter matching the STAGED WHEEL
ABI, not "newest python on PATH".

THE BUG (real, found by running the published one-liner end-to-end): the old
candidate loop preferred the newest pythonN.M it could find. vendor/wheels/ only
holds cp39-tagged compiled wheels (pyobjc, pydantic-core, markupsafe). On a Mac
that has e.g. Homebrew python3.12 ranking ahead of stock /usr/bin/python3 (3.9.6)
— plausible even for a non-technical CGP client who once installed *anything* —
the installer picked 3.12, then the offline `pip install --no-index
--find-links=vendor/wheels` failed:
    ERROR: No matching distribution found for pyobjc-core
with no PyPI fallback. Stock-only Macs were fine; mixed Macs broke silently.

THE FIX: derive the supported ABI(s) from the actual wheel filenames and pick an
interpreter whose ABI is in that set, regardless of PATH order. This test proves
it by running the REAL installer with a newer, ABI-MISMATCHED interpreter
prepended to PATH and asserting (a) the install still succeeds offline and (b) the
venv it built is the cp39-ABI interpreter — i.e. it did NOT take the newer one.

A test machine without a second (non-cp39) interpreter ahead of stock can't
exhibit the bug, so the test skips cleanly there.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WHEELS = REPO / "vendor" / "wheels"
INSTALLER = REPO / "install-app.sh"
STOCK_PY = "/usr/bin/python3"


def _interp_minor(exe: str):
    """Return (major, minor) for an interpreter path/name, or None."""
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        a, b = out.stdout.split()
        return int(a), int(b)
    except ValueError:
        return None


def _staged_abis():
    """Parse compiled-wheel ABI tags (cpXY) from vendor/wheels filenames."""
    abis = set()
    for whl in WHEELS.glob("*.whl"):
        m = re.search(r"cp(\d)(\d+)-cp\d+", whl.name)
        if m:
            abis.add((int(m.group(1)), int(m.group(2))))
    return abis


def _find_mismatched_newer_interp():
    """Find a python3.N on PATH whose ABI is NOT in the staged set (the kind of
    interpreter that triggered the bug). Returns its directory + (maj,min), or
    None if none exists on this machine."""
    staged = _staged_abis()
    for name in ("python3.13", "python3.12", "python3.11", "python3.10"):
        exe = shutil.which(name)
        if not exe:
            continue
        ver = _interp_minor(exe)
        if ver and ver not in staged and ver >= (3, 9):
            return Path(exe).parent, ver
    return None


_STAGED = _staged_abis()
_MISMATCH = _find_mismatched_newer_interp()

pytestmark = pytest.mark.skipif(
    not Path(STOCK_PY).exists()
    or not WHEELS.is_dir()
    or not any(WHEELS.glob("*.whl"))
    or not _STAGED
    or _MISMATCH is None,
    reason="needs stock /usr/bin/python3, committed cp-tagged wheels, AND a "
           "second ABI-mismatched python3.N on PATH to exhibit the bug",
)


def test_installer_picks_abi_matching_interp_not_newest(tmp_path):
    """The exact STATUS.md repro: a newer, ABI-mismatched interpreter ranks first
    on PATH. The installer must still install offline by selecting the cp39
    interpreter, NOT the newer one."""
    mismatch_dir, mismatch_ver = _MISMATCH
    home = tmp_path / "home"
    home.mkdir()
    (home / "Desktop").mkdir()
    app_dir = home / ".bubble_shield_app"

    # Force the mismatched interpreter's dir to the FRONT of PATH (the bug repro).
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_REPO"] = str(REPO)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)
    env["PATH"] = f"{mismatch_dir}:{env.get('PATH', '')}"

    r = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env, capture_output=True, text=True, timeout=400,
    )
    assert r.returncode == 0, (
        f"installer FAILED with python{mismatch_ver[0]}.{mismatch_ver[1]} ahead "
        f"of stock on PATH — the #396b bug:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )

    # The venv must exist and its interpreter must be an ABI-MATCHED one (in the
    # staged set), proving the installer did NOT pick the newer mismatched python.
    venv_py = app_dir / ".venv" / "bin" / "python"
    assert venv_py.exists(), "venv interpreter missing"
    chosen = _interp_minor(str(venv_py))
    assert chosen in _STAGED, (
        f"installer built a venv on python{chosen} which is NOT in the staged "
        f"wheel ABI set {_STAGED} — it should have selected an ABI match "
        f"(would have failed offline otherwise)"
    )
    assert chosen != mismatch_ver, (
        f"installer chose the mismatched newer interpreter {mismatch_ver} despite "
        "an ABI-matching one being available"
    )

    # And the .app bundle was actually produced (full end-to-end success).
    assert (home / "Desktop" / "Bubble Shield.app").exists(), ".app not created"
