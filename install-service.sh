#!/usr/bin/env bash
# Linux / Raspberry Pi: run the monitor as a systemd service that starts on boot and restarts on failure.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
sudo tee /etc/systemd/system/tcg-restock.service >/dev/null <<UNIT
[Unit]
Description=TCG Restock Monitor
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m restockbot run
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tcg-restock
echo "Installed. Logs: journalctl -u tcg-restock -f   (or data/restockbot.log)"
