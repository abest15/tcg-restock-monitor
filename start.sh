#!/usr/bin/env bash
cd "$(dirname "$0")"
while true; do
  .venv/bin/python -m restockbot run
  echo "Monitor stopped; restarting in 30s (Ctrl+C to quit)"; sleep 30
done
