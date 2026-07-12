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

# =============================================================================
# #604 zero-prereq / #venv-py312 — BARE-CLIENT-MAC PYTHON 3.12 PROVISIONING
# -----------------------------------------------------------------------------
# The ML accuracy pack (GLiNER ml-env, Gemma gemma-env, OCR ocr-env — created by
# plugin/bubble-shield/scripts/bubble_shield_setup_ml.py + bubble_shield_setup_ocr.py)
# PINS its venvs to Python 3.12 (find_python312(): searches `python3.12` on PATH
# generically; raises a clear error if absent). This is deliberate: stock macOS
# ships only Python 3.9.6, which (a) caused LibreSSL warnings + env flakiness and
# (b) BLOCKED the Gemma vision path (mlx_vlm needs 3.10+).
#
# PROBLEM ON A BARE CLIENT MAC: a fresh client Mac has ONLY /usr/bin/python3
# (3.9), NO Homebrew, NO python3.12. So bubble_shield_setup_ml.py would (correctly)
# fail-loud with "Python 3.12 required but no python3.12 on PATH". We PROVISION a
# 3.12 runtime WITHOUT requiring the client to install Homebrew (this build
# machine's 3.12 lives at /opt/homebrew — a client won't have that, and we must
# NOT hardcode it).
#
# APPROACH (Option A — python-build-standalone):
#   Download a RELOCATABLE prebuilt CPython 3.12 tarball from
#   astral-sh/python-build-standalone (apple-darwin, install_only), verify its
#   pinned SHA256, extract into ~/.bubble_shield/py312/, and expose its
#   bin/python3.12 on PATH so the generic `shutil.which("python3.12")` lookup in
#   the ML setup scripts finds it. No system install, no admin, no Homebrew.
#   ~15MB download. If a real `python3.12` is already on PATH (dev Mac with
#   Homebrew, or a previously-provisioned standalone), we reuse it and skip the
#   download entirely.
#
# PINNED RELEASE (do NOT bump to "latest" — unpinned == unreproducible + a
# supply-chain risk). Both the release tag AND the SHA256 are hardcoded.
#   Release tag : 20250723  (github.com/astral-sh/python-build-standalone/releases/tag/20250723)
#   CPython     : 3.12.11
#   SHA256SUMS  : .../releases/download/20250723/SHA256SUMS  (source of the hashes below)
#
# The LAUNCHER-app venv below is a SEPARATE concern (cp39 wheels) and is
# intentionally NOT moved to 3.12 here.
# -----------------------------------------------------------------------------
PBS_TAG="20250723"
PBS_CPYTHON="3.12.11"
PY312_ROOT="$HOME/.bubble_shield/py312"        # extraction target
PY312_BIN_DIR="$PY312_ROOT/python/bin"          # where install_only lands python3.12
PY312_BIN="$PY312_BIN_DIR/python3.12"

# Return the download URL + expected SHA256 for the current arch on stdout as
# "<url>\t<sha256>". Dies on an unsupported arch (we only ship darwin arm64/x86_64).
py312_asset() {
  local arch triple url sha
  arch="$(uname -m)"
  case "$arch" in
    arm64|aarch64)
      triple="aarch64-apple-darwin"
      sha="141272e6c6ae945b61fcf4073b7419451f8227187b3667b01ea9ec8993e0d7e9"
      ;;
    x86_64)
      triple="x86_64-apple-darwin"
      sha="1f152ee0dcc6ac5db93e39d74f0c50e319863d65fea0aab04e2e1b3f49b87f5f"
      ;;
    *)
      die "architecture non supportée pour la provision de Python 3.12 : '$arch' (attendu arm64 ou x86_64)."
      ;;
  esac
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PBS_CPYTHON}+${PBS_TAG}-${triple}-install_only.tar.gz"
  printf '%s\t%s\n' "$url" "$sha"
}

