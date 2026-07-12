import os, subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REAL_UNINSTALLER = REPO / "plugin" / "bubble-shield" / "scripts" / "uninstall_user_hooks.py"


def _seed_app_dir(app_dir: Path):
    """Populate a fake APP_DIR with the REAL uninstaller (so it runs for real) plus a
    sentinel file, mirroring the install-app.sh clone layout."""
    scripts = app_dir / "plugin" / "bubble-shield" / "scripts"
    scripts.mkdir(parents=True)
    # Copy the real uninstaller + its install-side dependency (it imports install_user_hooks).
    (scripts / "uninstall_user_hooks.py").write_text(REAL_UNINSTALLER.read_text())
    install_src = REAL_UNINSTALLER.parent / "install_user_hooks.py"
    (scripts / "install_user_hooks.py").write_text(install_src.read_text())
    (app_dir / "sentinel.txt").write_text("app code")


def test_uninstall_removes_app_dir(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    app_dir = home / ".bubble_shield_app"
    _seed_app_dir(app_dir)
    # A KEPT vault dir — must survive without --purge-data.
    vault = home / ".bubble_shield"; vault.mkdir()
    (vault / "vault.txt").write_text("secret mappings")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)
    # conftest sets BUBBLE_SHIELD_HOME to a throwaway dir; the uninstaller honours it for
    # _data_dir(). Point it at THIS test's vault so the assertion is about a real path.
    env["BUBBLE_SHIELD_HOME"] = str(vault)

    r = subprocess.run(["bash", str(REPO / "uninstall-app.sh")],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"uninstaller failed: {r.stdout}\n{r.stderr}"
    assert not app_dir.exists(), "APP_DIR not removed"
    # vaults KEPT without --purge-data
    assert vault.exists() and (vault / "vault.txt").exists(), "vault wrongly removed"


def test_uninstall_purge_data_removes_vault(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    app_dir = home / ".bubble_shield_app"
    _seed_app_dir(app_dir)
    vault = home / ".bubble_shield"; vault.mkdir()
    (vault / "vault.txt").write_text("secret mappings")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)
    env["BUBBLE_SHIELD_HOME"] = str(vault)  # override conftest's throwaway dir → this vault

    r = subprocess.run(["bash", str(REPO / "uninstall-app.sh"), "--purge-data"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"uninstaller failed: {r.stdout}\n{r.stderr}"
    assert not app_dir.exists(), "APP_DIR not removed"
    assert not vault.exists(), "--purge-data did not remove the LOCAL vault"


def test_uninstall_idempotent(tmp_path):
    """Running again with APP_DIR already gone must still exit 0 (fallback fetch path
    is NOT exercised here — we point BUBBLE_SHIELD_REPO at the local checkout)."""
    home = tmp_path / "home"; home.mkdir()
    app_dir = home / ".bubble_shield_app"
    _seed_app_dir(app_dir)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)
    env["BUBBLE_SHIELD_REPO"] = str(REPO)  # local checkout for the fallback clone
    for _ in range(2):
        r = subprocess.run(["bash", str(REPO / "uninstall-app.sh")],
                           env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"uninstaller failed: {r.stdout}\n{r.stderr}"
    assert not app_dir.exists()


# ── 🔴 THE NON-NEGOTIABLE SAFETY TEST ─────────────────────────────────────────
def test_uninstall_refuses_shared_dropbox_app_dir(tmp_path):
    """If BUBBLE_SHIELD_APP_DIR points under a fake Dropbox/ (a SHARED cabinet store),
    the wrapper's direct `rm -rf "$APP_DIR"` MUST be refused — the Dropbox folder and a
    sentinel inside it must SURVIVE. The python uninstaller's _is_shared_path doesn't
    cover OUR rm, so the wrapper mirrors the cloud-root hints itself."""
    home = tmp_path / "home"; home.mkdir()
    # APP_DIR lives UNDER a Dropbox root → shared, off-limits to our rm.
    dropbox = home / "Dropbox"; dropbox.mkdir()
    app_dir = dropbox / "cabinet-shield"
    _seed_app_dir(app_dir)
    # A sentinel inside the shared store — must survive.
    sentinel = dropbox / "SHARED_GAZETTEER.json"
    sentinel.write_text('{"shared":"cabinet config — never delete"}')

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)

    r = subprocess.run(["bash", str(REPO / "uninstall-app.sh")],
                       env=env, capture_output=True, text=True, timeout=120)
    # Exits 0 (hooks etc. were cleaned) but the Dropbox APP_DIR is left intact.
    assert r.returncode == 0, f"uninstaller errored: {r.stdout}\n{r.stderr}"
    assert dropbox.exists(), "shared Dropbox root was deleted!"
    assert sentinel.exists(), "shared cabinet sentinel was deleted!"
    assert app_dir.exists(), "shared Dropbox APP_DIR was deleted by our rm!"
    assert "NON supprimé" in r.stdout, "wrapper did not announce it refused the shared APP_DIR"


def test_uninstall_refuses_marker_app_dir(tmp_path):
    """Same protection via the `.bubble-shield.json` cabinet marker (not a Dropbox name):
    an APP_DIR carrying the marker is treated as shared and NOT rm'd."""
    home = tmp_path / "home"; home.mkdir()
    shared = home / "ClientShared"; shared.mkdir()
    (shared / ".bubble-shield.json").write_text("{}")  # cabinet marker on the parent
    app_dir = shared / "shield-app"
    _seed_app_dir(app_dir)
    sentinel = shared / "keep.txt"; sentinel.write_text("shared")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["BUBBLE_SHIELD_APP_DIR"] = str(app_dir)

    r = subprocess.run(["bash", str(REPO / "uninstall-app.sh")],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"uninstaller errored: {r.stdout}\n{r.stderr}"
    assert app_dir.exists(), "marker-protected APP_DIR was deleted!"
    assert sentinel.exists(), "shared sentinel deleted!"
    assert "NON supprimé" in r.stdout
