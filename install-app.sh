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

# 1. Clone or pull (idempotent self-update). git clone handles both a git URL and
# a local path. We do this BEFORE picking the interpreter, because the Python
# choice depends on the wheel ABIs staged under vendor/wheels/ (only present
# after the clone).
if [ -d "$APP_DIR/.git" ]; then
  say "Mise à jour de l'application…"
  git -C "$APP_DIR" pull --ff-only || die "échec de la mise à jour (git pull)."
else
  say "Installation de l'application…"
  git clone "$REPO_URL" "$APP_DIR" || die "échec du clonage depuis $REPO_URL."
fi

# --- Python interpreter selection -------------------------------------------
# We install dependencies OFFLINE from prebuilt wheels in vendor/wheels/. The
# COMPILED wheels there (pyobjc, pydantic-core, markupsafe) are ABI-locked: each
# is tagged for a single CPython ABI (e.g. cp39). pip can only install such a
# wheel under an interpreter whose ABI matches that tag. So the interpreter we
# pick is NOT "newest is best" — it MUST match the staged wheel ABI, or the
# offline install fails with "No matching distribution found for pyobjc-core".
#
# #396b bug (the one this block fixes): the old loop preferred the newest
# pythonN.M on PATH. On a client Mac that happens to have e.g. Homebrew
# python3.12 ahead of stock /usr/bin/python3 (3.9.6) — plausible even for a
# non-technical user who once installed *anything* — it picked 3.12, then the
# cp39-only offline wheels could not install. Stock-only Macs were fine; mixed
# Macs broke silently.
#
# Fix: derive the supported ABI(s) dynamically from the actual wheel filenames
# (the source of truth — if the wheel set is ever re-staged for a different ABI,
# this adapts automatically, no magic "39" hardcoded), then prefer an
# interpreter whose ABI is in that set, regardless of PATH order. Stock
# /usr/bin/python3 is NOT hardcoded as the *winner* — it's just added to the
# candidate set by its canonical path so it stays discoverable even when a
# Homebrew/pyenv `python3` shadows it on PATH (the common real case: `python3`
# resolves to 3.12/3.14 while the cp39 wheels need the stock 3.9 underneath).
# An interpreter is chosen ONLY because its ABI matches the staged wheels.

WHEELS="$APP_DIR/vendor/wheels"
# Exact-version pins matching the vendored wheels. Applied to BOTH the offline
# and online-fallback pip installs so they resolve identically (keeps the online
# fallback from pulling newer, API-incompatible releases like pywebview 6.x).
CONSTRAINTS="$APP_DIR/constraints.txt"

# Collect the set of CPython ABI tags the compiled wheels are built for, parsed
# from filenames like `pyobjc_core-11.1-cp39-cp39-macosx_10_9_universal2.whl`.
# Emits space-separated "major minor" pairs, e.g. "3 9". Empty if no compiled
# wheels are staged yet (pre-clone or pure-python-only set).
wheel_abis() {
  [ -d "$WHEELS" ] || return 0
  ls "$WHEELS"/*.whl 2>/dev/null \
    | grep -oE 'cp[0-9]+-cp[0-9]+' \
    | sed -E 's/^cp([0-9])([0-9]+)-.*/\1 \2/' \
    | sort -u
}

py_version() {  # echo "major minor" for an interpreter, or nothing on failure
  "$1" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null
}

# Resolve a candidate (name or path) to its real interpreter path, for dedup.
resolve_py() {
  command -v "$1" 2>/dev/null
}

ABIS="$(wheel_abis)"   # may be empty (no compiled wheels staged)

PY=""
PY_ONLINE_FALLBACK=0   # set to 1 if we had to pick an ABI-mismatched interpreter
NEWEST_PY=""           # newest >=3.9 interpreter seen, for the fallback path
SEEN_PATHS=" "         # space-delimited set of already-probed resolved paths

# Candidate list: explicit pythonN.M names + bare python3 (whatever's first on
# PATH) + the canonical stock path /usr/bin/python3. The stock absolute path is
# included so a shadowed-but-ABI-matching stock interpreter is still found; it is
# NOT preferred — it only wins if its ABI matches the staged wheels.
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3 /usr/bin/python3; do
  rp="$(resolve_py "$cand")" || true
  [ -n "${rp:-}" ] || continue
  case "$SEEN_PATHS" in *" $rp "*) continue ;; esac   # already probed this exe
  SEEN_PATHS="$SEEN_PATHS$rp "
  ver="$(py_version "$cand")" || continue
  [ -n "$ver" ] || continue
  set -- $ver; cmaj="$1"; cmin="$2"
  # Must be >= 3.9 regardless (the app's verified floor).
  if [ "$cmaj" -lt 3 ] || { [ "$cmaj" -eq 3 ] && [ "$cmin" -lt 9 ]; }; then
    continue
  fi
  # Track the newest >=3.9 interpreter for the online fallback.
  NEWEST_PY="${NEWEST_PY:-$cand}"
  # If we have a staged-ABI requirement, only accept a matching interpreter.
  if [ -n "$ABIS" ]; then
    if printf '%s\n' "$ABIS" | grep -qx "$cmaj $cmin"; then
      PY="$cand"; break          # ABI match — use this one, ignore PATH order
    fi
    continue                     # not a match; keep looking
  fi
  # No compiled wheels staged → ABI is irrelevant, first >=3.9 wins.
  PY="$cand"; break