# Provision a Python 3.12 interpreter and make it discoverable to the ML/OCR
# setup steps (which resolve `python3.12` via shutil.which() on PATH).
#   1. If a real python3.12 is already on PATH (dev Mac / Homebrew / previously
#      provisioned) → reuse it, skip the download.
#   2. Else if we already extracted one at $PY312_BIN in a prior run → reuse it.
#   3. Else download the pinned python-build-standalone tarball for this arch,
#      verify its SHA256, extract into $PY312_ROOT, and prepend its bin/ to PATH.
# FAILS LOUD on missing network / checksum mismatch — never silently proceeds to
# a broken 3.9 ML setup.
provision_python312() {
  # (1) Already on PATH? Verify it really reports 3.12 before trusting it.
  if command -v python3.12 >/dev/null 2>&1; then
    local onpath ver
    onpath="$(command -v python3.12)"
    ver="$("$onpath" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    if [ "$ver" = "3.12" ]; then
      say "Python 3.12 déjà disponible ($onpath) — provision ignorée."
      return 0
    fi
  fi

  # (2) Previously provisioned standalone still present and valid?
  if [ -x "$PY312_BIN" ] && [ "$("$PY312_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)" = "3.12" ]; then
    say "Python 3.12 autonome déjà installé ($PY312_BIN) — réutilisation."
    export PATH="$PY312_BIN_DIR:$PATH"
    return 0
  fi

  # (3) Download + verify + extract the pinned relocatable CPython 3.12.
  command -v curl >/dev/null 2>&1 || die "curl introuvable ; impossible de télécharger Python 3.12."
  local asset url sha tmpd tarball actual
  asset="$(py312_asset)"            # "<url>\t<sha256>" (or dies on bad arch)
  url="${asset%$'\t'*}"
  sha="${asset#*$'\t'}"

  say "Provision de Python 3.12 (${PBS_CPYTHON}, autonome, sans Homebrew ni admin)…"
  tmpd="$(mktemp -d "${TMPDIR:-/tmp}/bubble-shield-py312.XXXXXX")" || die "échec de la création d'un répertoire temporaire."
  # shellcheck disable=SC2064
  trap "rm -rf '$tmpd'" RETURN
  tarball="$tmpd/py312.tar.gz"

  if ! curl -fsSL --max-time 300 -o "$tarball" "$url"; then
    die "échec du téléchargement de Python 3.12 depuis $url — vérifiez votre connexion internet (le pack de précision ML nécessite Python 3.12)."
  fi

  # Verify the pinned SHA256 (shasum is stock on macOS). Refuse to proceed on any
  # mismatch — a wrong hash means a corrupt download or a tampered artifact.
  actual="$(shasum -a 256 "$tarball" | awk '{print $1}')"
  if [ "$actual" != "$sha" ]; then
    die "somme de contrôle SHA256 invalide pour Python 3.12 (attendu $sha, obtenu $actual) — téléchargement corrompu ou altéré ; installation interrompue."
  fi
  say "Somme de contrôle Python 3.12 vérifiée."

  # Extract into $PY312_ROOT. The install_only tarball unpacks a single top-level
  # `python/` dir, so $PY312_ROOT/python/bin/python3.12 is the interpreter. Clear
  # any stale/partial prior extraction first so we never mix versions.
  rm -rf "$PY312_ROOT"
  mkdir -p "$PY312_ROOT" || die "impossible de créer $PY312_ROOT."
  tar -xzf "$tarball" -C "$PY312_ROOT" || die "échec de l'extraction de Python 3.12."

  [ -x "$PY312_BIN" ] || die "Python 3.12 extrait mais introuvable à $PY312_BIN (disposition de l'archive inattendue)."
  local got
  got="$("$PY312_BIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
  case "$got" in
    3.12.*) : ;;
    *) die "l'interpréteur Python 3.12 provisionné ne démarre pas correctement (version rapportée : '${got:-aucune}')." ;;
  esac

  # Prepend to PATH so the ML/OCR setup steps' shutil.which("python3.12") find it.
  export PATH="$PY312_BIN_DIR:$PATH"
  say "Python 3.12 autonome installé dans $PY312_ROOT (python $got)."
}

# Provision 3.12 NOW, before any ML/OCR setup step that needs it. This only puts
# python3.12 on PATH for the remainder of THIS install run; the launcher-app venv
# built below deliberately uses the cp39-ABI interpreter selected further down,
# NOT this 3.12 (the two runtimes are independent by design).
provision_python312
#
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

