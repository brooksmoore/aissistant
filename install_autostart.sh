#!/bin/bash
# Auto-start one assistant instance at boot (and keep it alive).
#   bash install_autostart.sh penny            install/refresh
#   bash install_autostart.sh jarvis
#   bash install_autostart.sh penny remove     stop + uninstall
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="${1:?usage: install_autostart.sh <instance> [remove]}"
LABEL="com.aissistant.$INSTANCE"
PLIST=~/Library/LaunchAgents/$LABEL.plist

if [ "$2" = "remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✅ $INSTANCE stopped and auto-start removed."
  exit 0
fi

mkdir -p "$DIR/instances/$INSTANCE"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>EnvironmentVariables</key>
  <dict><key>AISSISTANT_INSTANCE</key><string>$INSTANCE</string></dict>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$DIR/venv/bin/python</string>
    <string>$DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/instances/$INSTANCE/assistant.log</string>
  <key>StandardErrorPath</key><string>$DIR/instances/$INSTANCE/assistant.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ $INSTANCE is running and will survive reboots."
echo "   Watch it:  tail -f $DIR/instances/$INSTANCE/assistant.log"
echo "   Stop it:   bash install_autostart.sh $INSTANCE remove"
