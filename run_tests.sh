#!/bin/bash
# Penny test suite.
#   bash run_tests.sh          — unit tests only (free, seconds)
#   bash run_tests.sh --live   — unit + live model tests (~3-5 cents)
cd "$(dirname "$0")"
echo "── unit suite (free) ──"
./venv/bin/python -m unittest discover tests -v 2>&1 | tail -5
if [ "$1" = "--live" ]; then
  echo ""
  echo "── live suite (real API, budget-capped) ──"
  ./venv/bin/python tests/test_live.py
fi