# Stale/wrong-ABI venv guard (fix for the live 2026-07-07 ResolutionImpossible
# bug): install-app.sh is idempotent — `git pull` above updates the app in
# place, and historically an EXISTING .venv was always reused as-is, with no
# check that it actually matches the interpreter we just selected. A client
# who first installed on a Mac where e.g. Homebrew python3.12 shadowed stock
# python3 (the #396b case) ends up with a .venv built for 3.12, holding old
# unpinned deps from before constraints.txt existed (pywebview 6.x, etc). On
# the next update, this script would try to `pip install --no-index` the
# vendored cp39-only wheels INTO that 3.12 venv — cp39 wheels can't install
# under 3.12, and the pre-existing pywebview 6.x conflicts with the pinned
# pywebview==3.4, so pip fails with "ResolutionImpossible". Reusing a venv is
# only safe when its Python ABI matches the interpreter chosen above ($PY).
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  EXISTING_VER="$(py_version "$APP_DIR/.venv/bin/python")" || EXISTING_VER=""
  SELECTED_VER="$(py_version "$PY")" || SELECTED_VER=""
  if [ -z "$EXISTING_VER" ] || [ "$EXISTING_VER" != "$SELECTED_VER" ]; then
    say "environnement Python incompatible détecté, reconstruction…"
    rm -rf "$APP_DIR/.venv"
  fi
fi

# Rebuilding a venv is cheap (the wheels/PyPI packages are re-installed right
# below regardless) and always safe — this only ever removes $APP_DIR/.venv,
# never the app checkout or any user data. Only create it if it isn't already
# there (either never existed, or was just removed above as stale) — a
# matching-ABI venv from a prior install is reused untouched (fast path).
if [ ! -d "$APP_DIR/.venv" ]; then
  "$PY" -m venv "$APP_DIR/.venv" || die "échec de la création du venv."
fi
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

# 3b. Install + load the background-sweep LaunchAgent.
# The sweep (bubble_shield_sweep.py) re-indexes the protected document root into
# the shadow store on a schedule so the read path stays zero-model. We ship a
# .plist TEMPLATE and substitute the app python, the protected root, $HOME and
# the interval here — mirroring the nerd LaunchAgent's write-then-(unload/load)
# pattern from bubble_shield_setup_ml.py. NON-FATAL: a launchctl failure must not
# break the already-installed app (the sweep is a background optimisation).
#
# __INTERVAL__ is emitted into the template as a BARE <integer> — a plist
# StartInterval must be an integer, never a quoted string. The singleton lock in
# the sweep makes an overlapping StartInterval fire a safe no-op, so no
# concurrency handling is needed here.
SWEEP_TPL="$APP_DIR/plugin/bubble-shield/launcher/com.bubbleinvest.bubble-shield-sweep.plist.tpl"
SWEEP_LABEL="com.bubbleinvest.bubble-shield-sweep"
SWEEP_PLIST="$HOME/Library/LaunchAgents/$SWEEP_LABEL.plist"
# The folder the sweep indexes; overridable for testing. Default: the shared
# document root the reviewer works from. The lock guards overlap; an unset/empty
# root just means the sweep no-ops on nothing until configured.
SWEEP_ROOT="${BUBBLE_SHIELD_SWEEP_ROOT:-$HOME/.bubble_shield/protected}"
SWEEP_INTERVAL="${BUBBLE_SHIELD_SWEEP_INTERVAL:-1200}"   # seconds; default 20 min
if [ -f "$SWEEP_TPL" ]; then
  say "Installation de la tâche d'indexation en arrière-plan (sweep)…"
  mkdir -p "$HOME/.bubble_shield" "$HOME/Library/LaunchAgents"
  sed \
    -e "s|__PYTHON__|$APP_DIR/.venv/bin/python|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__ROOT__|$SWEEP_ROOT|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__INTERVAL__|$SWEEP_INTERVAL|g" \
    "$SWEEP_TPL" > "$SWEEP_PLIST"
  # reload (unload-then-load) so a re-run picks up changes; ignore unload errors.
  launchctl unload "$SWEEP_PLIST" >/dev/null 2>&1 || true
  if launchctl load "$SWEEP_PLIST" >/dev/null 2>&1; then
    say "Tâche d'indexation en arrière-plan installée ($SWEEP_LABEL, toutes les $SWEEP_INTERVAL s)."
  else
    say "Note : la tâche d'indexation en arrière-plan a été écrite mais n'a pas pu être chargée ; l'application fonctionne normalement."
  fi
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
