#!/bin/bash
# Install/refresh the liveness watchdog (runs independently of both bot
# instances — checks them, is not checked by them).
#   bash install_heartbeat.sh            install/refresh, runs every 5 min
#   bash install_heartbeat.sh remove     stop + uninstall
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.aissistant.heartbeat"
PLIST=~/Library/LaunchAgents/$LABEL.plist

if [ "$1" = "remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✅ heartbeat stopped and auto-start removed."
  exit 0
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/venv/bin/python</string>
    <string>$DIR/heartbeat.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/heartbeat.log</string>
  <key>StandardErrorPath</key><string>$DIR/heartbeat.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ heartbeat is running (checks jarvis + penny every 5 min) and will survive reboots."
echo "   Watch it:  tail -f $DIR/heartbeat.log"
echo "   Stop it:   bash install_heartbeat.sh remove"
