#!/usr/bin/env bash
# Lumivara Sentinel - Linux/macOS launcher (creates .venv on first run,
# runs the setup wizard when .env is incomplete, then starts the bot).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
.venv/bin/python -m lumivara.setup_wizard --check || .venv/bin/python -m lumivara.setup_wizard
exec .venv/bin/python main.py
