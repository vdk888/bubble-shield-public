"""#396 — the desktop app must import + construct all routes on stock Python 3.9.

THE BUG: install-app.sh required Python >= 3.10 because webapp/app.py used
`UploadFile | None` in a FastAPI endpoint signature. FastAPI resolves endpoint
annotations at app-construction time (even with `from __future__ import
annotations`), and `type | None` raises TypeError on 3.9 -> the app crashed at
import. Stock macOS only has /usr/bin/python3 = 3.9.6, so any client without a
newer Python was blocked.

FIX: `UploadFile | None` -> `Optional[UploadFile]`, gate lowered to 3.9.

This test imports webapp.app under /usr/bin/python3 (3.9.x) using the deps from
vendor/wheels (installed into a throwaway venv) and asserts every FastAPI route
constructs. If anyone reintroduces a `X | Y` runtime annotation in a route
signature, this fails on 3.9 even though it would pass on a 3.12 dev machine.

Guards Problem A of #396 against regression.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WHEELS = REPO / "vendor" / "wheels"
STOCK_PY = "/usr/bin/python3"

pytestmark = pytest.mark.skipif(
    not Path(STOCK_PY).exists() or not WHEELS.is_dir()
    or not any(WHEELS.glob("*.whl")),
    reason="needs stock /usr/bin/python3 and committed vendor/wheels/*.whl",
)


def _stock_is_py39_or_310plus():
    r = subprocess.run([STOCK_PY, "-c", "import sys;print(sys.version_info[:2])"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def test_app_imports_and_routes_construct_on_stock_python(tmp_path):
    venv = tmp_path / "venv"
    subprocess.run([STOCK_PY, "-m", "venv", str(venv)], check=True, timeout=120)
    py = venv / "bin" / "python"
    env = dict(os.environ)
    env["PIP_NO_INDEX"] = "1"
    env.pop("PIP_INDEX_URL", None)
    env["BUBBLE_SHIELD_HOME"] = str(tmp_path / "bshome")

    # Install just what's needed to construct the app (fastapi pulls its chain).
    inst = subprocess.run(
        [str(py), "-m", "pip", "install", "--no-index", "--find-links", str(WHEELS),
         "fastapi", "jinja2", "pypdf", "python-multipart"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert inst.returncode == 0, f"dep install failed:\n{inst.stderr}"

    r = subprocess.run(
        [str(py), "-c",
         "import sys; sys.path.insert(0, %r);"
         "import webapp.app as m;"
         "from fastapi.routing import APIRoute;"
         "n=sum(1 for x in m.app.routes if isinstance(x, APIRoute));"
         "assert n>=20, n;"
         "print('ROUTES_OK', n)" % str(REPO)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    pyver = _stock_is_py39_or_310plus()
    assert r.returncode == 0, (
        f"webapp.app failed to construct on stock python {pyver}:\n{r.stderr}"
    )
    assert "ROUTES_OK" in r.stdout, r.stdout
