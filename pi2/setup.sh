#!/usr/bin/env bash
# Pi2 — Display + Brain Setup (Idempotent)
# Run as root on a fresh Raspberry Pi OS Bookworm 64-bit install.
# This script:
#   1. Installs all dependencies (mpv, rsync, flask, etc.)
#   2. Detects and optionally formats USB sticks for /data and /backup
#   3. Configures WiFi client to connect to Pi1's AP
#   4. Deploys and enables all systemd services
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/timelapse"
CONFIG_DIR="/data"

echo "=== Pi2 Display + Brain Setup ==="

# --- 1. System packages ---
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    mpv \
    rsync \
    dhcpcd5 \
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

# --- 4. Configure WiFi client ---
echo "[4/7] Configuring WiFi client..."

# Ensure WiFi radio is unblocked
rfkill unblock wifi 2>/dev/null || true

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

# On Bookworm, NetworkManager is the default. Use nmcli for WiFi client config.
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    # Disable dhcpcd service to prevent conflicts with NetworkManager
    systemctl stop dhcpcd 2>/dev/null || true
    systemctl disable dhcpcd 2>/dev/null || true
    echo "  dhcpcd disabled (NetworkManager manages WiFi)"

    nmcli connection delete "$WIFI_SSID" 2>/dev/null || true
    nmcli connection add type wifi con-name "$WIFI_SSID" \
        wifi.ssid "$WIFI_SSID" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$WIFI_PASS" \
        ipv4.method manual \
        ipv4.addresses "192.168.50.20/24" \
        ipv4.gateway "192.168.50.1" \
        ipv4.dns "192.168.50.1" \
        connection.autoconnect yes \
        connection.autoconnect-priority 100
    echo "  WiFi configured via NetworkManager for SSID: $WIFI_SSID"
else
    # Fall back to wpa_supplicant + dhcpcd (non-Bookworm or Lite without NM)
    # Enable dhcpcd service for static IP management
    systemctl enable dhcpcd 2>/dev/null || true

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
    echo "  WiFi configured via wpa_supplicant for SSID: $WIFI_SSID"

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
        echo "  Static IP 192.168.50.20 configured via dhcpcd"
    fi
fi

# --- 5. Install systemd services ---
echo "[5/7] Installing systemd services..."
cp "$SCRIPT_DIR/grabber.service" /etc/systemd/system/
cp "$SCRIPT_DIR/playback.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/backup.timer" /etc/systemd/system/
cp "$SCRIPT_DIR/portal/portal.service" /etc/systemd/system/
cp "$SCRIPT_DIR/wifi-retry.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable grabber.service
systemctl enable playback.service
systemctl enable backup.timer
systemctl enable portal.service
systemctl enable wifi-retry.service

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
echo "=== Pi2 setup complete ==="
echo "Services installed: grabber, playback, backup (timer), portal, wifi-retry"
echo "Reboot to activate: sudo reboot"
