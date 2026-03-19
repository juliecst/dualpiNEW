#!/usr/bin/env bash
# Pi1 — Display + Brain + AP Master Setup (Idempotent)
# Run as root on a fresh Raspberry Pi OS Bookworm 64-bit install.
# This script:
#   1. Installs all dependencies
#   2. Detects and optionally formats USB sticks for /data and /backup
#   3. Configures WiFi AP (hostapd + dnsmasq)
#   4. Deploys and enables all systemd services
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/timelapse"
CONFIG_DIR="/data"

echo "=== Pi1 Display + Brain + AP Master Setup ==="

# --- 1. System packages ---
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    hostapd \
    dnsmasq \
    mpv \
    rsync \
    python3 \
    python3-yaml \
    python3-flask \
    python3-pip \
    usbutils

# --- 2. Deploy application files ---
echo "[2/7] Deploying application files..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
cp "$SCRIPT_DIR/grabber.py" "$INSTALL_DIR/grabber.py"
cp "$SCRIPT_DIR/backup.sh" "$INSTALL_DIR/backup.sh"
cp "$SCRIPT_DIR/portal/portal.py" "$INSTALL_DIR/portal.py"
cp "$REPO_DIR/common/disk_monitor.sh" "$INSTALL_DIR/disk_monitor.sh"
chmod +x "$INSTALL_DIR/grabber.py" "$INSTALL_DIR/backup.sh" \
         "$INSTALL_DIR/portal.py" "$INSTALL_DIR/disk_monitor.sh"

# Deploy default config if none exists
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$REPO_DIR/config/config.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Deployed default config.yaml"
fi

# --- 3. USB storage setup ---
echo "[3/7] Setting up USB storage..."
mkdir -p /data /backup

# Check for USB devices and provide guidance
USB_DEVS=$(lsblk -dpno NAME,TRAN 2>/dev/null | grep usb | awk '{print $1}' || true)
if [ -n "$USB_DEVS" ]; then
    echo "  Found USB devices: $USB_DEVS"
    echo "  Mount them to /data and /backup as needed."
    echo "  Example fstab entries:"
    echo "    UUID=<data-usb-uuid>   /data   exfat defaults,nofail,uid=1000,gid=1000 0 0"
    echo "    UUID=<backup-usb-uuid> /backup exfat defaults,nofail,uid=1000,gid=1000 0 0"
else
    echo "  No USB devices found. /data will use SD card."
fi

# Create timelapse directories
mkdir -p /data/timelapse/current

# --- 4. Configure WiFi Access Point ---
echo "[4/7] Configuring WiFi AP (hostapd + dnsmasq)..."

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

# Set static IP for wlan0 via dhcpcd
DHCPCD_CONF="/etc/dhcpcd.conf"
if ! grep -q "interface wlan0" "$DHCPCD_CONF" 2>/dev/null; then
    cat >> "$DHCPCD_CONF" <<EOF

# Pi1 AP static IP
interface wlan0
static ip_address=192.168.50.1/24
nohook wpa_supplicant
EOF
    echo "  Static IP 192.168.50.1 configured"
fi

# Unmask and enable hostapd
systemctl unmask hostapd 2>/dev/null || true
systemctl enable hostapd
systemctl enable dnsmasq

# --- 5. Install systemd services ---
echo "[5/7] Installing systemd services..."
cp "$SCRIPT_DIR/grabber.service" /etc/systemd/system/
cp "$SCRIPT_DIR/playback.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.timer" /etc/systemd/system/
cp "$SCRIPT_DIR/portal/portal.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable grabber.service
systemctl enable playback.service
systemctl enable backup.timer
systemctl enable portal.service

# --- 6. Disk monitor cron ---
echo "[6/7] Installing disk monitor cron..."
CRON_LINE="0 */6 * * * /opt/timelapse/disk_monitor.sh"
(crontab -l 2>/dev/null | grep -v "disk_monitor.sh"; echo "$CRON_LINE") | crontab -

# --- 7. System hardening ---
echo "[7/7] System hardening..."

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
echo "Services installed: grabber, playback, backup (timer), portal"
echo "WiFi AP: $AP_SSID (channel $AP_CHANNEL)"
echo "Reboot to activate: sudo reboot"
