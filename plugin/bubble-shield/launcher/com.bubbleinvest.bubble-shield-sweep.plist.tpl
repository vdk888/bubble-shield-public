<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Bubble Shield background sweep LaunchAgent (Task 12).
     install-app.sh substitutes the __PLACEHOLDERS__ at install time:
       __PYTHON__   -> the venv python interpreter
       __APP_DIR__  -> the app checkout root (holds bubble_shield_sweep.py under
                       plugin/bubble-shield/scripts/)
       __ROOT__     -> the protected document root to sweep
       __HOME__     -> $HOME (for the log paths)
       __INTERVAL__ -> sweep cadence in whole SECONDS (default 1200 = 20 min)
     StartInterval fires every __INTERVAL__ seconds; the singleton lock in the
     sweep (Task 9) makes an overlapping fire a safe no-op, so a long first-day
     full index that outlasts the interval is fine. RunAtLoad kicks the first
     sweep at login. __INTERVAL__ is emitted as a BARE <integer> — a plist
     StartInterval must be an integer, never a quoted string. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bubbleinvest.bubble-shield-sweep</string>
  <key>ProgramArguments</key>
  <array>
    <string>__PYTHON__</string>
    <string>__APP_DIR__/plugin/bubble-shield/scripts/bubble_shield_sweep.py</string>
    <string>--root</string>
    <string>__ROOT__</string>
  </array>
  <key>StartInterval</key><integer>__INTERVAL__</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>__HOME__/.bubble_shield/sweep.log</string>
  <key>StandardErrorPath</key><string>__HOME__/.bubble_shield/sweep.log</string>
</dict>
</plist>
