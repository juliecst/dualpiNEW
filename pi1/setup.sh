#!/usr/bin/env bash
# Pi1 — Camera + AP Master Setup (Idempotent)
# Run as root on a fresh Raspberry Pi OS Bookworm 64-bit install.
# This script:
#   1. Installs rpicam-apps and Python dependencies
#   2. Configures WiFi AP (hostapd + dnsmasq)
#   3. Deploys and enables systemd services (camera-server)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/timelapse"
CONFIG_DIR="/data"

echo "=== Pi1 Camera + AP Master Setup ==="

# --- 1. System packages ---
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    rpicam-apps \
    hostapd \
    dnsmasq \
    python3 \
    python3-yaml \
    python3-pip

# --- 2. Deploy application files ---
echo "[2/6] Deploying application files..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
cp "$SCRIPT_DIR/camera_server.py" "$INSTALL_DIR/camera_server.py"
cp "$REPO_DIR/common/disk_monitor.sh" "$INSTALL_DIR/disk_monitor.sh"
chmod +x "$INSTALL_DIR/camera_server.py" "$INSTALL_DIR/disk_monitor.sh"

# Deploy default config if none exists
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$REPO_DIR/config/config.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Deployed default config.yaml"
fi

# --- 3. Configure WiFi Access Point ---
echo "[3/6] Configuring WiFi AP (hostapd + dnsmasq)..."

# Ensure WiFi radio is unblocked
rfkill unblock wifi 2>/dev/null || true

# Read AP settings from config
AP_SSID="timelapse-ap"
AP_PASS="changeme2"
AP_CHANNEL="7"
if command -v python3 &>/dev/null && [ -f "$CONFIG_DIR/config.yaml" ]; then
    AP_SSID=$(python3 -c "
import yaml
with open('$CONFIG_DIR/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('network', {}).get('ap_ssid', 'timelapse-ap'))
" 2>/dev/null || echo "timelapse-ap")
    AP_PASS=$(python3 -c "
import yaml
with open('$CONFIG_DIR/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('network', {}).get('ap_password', 'changeme2'))
" 2>/dev/null || echo "changeme2")
    AP_CHANNEL=$(python3 -c "
import yaml
with open('$CONFIG_DIR/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('network', {}).get('ap_channel', 7))
" 2>/dev/null || echo "7")
fi

# On Bookworm, NetworkManager is the default. Tell it to ignore wlan0
# so hostapd can manage the AP interface directly.
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf <<'NMEOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
NMEOF
    systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager
    echo "  NetworkManager: wlan0 set to unmanaged"
fi

# Deploy hostapd config with credentials from config.yaml
cp "$SCRIPT_DIR/hostapd.conf" /etc/hostapd/hostapd.conf
sed -i "s/^ssid=.*/ssid=$AP_SSID/" /etc/hostapd/hostapd.conf
sed -i "s/^wpa_passphrase=.*/wpa_passphrase=$AP_PASS/" /etc/hostapd/hostapd.conf
sed -i "s/^channel=.*/channel=$AP_CHANNEL/" /etc/hostapd/hostapd.conf

# Point hostapd daemon config to our file
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

# Deploy dnsmasq config
cp "$SCRIPT_DIR/dnsmasq.conf" /etc/dnsmasq.d/timelapse.conf

# Set static IP for wlan0
# On Bookworm, dhcpcd may not be installed (NetworkManager is default).
# Use a dedicated systemd service to configure the IP before hostapd starts.
cp "$SCRIPT_DIR/ap-network.service" /etc/systemd/system/
# Update the IP address in the service file from config if needed
sed -i "s|192.168.50.1/24|${PI1_IP:-192.168.50.1}/24|" /etc/systemd/system/ap-network.service

# Also configure via dhcpcd if it is available (legacy / non-Bookworm systems)
DHCPCD_CONF="/etc/dhcpcd.conf"
if command -v dhcpcd &>/dev/null && [ -f "$DHCPCD_CONF" ]; then
    if ! grep -q "interface wlan0" "$DHCPCD_CONF" 2>/dev/null; then
        cat >> "$DHCPCD_CONF" <<EOF

# Pi1 AP static IP
interface wlan0
static ip_address=192.168.50.1/24
nohook wpa_supplicant
EOF
        echo "  Static IP 192.168.50.1 configured via dhcpcd"
    fi
fi

# Unmask and enable hostapd
systemctl unmask hostapd 2>/dev/null || true
systemctl enable hostapd
systemctl enable dnsmasq

# --- 4. Install systemd services ---
echo "[4/6] Installing systemd services..."
cp "$SCRIPT_DIR/camera-server.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable ap-network.service
systemctl enable camera-server.service

# --- 5. Configure tmpfs for minimal SD card wear ---
echo "[5/6] Configuring tmpfs..."
if ! grep -q "tmpfs /tmp" /etc/fstab 2>/dev/null; then
    echo "tmpfs /tmp tmpfs defaults,noatime,nosuid,size=100m 0 0" >> /etc/fstab
fi
if ! grep -q "tmpfs /var/log" /etc/fstab 2>/dev/null; then
    echo "tmpfs /var/log tmpfs defaults,noatime,nosuid,size=50m 0 0" >> /etc/fstab
fi

# --- 6. System hardening ---
echo "[6/6] System hardening..."

# Disable swap to reduce SD card wear
dphys-swapfile swapoff 2>/dev/null || true
dphys-swapfile uninstall 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true

# Volatile journal (RAM only)
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf <<EOF
[Journal]
Storage=volatile
RuntimeMaxUse=30M
EOF

echo ""
echo "=== Pi1 setup complete ==="
echo "Services installed: camera-server"
echo "WiFi AP: $AP_SSID (channel $AP_CHANNEL)"
echo "Reboot to activate: sudo reboot"
