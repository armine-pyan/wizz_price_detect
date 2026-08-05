#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

if [ ! -f .env ]; then
  echo "Missing .env file. Copy .env.example to .env and add your Telegram values."
  exit 1
fi

set -a
source .env
set +a

python tracker.py
