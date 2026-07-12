import os, subprocess, sys, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # the bubble-shield repo root (dev = has both)

def test_launcher_boots_with_vendored_engine_on_pythonpath(tmp_path):
    # Simulate the installed layout's PYTHONPATH: repo root + vendored engine dir.
    vendor = REPO / "plugin" / "bubble-shield" / "vendor"
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{vendor}"
    env["BUBBLE_SHIELD_HEADLESS"] = "1"
    env["BUBBLE_SHIELD_HOME"] = str(tmp_path)
    p = subprocess.Popen([sys.executable, "-m", "launcher"],
                         cwd=str(REPO), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # poll the url file the launcher writes
        url = None
        for _ in range(60):
            f = tmp_path / "launcher.url"
            if f.exists():
                url = f.read_text().strip(); break
            time.sleep(0.5)
        assert url, "launcher did not report a URL (failed to boot)"
        with urllib.request.urlopen(f"{url}/review", timeout=3) as r:
            assert r.status == 200
    finally:
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()