done

if [ -z "$PY" ]; then
  # No interpreter matched the staged wheel ABI. Two sub-cases:
  if [ -n "$NEWEST_PY" ]; then
    # A usable (>=3.9) interpreter EXISTS, it just doesn't match the offline
    # wheel ABI (e.g. client has only python3.11+, no 3.9 anywhere). Rather than
    # hard-fail — failing to install at all is worse than one client needing the
    # network once — fall back to that interpreter and install ONLINE from PyPI.
    # This is the documented residual path for the rare ABI-mismatch-only Mac.
    PY="$NEWEST_PY"
    PY_ONLINE_FALLBACK=1
    say "Aucun interpréteur Python compatible avec les paquets hors-ligne (ABI cp$(printf '%s' "$ABIS" | tr -d ' ' | tr '\n' '/')) n'a été trouvé ; utilisation de $("$PY" --version 2>&1) avec installation en ligne depuis PyPI."
  else
    # No usable Python at all.
    die "Python 3.9 ou plus récent introuvable. macOS en fournit normalement un via les outils Xcode : exécutez 'xcode-select --install' puis relancez."
  fi
fi

# 2. venv + deps.
# Default path = OFFLINE from vendored wheels (no PyPI access required):
# vendor/wheels/ holds prebuilt arm64 wheels matching the interpreter ABI we
# selected above, so install works on a locked-down/offline client Mac.
# Fallback path (PY_ONLINE_FALLBACK=1) = the rare Mac with no ABI-matching
# interpreter: install ONLINE from PyPI with the best interpreter we found.
say "Préparation de l'environnement Python ($("$PY" --version 2>&1))…"
"$PY" -m venv "$APP_DIR/.venv" || die "échec de la création du venv."
if [ "$PY_ONLINE_FALLBACK" -eq 0 ] && [ -d "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
  # Offline path: install ONLY from the bundled wheels, never touch the network.
  "$APP_DIR/.venv/bin/python" -m pip install --quiet --no-index --find-links="$WHEELS" \
    -c "$CONSTRAINTS" \
    fastapi uvicorn pywebview jinja2 pypdf python-multipart \
    || die "échec de l'installation des dépendances (wheels hors-ligne)."
else
  # Online path: either no vendored wheels present, or the only interpreters
  # available don't match the staged wheel ABI (PY_ONLINE_FALLBACK). Drop
  # --no-index and install from PyPI. Requires network, by design, for this
  # specific fallback only.
  say "Installation des dépendances depuis PyPI (connexion requise)…"
  "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip || die "échec de pip upgrade."
  "$APP_DIR/.venv/bin/python" -m pip install --quiet -c "$CONSTRAINTS" \
    fastapi uvicorn pywebview jinja2 pypdf python-multipart \
    || die "échec de l'installation des dépendances (PyPI). Vérifiez votre connexion internet."
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

# 4. Install Claude Code CLI (support/audit tool).
# This is NOT part of the client's Shield runtime — it is a support tool so the
# Bubble team can run Claude Code directly ON this machine (outside Cowork) to
# audit that Shield is correctly installed. The team authenticates per-session
# (`claude login`) when they audit; no credentials are stored here.
# NON-FATAL by design: if this step fails (offline, network policy, etc.), the
# Shield app above is already fully installed — a Claude Code install failure
# must never break or roll back the client's Shield installation.
if command -v claude >/dev/null 2>&1; then
  say "Claude Code déjà présent (outil de support) — ignoré."
else
  say "Installation de Claude Code (outil de support pour l'audit)…"
  # Log the official installer's output to a file (not /dev/null) so the team can
  # diagnose a failed audit-tool install remotely without re-running interactively.
  # --max-time caps a hung network so step 4 fails fast instead of stalling the
  # (already-complete) Shield install.
  CC_LOG="${TMPDIR:-/tmp}/bubble-shield-claude-install.log"
  if curl -fsSL --max-time 120 https://claude.ai/install.sh 2>"$CC_LOG" | bash >>"$CC_LOG" 2>&1 && command -v claude >/dev/null 2>&1; then
    say "Claude Code installé (outil de support)."
  else
    # Do not die — the Shield install already succeeded. Just note it.
    say "Note : Claude Code (outil de support) n'a pas pu être installé automatiquement ; l'application Bubble Shield fonctionne normalement. L'équipe l'installera manuellement si nécessaire (journal : $CC_LOG)."
  fi
fi
