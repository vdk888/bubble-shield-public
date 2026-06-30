#!/bin/bash
# install-app.sh — one-line installer for the Bubble Shield desktop app.
# Usage (reviewer Mac):
#   curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/install-app.sh | bash
# Local test:
#   BUBBLE_SHIELD_REPO=/path/to/checkout bash install-app.sh
set -euo pipefail

REPO_URL="${BUBBLE_SHIELD_REPO:-https://github.com/vdk888/bubble-shield-public.git}"
APP_DIR="${BUBBLE_SHIELD_APP_DIR:-$HOME/.bubble_shield_app}"
DESKTOP="$HOME/Desktop"

say() { printf '\n[Bubble Shield] %s\n' "$1"; }
die() { printf '\n[Bubble Shield] ERREUR : %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git introuvable. Installez les outils en ligne de commande Xcode (xcode-select --install)."

# Pick a Python >= 3.9. Stock macOS ships /usr/bin/python3 as the Xcode 3.9.6
# build, and the app is verified 3.9-compatible (#396: the one `X | None` runtime
# annotation was replaced with typing.Optional, so nothing in the import graph
# needs 3.10+). Prefer a newer explicit pythonN.M if present, else fall back to a
# bare python3 that is >= 3.9. A client whose Mac was never used for dev work has
# ONLY 3.9.6 — this must NOT block them.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || die "Python 3.9 ou plus récent introuvable. macOS en fournit normalement un via les outils Xcode : exécutez 'xcode-select --install' puis relancez."

# 1. Clone or pull (idempotent self-update). git clone handles both a git URL and a local path.
if [ -d "$APP_DIR/.git" ]; then
  say "Mise à jour de l'application…"
  git -C "$APP_DIR" pull --ff-only || die "échec de la mise à jour (git pull)."
else
  say "Installation de l'application…"
  git clone "$REPO_URL" "$APP_DIR" || die "échec du clonage depuis $REPO_URL."
fi

# 2. venv + deps — OFFLINE from vendored wheels (no PyPI access required).
# vendor/wheels/ holds prebuilt arm64 / py3.9 wheels (pure-python + pyobjc Cocoa
# backend), so install works on a locked-down/offline client Mac after the clone.
say "Préparation de l'environnement Python ($("$PY" --version 2>&1))…"
"$PY" -m venv "$APP_DIR/.venv" || die "échec de la création du venv."
WHEELS="$APP_DIR/vendor/wheels"
if [ -d "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
  # Offline path: install ONLY from the bundled wheels, never touch the network.
  "$APP_DIR/.venv/bin/python" -m pip install --quiet --no-index --find-links="$WHEELS" \
    fastapi uvicorn pywebview jinja2 pypdf python-multipart \
    || die "échec de l'installation des dépendances (wheels hors-ligne)."
else
  # Fallback: no vendored wheels present → online install from PyPI.
  "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip || die "échec de pip upgrade."
  "$APP_DIR/.venv/bin/python" -m pip install --quiet fastapi uvicorn pywebview jinja2 pypdf python-multipart \
    || die "échec de l'installation des dépendances."
fi

# 3. Create a real .app on the Desktop (icon + name, no terminal)
say "Création de l'application sur le Bureau…"
mkdir -p "$DESKTOP"
MAKE_APP="$APP_DIR/launcher/make_app_bundle.sh"
ICNS="$APP_DIR/launcher/assets/BubbleShield.icns"
if [ -f "$MAKE_APP" ]; then
  bash "$MAKE_APP" "$APP_DIR" "$DESKTOP/Bubble Shield.app" "$ICNS" || die "échec de la création de l'application."
  rm -f "$DESKTOP/Bubble Shield.command"   # remove any old .command from a prior install
  say "Terminé. Double-cliquez « Bubble Shield » sur votre Bureau."
  say "(Au tout premier lancement : clic droit → Ouvrir, une seule fois.)"
else
  # Fallback: bare .command if the app builder is missing.
  TEMPLATE="$APP_DIR/launcher/templates/Bubble Shield.command.template"
  [ -f "$TEMPLATE" ] || die "modèle de lancement introuvable ($TEMPLATE)."
  sed "s|{{APP_DIR}}|$APP_DIR|g" "$TEMPLATE" > "$DESKTOP/Bubble Shield.command"
  chmod +x "$DESKTOP/Bubble Shield.command"
  say "Terminé. Double-cliquez « Bubble Shield » sur votre Bureau pour lancer l'application."
fi
