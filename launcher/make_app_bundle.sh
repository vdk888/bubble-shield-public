#!/bin/bash
# make_app_bundle.sh — build a lightweight "Bubble Shield.app" wrapper on the Desktop.
#
# Not a frozen PyInstaller app — just a native .app SHELL whose executable runs
# the installed venv launcher. Gives the client a real app icon + name + no
# terminal window, with NO Apple signing (unsigned → first launch needs a
# one-time right-click → Open).
#
# Usage:  make_app_bundle.sh <APP_DIR> <DEST_APP_PATH> <ICNS_PATH>
#   APP_DIR       = the install dir (e.g. ~/.bubble_shield_app) holding .venv + source
#   DEST_APP_PATH = where to create the .app (e.g. ~/Desktop/Bubble Shield.app)
#   ICNS_PATH     = path to the .icns icon to embed
set -euo pipefail

APP_DIR="$1"
DEST="$2"
ICNS="$3"

# Fresh bundle each time (idempotent — the installer regenerates on update).
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"

# The executable inside the .app: runs the installed venv launcher. No terminal
# is shown because a .app's MacOS executable runs without a visible shell window.
cat > "$DEST/Contents/MacOS/BubbleShield" <<EOF
#!/bin/bash
# Rosetta-proof native-arch launch: uname -m LIES under x86 translation, so use
# sysctl hw.optional.arm64 to detect real Apple Silicon and force the arm64 slice
# that matches the arm64 compiled wheels (else "incompatible architecture" crash).
if [ "\$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  TARGET_ARCH="arm64"
else
  TARGET_ARCH="x86_64"
fi
if [ "\$(/usr/bin/arch)" != "\$TARGET_ARCH" ] && [ -z "\${BS_ARCH_REEXEC:-}" ]; then
  export BS_ARCH_REEXEC=1
  exec /usr/bin/arch -"\$TARGET_ARCH" /bin/bash "\$0" "\$@"
fi
APP_DIR="$APP_DIR"
export BUBBLE_SHIELD_HOME="\$HOME/.bubble_shield"
export PYTHONPATH="\$APP_DIR:\$APP_DIR/plugin/bubble-shield/vendor"
exec /usr/bin/arch -"\$TARGET_ARCH" "\$APP_DIR/.venv/bin/python" -m launcher
EOF
chmod +x "$DEST/Contents/MacOS/BubbleShield"

# The icon.
if [ -f "$ICNS" ]; then
  cp "$ICNS" "$DEST/Contents/Resources/BubbleShield.icns"
fi

# Info.plist — the metadata that makes Finder show it as "Bubble Shield" with the icon.
cat > "$DEST/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Bubble Shield</string>
  <key>CFBundleDisplayName</key><string>Bubble Shield</string>
  <key>CFBundleIdentifier</key><string>invest.bubble.shield.launcher</string>
  <key>CFBundleVersion</key><string>1.23.8</string>
  <key>CFBundleShortVersionString</key><string>1.23.8</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>BubbleShield</string>
  <key>CFBundleIconFile</key><string>BubbleShield.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Nudge Finder to refresh the icon (the bundle is new on each build).
touch "$DEST"

echo "[Bubble Shield] Application créée : $DEST"
