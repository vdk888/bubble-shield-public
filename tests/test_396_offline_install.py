"""#396 — the desktop app installs OFFLINE from vendored wheels on stock 3.9.6.

THE BUG (real, client-reported): a CGP client hit a "Python missing" error on the
`install-app.sh` one-liner. Two causes:
  (A) install-app.sh hard-required Python >= 3.10; stock macOS ships
      /usr/bin/python3 as 3.9.6 -> die. (Fixed: gate lowered to 3.9 after proving
      the whole app import graph runs on 3.9.6, see test_396_app_runs_on_py39.)
  (B) even past the gate, deps were pip-installed from PyPI -> a locked-down or
      offline client could not install. (Fixed: wheels vendored under
      vendor/wheels/, installed with --no-index.)

This test proves (B): a fresh stock-3.9.6 venv installs the full dependency set
from ONLY the bundled wheels, with the network forced off (PIP_NO_INDEX=1 +
--no-index). If the wheel set is incomplete or an annotation drags in a
compiled-only dep, this fails.

It targets /usr/bin/python3 specifically — the interpreter a clean client Mac
actually has — so a dev machine with python3.12 + internet can't mask a gap.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WHEELS = REPO / "vendor" / "wheels"
STOCK_PY = "/usr/bin/python3"

# Same dependency set install-app.sh installs.
APP_DEPS = ["fastapi", "uvicorn", "pywebview", "jinja2", "pypdf", "python-multipart"]

pytestmark = pytest.mark.skipif(
    not Path(STOCK_PY).exists() or not WHEELS.is_dir()
    or not any(WHEELS.glob("*.whl")),
    reason="needs stock /usr/bin/python3 and committed vendor/wheels/*.whl",
)


def test_offline_install_from_vendored_wheels(tmp_path):
    """Create a stock-3.9 venv and install every app dep with no network."""
    venv = tmp_path / "venv"
    subprocess.run([STOCK_PY, "-m", "venv", str(venv)], check=True, timeout=120)
    py = venv / "bin" / "python"

    env = dict(os.environ)
    env["PIP_NO_INDEX"] = "1"          # belt: refuse any index
    env.pop("PIP_INDEX_URL", None)
    env.pop("PIP_EXTRA_INDEX_URL", None)

    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--no-index",
         "--find-links", str(WHEELS), *APP_DEPS],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, (
        "offline wheel install failed — wheel set likely incomplete:\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )

    # The installed env must actually import the app (3.9 runtime, real deps).
    check = subprocess.run(
        [str(py), "-c",
         "import sys; sys.path.insert(0, %r);"
         "import webapp.app, fastapi, uvicorn, jinja2, pypdf, multipart, webview;"
         "print('OK')" % str(REPO)],
        env={**env, "BUBBLE_SHIELD_HOME": str(tmp_path / "bshome")},
        capture_output=True, text=True, timeout=120,
    )
    assert check.returncode == 0 and "OK" in check.stdout, (
        f"app failed to import on the offline-installed 3.9 venv:\n{check.stderr}"
    )
