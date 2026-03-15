#!/usr/bin/env bash
###############################################################################
# Pi 1 (Camera Pi) — Full Setup Script
# Timelapse Art Installation
#
# Run as root:  sudo bash setup.sh
# This script is idempotent — safe to run multiple times.
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || error "This script must be run as root."

###############################################################################
# 1. Install packages
###############################################################################
info "Updating apt and installing packages…"
apt-get update -qq
apt-get install -y \
  hostapd dnsmasq chrony samba samba-common-bin \
  python3-flask python3-pillow python3-pip \
  rpicam-apps rsync exfatprogs iptables \
  ffmpeg jq

###############################################################################
# 2. Format and mount USB sticks
###############################################################################
format_usb_sticks() {
    info "Detecting USB block devices…"

    # Find USB mass-storage block devices (exclude SD card / loop / ram)
    mapfile -t USB_DEVS < <(
        lsblk -dnpo NAME,TRAN | awk '$2=="usb"{print $1}' | sort
    )

    if [[ ${#USB_DEVS[@]} -lt 2 ]]; then
        warn "Need 2 USB sticks but found ${#USB_DEVS[@]}."
        warn "Plug in both USB sticks and re-run, or set up fstab manually."
        return 1
    fi

    # Sort by size descending — two largest are the target sticks
    mapfile -t SORTED < <(
        for d in "${USB_DEVS[@]}"; do
            sz=$(lsblk -bdnpo SIZE "$d" 2>/dev/null || echo 0)
            echo "$sz $d"
        done | sort -rn | head -2 | awk '{print $2}'
    )

    WORKING_DEV="${SORTED[0]}"
    BACKUP_DEV="${SORTED[1]}"

    info "Working stick: $WORKING_DEV"
    info "Backup  stick: $BACKUP_DEV"

    read -rp "Format BOTH sticks as exFAT? ALL DATA WILL BE LOST. [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || { warn "Skipping format."; return 1; }

    for dev in "$WORKING_DEV" "$BACKUP_DEV"; do
        info "Formatting $dev as exFAT…"
        wipefs -a "$dev"
        # Create a single partition
        echo -e "o\nn\np\n1\n\n\nt\n7\nw" | fdisk "$dev" || true
        sleep 1
        PART="${dev}1"
        [[ -b "$PART" ]] || PART="$dev"   # handle whole-disk devices
        mkfs.exfat -n TIMELAPSE "$PART"
    done

    # Get UUIDs
    sleep 1
    WORKING_PART="${WORKING_DEV}1"; [[ -b "$WORKING_PART" ]] || WORKING_PART="$WORKING_DEV"
    BACKUP_PART="${BACKUP_DEV}1";   [[ -b "$BACKUP_PART" ]]  || BACKUP_PART="$BACKUP_DEV"

    WORKING_UUID=$(blkid -s UUID -o value "$WORKING_PART")
    BACKUP_UUID=$(blkid -s UUID -o value "$BACKUP_PART")

    info "Working UUID: $WORKING_UUID"
    info "Backup  UUID: $BACKUP_UUID"

    # Remove old entries
    sed -i '\|/data |d; \|/backup |d' /etc/fstab

    # Append new entries
    cat >> /etc/fstab <<EOF
UUID=${WORKING_UUID}  /data    exfat  defaults,nofail,uid=1000,gid=1000,dmask=0022,fmask=0133  0  0
UUID=${BACKUP_UUID}   /backup  exfat  defaults,nofail,uid=1000,gid=1000,dmask=0022,fmask=0133  0  0
EOF

    mkdir -p /data /backup
    mount -a
    info "USB sticks mounted."
}

# Only format if /data is not already a USB mount
if ! mountpoint -q /data 2>/dev/null; then
    format_usb_sticks || warn "USB setup incomplete — configure /etc/fstab manually."
fi

###############################################################################
# 3. Create directory structure on USB sticks
###############################################################################
info "Creating directory structure on /data and /backup…"
mkdir -p /data/timelapse/current /data/timelapse/archive /data/renders
mkdir -p /backup/timelapse/current /backup/timelapse/archive

# Write default config.json if absent
if [[ ! -f /data/config.json ]]; then
    cat > /data/config.json <<'CONF'
{
  "capture_interval_minutes": 5,
  "exposure_mode": "auto",
  "exposure_shutter_speed": 10000,
  "exposure_iso": 100,
  "luma_target": null,
  "playback_fps": 25,
  "display_brightness": 100,
  "ffmpeg_video_backup_enabled": true,
  "admin_password": "changeme",
  "wifi_ssid": "timelapse-ap",
  "wifi_password": "changeme2",
  "uplink_wifi_ssid": "",
  "uplink_wifi_password": "",
  "display_type": "hdmi"
}
CONF
    info "Default config.json written."
fi

# Write initial session.id if absent
if [[ ! -f /data/timelapse/current/session.id ]]; then
    date +"%Y%m%d_%H%M%S" > /data/timelapse/current/session.id
    info "Initial session.id created."
fi

chown -R 1000:1000 /data /backup 2>/dev/null || true

###############################################################################
# 4. Configure WiFi Access Point
###############################################################################
info "Configuring WiFi Access Point…"

# Read SSID / password from config.json if available
if [[ -f /data/config.json ]]; then
    WIFI_SSID=$(jq -r '.wifi_ssid // "timelapse-ap"' /data/config.json)
    WIFI_PASS=$(jq -r '.wifi_password // "changeme2"' /data/config.json)
else
    WIFI_SSID="timelapse-ap"
    WIFI_PASS="changeme2"
fi

# Install hostapd config
cp "$SCRIPT_DIR/hostapd.conf" /etc/hostapd/hostapd.conf
sed -i "s/^ssid=.*/ssid=${WIFI_SSID}/" /etc/hostapd/hostapd.conf
sed -i "s/^wpa_passphrase=.*/wpa_passphrase=${WIFI_PASS}/" /etc/hostapd/hostapd.conf

# Point hostapd to config
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true

# Install dnsmasq config
cp "$SCRIPT_DIR/dnsmasq.conf" /etc/dnsmasq.conf

# Disable wpa_supplicant on wlan0 (we are AP, not client)
systemctl disable --now wpa_supplicant 2>/dev/null || true

# Static IP for wlan0 via dhcpcd
if ! grep -q "interface wlan0" /etc/dhcpcd.conf 2>/dev/null; then
    cat >> /etc/dhcpcd.conf <<'EOF'

# BEGIN TIMELAPSE AP
# Timelapse AP — static IP for wlan0
interface wlan0
    static ip_address=192.168.50.1/24
    nohook wpa_supplicant
# END TIMELAPSE AP
EOF
fi

# Alternatively for NetworkManager-based setups (Bookworm)
if command -v nmcli &>/dev/null; then
    nmcli con delete timelapse-ap 2>/dev/null || true
    # We still use hostapd, so just ensure NM ignores wlan0
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/10-ignore-wlan0.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
    systemctl restart NetworkManager 2>/dev/null || true
fi

# Enable IP forwarding (not strictly needed, but good practice)
sysctl -w net.ipv4.ip_forward=1
grep -q "^net.ipv4.ip_forward" /etc/sysctl.conf || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

# Redirect port 80 traffic (captive portal)
iptables -t nat -C PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 80 2>/dev/null || \
    iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 80

# Persist iptables rules
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4

systemctl unmask hostapd 2>/dev/null || true
systemctl enable hostapd dnsmasq

###############################################################################
# 5. Chrony NTP server
###############################################################################
info "Configuring chrony NTP server…"
cp "$SCRIPT_DIR/chrony.conf" /etc/chrony/chrony.conf
systemctl enable chrony

###############################################################################
# 6. Samba
###############################################################################
info "Configuring Samba…"
cp "$SCRIPT_DIR/smb.conf" /etc/samba/smb.conf
systemctl enable smbd nmbd

###############################################################################
# 7. Install Python services
###############################################################################
info "Installing Python services…"

cp "$SCRIPT_DIR/services/capture.py"  /opt/capture.py
cp "$SCRIPT_DIR/services/portal.py"   /opt/portal.py

# Copy templates directory
mkdir -p /opt/templates
if [[ -d "$SCRIPT_DIR/services/templates" ]]; then
    cp -r "$SCRIPT_DIR/services/templates/"* /opt/templates/ 2>/dev/null || true
fi

chmod +x /opt/capture.py /opt/portal.py

###############################################################################
# 8. Install systemd units
###############################################################################
info "Installing systemd service units…"

cp "$SCRIPT_DIR/services/capture.service" /etc/systemd/system/
cp "$SCRIPT_DIR/services/portal.service"  /etc/systemd/system/

systemctl daemon-reload
systemctl enable capture.service portal.service

###############################################################################
# 9. Cron jobs
###############################################################################
info "Installing cron jobs…"

cp "$SCRIPT_DIR/services/backup.sh"       /opt/backup.sh
cp "$SCRIPT_DIR/services/disk_monitor.sh" /opt/disk_monitor.sh
chmod +x /opt/backup.sh /opt/disk_monitor.sh

# Write crontab (idempotent — replace existing timelapse entries)
CRON_TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "# timelapse-" > "$CRON_TMP" || true
cat >> "$CRON_TMP" <<'EOF'
0 3 * * * /opt/backup.sh        # timelapse-backup
0 */6 * * * /opt/disk_monitor.sh # timelapse-diskmon
EOF
crontab "$CRON_TMP"
rm -f "$CRON_TMP"

###############################################################################
# 10. Stability hardening
###############################################################################
info "Applying stability hardening…"

# Hardware watchdog
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=30
ShutdownWatchdogSec=60
EOF

# Volatile journal (no SD card writes for logs)
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=50M
EOF

systemctl restart systemd-journald 2>/dev/null || true

###############################################################################
# 11. Final
###############################################################################
info "Starting services…"
systemctl restart dhcpcd 2>/dev/null || true
systemctl restart hostapd dnsmasq chrony smbd nmbd 2>/dev/null || true
systemctl start capture.service portal.service

info "═══════════════════════════════════════════════════════"
info " Pi 1 (Camera Pi) setup complete!"
info " AP SSID:   $WIFI_SSID"
info " AP IP:     192.168.50.1"
info " Admin UI:  http://192.168.50.1/"
info " Samba:     \\\\192.168.50.1\\timelapse"
info "═══════════════════════════════════════════════════════"
