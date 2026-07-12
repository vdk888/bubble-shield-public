import os, subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def test_install_script_local_source(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    desktop = home / "Desktop"; desktop.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_REPO"] = str(REPO)        # local source instead of GitHub
    env["BUBBLE_SHIELD_APP_DIR"] = str(home / ".bubble_shield_app")
    r = subprocess.run(["bash", str(REPO / "install-app.sh")],
                       env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"installer failed: {r.stdout}\n{r.stderr}"
    app = home / ".bubble_shield_app"
    assert (app / ".venv" / "bin" / "python").exists(), "venv not created"
    # The installer now ships a real .app bundle (not a bare .command).
    appbundle = desktop / "Bubble Shield.app"
    assert appbundle.exists(), "app bundle not created"
    exe = appbundle / "Contents" / "MacOS" / "BubbleShield"
    assert exe.exists(), ".app executable missing"
    body = exe.read_text()
    assert str(app) in body and "-m launcher" in body
    assert os.access(exe, os.X_OK), ".app executable not executable"
    assert (appbundle / "Contents" / "Info.plist").exists(), "Info.plist missing"

def test_install_script_idempotent(tmp_path):
    # running twice must not fail (pull + recreate)
    home = tmp_path / "home"; home.mkdir(); (home / "Desktop").mkdir()
    env = dict(os.environ); env["HOME"] = str(home)
    env["BUBBLE_SHIELD_REPO"] = str(REPO)
    env["BUBBLE_SHIELD_APP_DIR"] = str(home / ".bubble_shield_app")
    for _ in range(2):
        r = subprocess.run(["bash", str(REPO / "install-app.sh")], env=env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr
