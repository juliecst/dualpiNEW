#!/usr/bin/env bash
# Pi2 — Camera Node Setup (Idempotent)
# Run as root on a fresh Raspberry Pi OS Bookworm 64-bit install.
# This script:
#   1. Installs rpicam-apps and Python dependencies
#   2. Configures WiFi client to connect to Pi1's AP
#   3. Deploys and enables systemd services
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/timelapse"
CONFIG_DIR="/data"

echo "=== Pi2 Camera Node Setup ==="

# --- 1. System packages ---
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    rpicam-apps \
    python3 \
    python3-yaml \
    python3-pip

# --- 2. Deploy application files ---
echo "[2/5] Deploying application files..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
cp "$SCRIPT_DIR/camera_server.py" "$INSTALL_DIR/camera_server.py"
chmod +x "$INSTALL_DIR/camera_server.py"

# Deploy default config if none exists
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$REPO_DIR/config/config.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Deployed default config.yaml"
fi

# --- 3. Configure WiFi client ---
echo "[3/5] Configuring WiFi client..."

# Read AP credentials from config (with fallback defaults)
WIFI_SSID="timelapse-ap"
WIFI_PASS="changeme2"
if command -v python3 &>/dev/null && [ -f "$CONFIG_DIR/config.yaml" ]; then
    WIFI_SSID=$(python3 -c "
import yaml
with open('$CONFIG_DIR/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('network', {}).get('ap_ssid', 'timelapse-ap'))
" 2>/dev/null || echo "timelapse-ap")
    WIFI_PASS=$(python3 -c "
import yaml
with open('$CONFIG_DIR/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('network', {}).get('ap_password', 'changeme2'))
" 2>/dev/null || echo "changeme2")
fi

# Configure wpa_supplicant for WiFi client mode
WPA_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"
cat > "$WPA_CONF" <<EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASS"
    key_mgmt=WPA-PSK
    priority=1
}
EOF
chmod 600 "$WPA_CONF"
echo "  WiFi configured for SSID: $WIFI_SSID"

# Set static IP for wlan0 via dhcpcd
DHCPCD_CONF="/etc/dhcpcd.conf"
if ! grep -q "interface wlan0" "$DHCPCD_CONF" 2>/dev/null; then
    cat >> "$DHCPCD_CONF" <<EOF

# Pi2 static IP on timelapse AP network
interface wlan0
static ip_address=192.168.50.20/24
static routers=192.168.50.1
static domain_name_servers=192.168.50.1
EOF
    echo "  Static IP 192.168.50.20 configured"
fi

# --- 4. Install systemd services ---
echo "[4/5] Installing systemd services..."
cp "$SCRIPT_DIR/camera-server.service" /etc/systemd/system/
cp "$SCRIPT_DIR/wifi-retry.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable camera-server.service
systemctl enable wifi-retry.service

# --- 5. Configure tmpfs for minimal SD card wear ---
echo "[5/5] Configuring tmpfs..."
if ! grep -q "tmpfs /tmp" /etc/fstab 2>/dev/null; then
    echo "tmpfs /tmp tmpfs defaults,noatime,nosuid,size=100m 0 0" >> /etc/fstab
fi
if ! grep -q "tmpfs /var/log" /etc/fstab 2>/dev/null; then
    echo "tmpfs /var/log tmpfs defaults,noatime,nosuid,size=50m 0 0" >> /etc/fstab
fi

echo ""
echo "=== Pi2 setup complete ==="
echo "Services installed: camera-server, wifi-retry"
echo "Reboot to activate: sudo reboot"
