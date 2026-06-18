#!/bin/bash
# RepairDesk launcher — sets up a virtual environment on first run, then starts the app.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ or with: brew install python"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "First-time setup: creating virtual environment…"
  "$PY" -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  echo "Installing dependencies…"
  ./.venv/bin/pip install -r requirements.txt
fi

echo "Starting RepairDesk…"
exec ./.venv/bin/python app.py
