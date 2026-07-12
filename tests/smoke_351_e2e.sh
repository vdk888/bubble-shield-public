#!/bin/bash
# Task 3 e2e smoke for #351 — runs ENTIRELY inside a temp HOME. Never touches real ~/.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=$(mktemp -d /tmp/bs-e2e-XXXXXX)
echo "### TMP=$TMP"
mkdir -p "$TMP/Desktop"

cleanup_pids=()
kill_pid() {
  local pid=$1
  [ -z "$pid" ] && return 0
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
}

poll_url() {
  local urlfile=$1 url=""
  for _ in $(seq 1 60); do
    if [ -f "$urlfile" ]; then url=$(tr -d '\n' < "$urlfile"); [ -n "$url" ] && break; fi
    sleep 0.5
  done
  echo "$url"
}

echo "============================================================"
echo "### STEP 1a: install-app.sh into temp HOME (local source)"
echo "============================================================"
HOME="$TMP" BUBBLE_SHIELD_REPO="$REPO" BUBBLE_SHIELD_APP_DIR="$TMP/.bubble_shield_app" \
  bash "$REPO/install-app.sh"
INSTALL_RC=$?
echo "### installer exit code: $INSTALL_RC"
[ $INSTALL_RC -ne 0 ] && { echo "FAIL: installer non-zero"; rm -rf "$TMP"; exit 1; }

echo "### installed layout:"
ls -la "$TMP/.bubble_shield_app/.venv/bin/python" 2>&1
ls -la "$TMP/Desktop/Bubble Shield.command" 2>&1

echo "============================================================"
echo "### STEP 1b: HAND-LAUNCH headless via installed venv + PYTHONPATH"
echo "============================================================"
# Launch python DIRECTLY (no subshell wrapper) so $! is the real python PID and a
# single SIGTERM reaches the launcher's headless signal handler -> clean stop().
cd "$TMP/.bubble_shield_app"
BUBBLE_SHIELD_HEADLESS=1 BUBBLE_SHIELD_HOME="$TMP/data" \
  PYTHONPATH="$TMP/.bubble_shield_app:$TMP/.bubble_shield_app/plugin/bubble-shield/vendor" \
  "$TMP/.bubble_shield_app/.venv/bin/python" -m launcher > "$TMP/hand.log" 2>&1 &
HAND_PID=$!
cd - >/dev/null
echo "### hand-launch pid: $HAND_PID"

URL=$(poll_url "$TMP/data/launcher.url")
echo "### launcher.url => '$URL'"
if [ -z "$URL" ]; then echo "FAIL: no URL"; cat "$TMP/hand.log"; kill_pid "$HAND_PID"; rm -rf "$TMP"; exit 1; fi
HAND_PORT=$(echo "$URL" | sed -E 's|.*:([0-9]+).*|\1|')
echo "### hand-launch port: $HAND_PORT"

echo "### curl $URL/review"
HAND_CODE=$(curl -s -o "$TMP/hand_review.html" -w '%{http_code}' "$URL/review")
echo "### /review HTTP status: $HAND_CODE"
echo "### server stdout:"; cat "$TMP/hand.log"

echo "### killing hand-launch pid $HAND_PID"
kill_pid "$HAND_PID"
sleep 1
ORPHAN_HAND=$(lsof -nP -iTCP:"$HAND_PORT" -sTCP:LISTEN 2>/dev/null)
echo "### orphan on port $HAND_PORT after kill: '${ORPHAN_HAND:-NONE}'"

echo "============================================================"
echo "### STEP 1c: BONUS — run the DROPPED .command in headless mode"
echo "###   (.command hardcodes BUBBLE_SHIELD_HOME=\$HOME/.bubble_shield => set HOME=\$TMP)"
echo "============================================================"
( HOME="$TMP" BUBBLE_SHIELD_HEADLESS=1 \
  bash "$TMP/Desktop/Bubble Shield.command" > "$TMP/cmd.log" 2>&1 ) &
CMD_PID=$!
echo "### .command pid: $CMD_PID"

CMD_URL=$(poll_url "$TMP/.bubble_shield/launcher.url")
echo "### .command launcher.url => '$CMD_URL'"
if [ -z "$CMD_URL" ]; then echo "FAIL: .command no URL"; cat "$TMP/cmd.log"; kill_pid "$CMD_PID"; rm -rf "$TMP"; exit 1; fi
CMD_PORT=$(echo "$CMD_URL" | sed -E 's|.*:([0-9]+).*|\1|')
echo "### .command port: $CMD_PORT"

echo "### curl $CMD_URL/review"
CMD_CODE=$(curl -s -o "$TMP/cmd_review.html" -w '%{http_code}' "$CMD_URL/review")
echo "### .command /review HTTP status: $CMD_CODE"
echo "### .command server stdout:"; cat "$TMP/cmd.log"

echo "### killing .command pid $CMD_PID (and any child python)"
# the .command uses exec, so $CMD_PID IS the python; but kill the tree to be safe
pkill -TERM -P "$CMD_PID" 2>/dev/null
kill_pid "$CMD_PID"
sleep 1
ORPHAN_CMD=$(lsof -nP -iTCP:"$CMD_PORT" -sTCP:LISTEN 2>/dev/null)
echo "### orphan on port $CMD_PORT after kill: '${ORPHAN_CMD:-NONE}'"

echo "============================================================"
echo "### FINAL VERDICT"
echo "============================================================"
echo "installer_exit=$INSTALL_RC"
echo "hand_review=$HAND_CODE hand_orphan='${ORPHAN_HAND:-NONE}'"
echo "command_review=$CMD_CODE command_orphan='${ORPHAN_CMD:-NONE}'"

# real-home pollution check
echo "### real-home pollution check:"
REAL_HOME="${HOME_REAL:-$HOME}"
[ -d "$REAL_HOME/.bubble_shield_app" ] && echo "WARN real app dir" || echo "no real ~/.bubble_shield_app"

PASS=1
[ "$HAND_CODE" = "200" ] || { echo "FAIL hand /review != 200"; PASS=0; }
[ "$CMD_CODE" = "200" ] || { echo "FAIL command /review != 200"; PASS=0; }
[ -z "$ORPHAN_HAND" ] || { echo "FAIL hand orphan"; PASS=0; }
[ -z "$ORPHAN_CMD" ] || { echo "FAIL command orphan"; PASS=0; }

echo "### cleaning temp HOME $TMP"
rm -rf "$TMP"

if [ "$PASS" = "1" ]; then echo "### ALL GREEN"; exit 0; else echo "### SMOKE FAILED"; exit 1; fi
