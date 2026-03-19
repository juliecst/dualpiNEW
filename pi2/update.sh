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
cp "$SCRIPT_DIR/grabber.py" "$INSTALL_DIR/grabber.py"
cp "$SCRIPT_DIR/backup.sh" "$INSTALL_DIR/backup.sh"
cp "$SCRIPT_DIR/portal/portal.py" "$INSTALL_DIR/portal.py"
cp "$REPO_DIR/common/disk_monitor.sh" "$INSTALL_DIR/disk_monitor.sh"

cp "$SCRIPT_DIR/grabber.service" /etc/systemd/system/
cp "$SCRIPT_DIR/playback.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.timer" /etc/systemd/system/
cp "$SCRIPT_DIR/portal/portal.service" /etc/systemd/system/
cp "$SCRIPT_DIR/wifi-retry.service" /etc/systemd/system/

echo "[3/3] Restarting services..."
systemctl daemon-reload
systemctl restart grabber.service
systemctl restart playback.service
systemctl restart portal.service
systemctl restart wifi-retry.service
# backup.timer doesn't need restart (timer-triggered)

echo "=== Pi2 update complete ==="
