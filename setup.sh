#!/bin/bash
# One-time setup: creates a private Python environment and installs everything.
set -e
cd "$(dirname "$0")"

PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
echo "Using Python: $PY ($($PY --version))"

$PY -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "✅ Installed. NEXT: open the .env file in this folder and fill in"
  echo "   TELEGRAM_TOKEN and ANTHROPIC_API_KEY (README.md steps 2-3),"
  echo "   then start the bot with:  bash run.sh"
else
  echo "✅ Installed. Start the bot with:  bash run.sh"
fi
