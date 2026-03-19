#!/usr/bin/env bash
# Pi2 — Update Script
# Pulls latest code from git and restarts services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/timelapse"

echo "=== Pi2 Update ==="

cd "$REPO_DIR"
echo "[1/3] Pulling latest code..."
git pull --ff-only

echo "[2/3] Deploying updated files..."
cp "$SCRIPT_DIR/camera_server.py" "$INSTALL_DIR/camera_server.py"
cp "$SCRIPT_DIR/camera-server.service" /etc/systemd/system/
cp "$SCRIPT_DIR/wifi-retry.service" /etc/systemd/system/

echo "[3/3] Restarting services..."
systemctl daemon-reload
systemctl restart camera-server.service
systemctl restart wifi-retry.service

echo "=== Pi2 update complete ==="
