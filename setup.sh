#!/usr/bin/env bash
# First-time setup on macOS / Linux / Raspberry Pi.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m restockbot setup
.venv/bin/python -m restockbot doctor
echo
echo "Setup finished. Start with: ./start.sh"
echo "Run 24/7 in the background (Linux/Raspberry Pi): ./install-service.sh"
