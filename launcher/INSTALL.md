# Bubble Shield — Installation (desktop app)

The Bubble Shield desktop app (review / vault / gazetteer UI) runs from source with
plain Python 3.10+ and 6 dependencies. No pre-built `.app`, no Apple signing, no
Gatekeeper friction. Installable and updatable remotely from GitHub.

## One-line install (reviewer Mac)

```bash
curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/install-app.sh | bash
```

This:

1. Clones (or `git pull`s, if already installed) the public repo into `~/.bubble_shield_app/`.
2. Creates a Python virtualenv at `~/.bubble_shield_app/.venv` using the newest Python
   3.10+ on your machine (a stock Mac's system `python3` is 3.9, which the app cannot
   run — install a newer one with `brew install python@3.12` if needed).
3. Installs the 6 runtime deps: `fastapi uvicorn pywebview jinja2 pypdf python-multipart`.
4. Drops a `Bubble Shield.command` launcher on your Desktop.

**Re-run the same command any time to self-update** — it `git pull`s the latest code,
refreshes the venv, and re-drops the launcher. Idempotent: no duplicate installs.

## First launch

Double-click **Bubble Shield** on your Desktop. A `.command` file is not
Gatekeeper-blocked, so a normal double-click works. If macOS still shows a warning
the very first time, **right-click → Open** once (then double-click works thereafter).

The app reads/writes its vault and gazetteer DATA under `~/.bubble_shield/`
(separate from the app CODE in `~/.bubble_shield_app/`).

## Manual fallback (no one-liner)

```bash
git clone https://github.com/vdk888/bubble-shield-public.git ~/.bubble_shield_app
cd ~/.bubble_shield_app
python3.12 -m venv .venv   # any Python >= 3.10 (NOT the system 3.9)
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install fastapi uvicorn pywebview jinja2 pypdf python-multipart
# run it:
BUBBLE_SHIELD_HOME="$HOME/.bubble_shield" \
PYTHONPATH="$PWD:$PWD/plugin/bubble-shield/vendor" \
  .venv/bin/python -m launcher
```

## Uninstall

### One-line uninstall (recommended)

Symmetric with the install one-liner — wraps the uninstaller below and also removes the
app dir:

```bash
# remove local footprint, KEEP your vaults in ~/.bubble_shield
curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/uninstall-app.sh | bash

# also remove the LOCAL data dir (vaults + models) — you lose decloak ability
curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/uninstall-app.sh | bash -s -- --purge-data
```

It works even if `~/.bubble_shield_app` is already gone (it fetches the uninstaller from the
public repo). **Shared cabinet config is never touched.** Afterwards, also remove the plugin
in **Cowork → Customize → Plugins** (toggle off / remove) — the host script can't reach that
Cowork-side half.

### Full uninstall (plugin + host footprint)

`/plugin uninstall` removes ONLY the marketplace entry — it leaves the active hooks, the
host scripts, the LaunchAgent, the caches and your data behind. The one-liner above wraps
this script; you can also run it directly:

```bash
# remove local footprint, KEEP your vaults in ~/.bubble_shield
python3 plugin/bubble-shield/scripts/uninstall_user_hooks.py

# also remove the LOCAL data dir (vaults + models) — you lose decloak ability
python3 plugin/bubble-shield/scripts/uninstall_user_hooks.py --purge-data
```

It is idempotent (safe to re-run) and removes, all per-machine:

- the Bubble Shield hook entries in `~/.claude/settings.json` (matched by marker — **other
  hooks are preserved**),
- `~/.claude/bubble-shield/` (host scripts),
- the LaunchAgent `~/Library/LaunchAgents/com.bubbleinvest.bubble-shield-nerd.plist` (unload + rm),
- the plugin cache `~/.claude/plugins/cache/bubble-shield`,
- `~/.config/bubble_shield/` (local config),
- the desktop app: `~/.bubble_shield_app/`, `~/Desktop/Bubble Shield.app`, and any old
  `~/Desktop/Bubble Shield.command`.
- `~/.bubble_shield/` (vaults + models) **only with `--purge-data`**.

**SHARED config is NEVER touched.** When the cabinet shares its gazetteer / custom-fields /
policy via a Dropbox folder (marked with `.bubble-shield.json`), a single client's uninstall
will **refuse to delete or descend into it** — even with `--purge-data` — because that store
is owned by the cabinet and wiping it would destroy config for all CGPs. Vaults are always
local-only, so removing a local vault only loses *this machine's* decloak ability.

### App-only uninstall (manual)

```bash
rm -rf ~/.bubble_shield_app "$HOME/Desktop/Bubble Shield.app" "$HOME/Desktop/Bubble Shield.command"
```

(This leaves your vault/gazetteer DATA in `~/.bubble_shield/` untouched. Delete that
directory too if you also want to remove your stored data — but **never** delete a shared
Dropbox cabinet folder.)

## Local testing (developers)

Point the installer at a local checkout instead of GitHub:

```bash
BUBBLE_SHIELD_REPO=/path/to/checkout \
BUBBLE_SHIELD_APP_DIR=/tmp/bs_app \
  bash install-app.sh
```

`git clone` accepts a local path as the source, so the script's logic can be exercised
without touching GitHub.
