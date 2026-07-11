#!/bin/bash
# One-time setup: creates a private Python environment and installs everything.
set -e
cd "$(dirname "$0")"

PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
echo "Using Python: $PY ($($PY --version))"

$PY -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# each install lives under instances/<name>/ — that .env is the ONLY one loaded
INSTANCE="${1:-penny}"
ENV_FILE="instances/$INSTANCE/.env"
mkdir -p "instances/$INSTANCE"
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  echo ""
  echo "✅ Installed. NEXT: open $ENV_FILE and fill in"
  echo "   TELEGRAM_TOKEN and ANTHROPIC_API_KEY (README.md steps 2-3),"
  echo "   then start the bot with:  bash run.sh $INSTANCE"
else
  echo "✅ Installed. Start the bot with:  bash run.sh $INSTANCE"
fi
