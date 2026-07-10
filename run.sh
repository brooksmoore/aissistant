#!/bin/bash
# Start one assistant instance in the foreground (Ctrl+C to stop).
#   bash run.sh penny     bash run.sh jarvis
# For always-on operation use:  bash install_autostart.sh <instance>
cd "$(dirname "$0")"
export AISSISTANT_INSTANCE="${1:-penny}"
echo "starting instance: $AISSISTANT_INSTANCE"
exec ./venv/bin/python bot.py
