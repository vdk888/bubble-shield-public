#!/bin/bash
# uninstall-app.sh — one-line UNINSTALLER for Bubble Shield (symmetric with install-app.sh).
# Usage (reviewer Mac):
#   curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/uninstall-app.sh | bash
# Also wipe LOCAL vaults + models (you lose this machine's decloak ability):
#   curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/uninstall-app.sh | bash -s -- --purge-data
# Local test:
#   BUBBLE_SHIELD_REPO=/path/to/checkout bash uninstall-app.sh
#
# This wraps plugin/bubble-shield/scripts/uninstall_user_hooks.py (card #383): the ONLY
# clean way to remove the live host footprint (PreToolUse guard hooks in
# ~/.claude/settings.json, the STABLE_DIR host scripts, the LaunchAgent, caches). It NEVER
# touches shared cabinet config — that safety lives in the python uninstaller's
# _is_shared_path, and this wrapper mirrors it for its own one direct rm (APP_DIR).
set -euo pipefail

REPO_URL="${BUBBLE_SHIELD_REPO:-https://github.com/vdk888/bubble-shield-public.git}"
APP_DIR="${BUBBLE_SHIELD_APP_DIR:-$HOME/.bubble_shield_app}"

PURGE_DATA=""
for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA="--purge-data" ;;
    *) ;;  # ignore unknown args (e.g. when piped via `bash -s --`)
  esac
done

say() { printf '\n[Bubble Shield] %s\n' "$1"; }
die() { printf '\n[Bubble Shield] ERREUR : %s\n' "$1" >&2; exit 1; }

# Pick a Python the SAME WAY install-app.sh does: an explicit python3.1x >= 3.10, else
# python3 only if it is >= 3.10. The uninstaller uses 3.10+ type syntax (`X | None`).
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || die "Python 3.10 ou plus récent introuvable (la version système 3.9 ne suffit pas). Installez-le : brew install python@3.12 puis relancez."

# ── SHARED-CONFIG SAFETY for our ONE direct rm (APP_DIR) ──────────────────────
# The python uninstaller refuses to delete any shared cabinet store (Dropbox / Google
# Drive / OneDrive / iCloud / Box, or a `.bubble-shield.json`-marked folder) via its
# _is_shared_path — but that guard does NOT protect OUR direct `rm -rf "$APP_DIR"`.
# APP_DIR defaults to ~/.bubble_shield_app (local-only). If a user set
# BUBBLE_SHIELD_APP_DIR to a shared path, mirror the python hints and REFUSE the rm.
SHARED_MARKER=".bubble-shield.json"
_is_shared_app_dir() {
  local p="$1"
  # cloud-sync root anywhere in the path (mirror _SHARED_ROOT_HINTS).
  case "/$p/" in
    */Dropbox/*|*/"Google Drive"/*|*/OneDrive/*|*/"iCloud Drive"/*|*/Box/*) return 0 ;;
  esac
  # .bubble-shield.json cabinet marker on the dir itself or any ancestor.
  local cur="$p"
  while :; do
    [ -f "$cur/$SHARED_MARKER" ] && return 0
    local parent
    parent="$(dirname "$cur")"
    [ "$parent" = "$cur" ] && break
    cur="$parent"
  done
  return 1
}

# ── 1) Locate the uninstaller: prefer the cloned APP_DIR; else fetch from the repo ──
UNINSTALLER="$APP_DIR/plugin/bubble-shield/scripts/uninstall_user_hooks.py"
TMP_CLONE=""
cleanup() { [ -n "$TMP_CLONE" ] && rm -rf "$TMP_CLONE" 2>/dev/null || true; }
trap cleanup EXIT

if [ -f "$UNINSTALLER" ]; then
  say "Désinstallation à partir de l'application installée ($APP_DIR)…"
else
  say "Application introuvable dans $APP_DIR — récupération du désinstalleur depuis le dépôt…"
  command -v git >/dev/null 2>&1 || die "git introuvable. Installez les outils en ligne de commande Xcode (xcode-select --install)."
  TMP_CLONE="$(mktemp -d "${TMPDIR:-/tmp}/bubble-shield-uninstall.XXXXXX")"
  git clone --depth 1 "$REPO_URL" "$TMP_CLONE" >/dev/null 2>&1 \
    || die "échec du clonage depuis $REPO_URL (impossible de récupérer le désinstalleur)."
  UNINSTALLER="$TMP_CLONE/plugin/bubble-shield/scripts/uninstall_user_hooks.py"
  [ -f "$UNINSTALLER" ] || die "désinstalleur introuvable dans le dépôt ($UNINSTALLER)."
fi

# ── 2) Run the python uninstaller (the shared-config-safe removals live here) ──
# NO --purge-data by default → vaults in ~/.bubble_shield are KEPT. Pass-through flag
# forwards --purge-data so an advanced user can wipe LOCAL data too.
say "Suppression de l'empreinte locale (hooks, scripts hôte, LaunchAgent, caches)…"
"$PY" "$UNINSTALLER" $PURGE_DATA || die "échec du désinstalleur ($UNINSTALLER)."

# ── 3) Belt-and-suspenders: remove APP_DIR (the install-app.sh clone + venv) ──
# uninstall_user_hooks.py already removes ~/.bubble_shield_app (its _app_dir step), so
# this is usually a no-op — but if APP_DIR was overridden we make sure it's gone.
# GUARD: never rm a shared/Dropbox-marked APP_DIR (the python guard doesn't cover OUR rm).
if [ -d "$APP_DIR" ]; then
  if _is_shared_app_dir "$APP_DIR"; then
    say "ATTENTION : APP_DIR ($APP_DIR) ressemble à un dossier partagé (Dropbox/marqueur cabinet) — NON supprimé."
  else
    rm -rf "$APP_DIR" || true
    say "Dossier de l'application supprimé : $APP_DIR"
  fi
fi

# ── 4) Final message: removed / kept / Cowork-side half this host script can't touch ──
say "Désinstallation terminée."
printf '\n'
printf '  Supprimé : hooks Bubble Shield (~/.claude/settings.json), scripts hôte\n'
printf '             (~/.claude/bubble-shield/), LaunchAgent, caches, ~/.config/bubble_shield,\n'
printf '             et l’application (~/.bubble_shield_app + Bureau).\n'
if [ -n "$PURGE_DATA" ]; then
  printf '  Supprimé aussi : vos coffres LOCAUX (~/.bubble_shield) — capacité de décloaking perdue.\n'
else
  printf '  Conservé : vos coffres LOCAUX (~/.bubble_shield). Pour les supprimer : relancez avec --purge-data.\n'
fi
printf '  Jamais touché : la config PARTAGÉE du cabinet (dossier Dropbox marqué .bubble-shield.json).\n'
printf '\n'
printf '  ⚠️  IL RESTE UNE ÉTAPE côté Cowork (que ce script ne peut pas faire) :\n'
printf '      Cowork → Customize → Plugins → désactivez / supprimez « bubble-shield »,\n'
printf '      puis /reload-plugins (ou redémarrez Cowork).\n'
printf '\n'
