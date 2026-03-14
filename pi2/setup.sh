#!/usr/bin/env bash
###############################################################################
# Pi 2 (Display Pi) — Full Setup Script
# Timelapse Art Installation
#
# Run as root:  sudo bash setup.sh
# This script is idempotent — safe to run multiple times.
#
# IMPORTANT: Pi 1 must be fully configured and running before running this.
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  cifs-utils samba-client rsync ffmpeg mpv \
  python3-flask python3-pip \
  chrony jq

###############################################################################
# 2. Configure WiFi client
###############################################################################
info "Configuring WiFi client…"

# Read credentials — use defaults or prompt
WIFI_SSID="${WIFI_SSID:-timelapse-ap}"
WIFI_PASS="${WIFI_PASS:-changeme2}"

# For dhcpcd-based systems
if [[ -f /etc/dhcpcd.conf ]]; then
    if ! grep -q "interface wlan0" /etc/dhcpcd.conf 2>/dev/null; then
        cat >> /etc/dhcpcd.conf <<EOF

# Timelapse — static IP on Pi 1's AP
interface wlan0
    static ip_address=192.168.50.20/24
    static routers=192.168.50.1
    static domain_name_servers=192.168.50.1
EOF
    fi
fi

# WPA supplicant config
cat > /etc/wpa_supplicant/wpa_supplicant.conf <<EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
    ssid="${WIFI_SSID}"
    psk="${WIFI_PASS}"
    key_mgmt=WPA-PSK
    priority=1
}
EOF

# For NetworkManager-based Bookworm
if command -v nmcli &>/dev/null; then
    nmcli con delete timelapse-client 2>/dev/null || true
    nmcli con add con-name timelapse-client \
        type wifi ifname wlan0 ssid "${WIFI_SSID}" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${WIFI_PASS}" \
        ipv4.method manual ipv4.addresses 192.168.50.20/24 \
        ipv4.gateway 192.168.50.1 ipv4.dns 192.168.50.1 \
        connection.autoconnect yes connection.autoconnect-retries 0 2>/dev/null || true
fi

# Create connection retry service with exponential backoff
cat > /etc/systemd/system/wifi-retry.service <<'EOF'
[Unit]
Description=WiFi connection retry with exponential backoff
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/opt/wifi_retry.sh
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF

cat > /opt/wifi_retry.sh <<'SCRIPT'
#!/usr/bin/env bash
# Retry WiFi connection with exponential backoff
DELAY=2
MAX_DELAY=120
while true; do
    if ip addr show wlan0 | grep -q "192.168.50.20"; then
        logger -t wifi-retry "Connected to AP"
        exit 0
    fi
    logger -t wifi-retry "AP not available — retrying in ${DELAY}s"
    sleep "$DELAY"
    DELAY=$(( DELAY * 2 ))
    [[ $DELAY -gt $MAX_DELAY ]] && DELAY=$MAX_DELAY
    # Trigger re-connection attempt
    wpa_cli -i wlan0 reconnect 2>/dev/null || true
    nmcli con up timelapse-client 2>/dev/null || true
done
SCRIPT
chmod +x /opt/wifi_retry.sh
systemctl enable wifi-retry.service

###############################################################################
# 3. Configure chrony NTP client
###############################################################################
info "Configuring chrony NTP client…"
cat > /etc/chrony/chrony.conf <<'EOF'
# Sync time exclusively from Pi 1
server 192.168.50.1 iburst

# Record drift
driftfile /var/lib/chrony/chrony.drift

# Step clock at startup
makestep 1.0 3

# RTC sync
rtcsync

# Log
logdir /var/log/chrony
EOF
systemctl enable chrony

###############################################################################
# 4. Create local data directories
###############################################################################
info "Creating local data directories…"
mkdir -p /data/cache /data/renders /mnt/timelapse

###############################################################################
# 5. Mount Pi 1's Samba share
###############################################################################
info "Configuring Samba mount…"

# Append fstab entry
if ! grep -q "192.168.50.1/timelapse" /etc/fstab; then
    cat >> /etc/fstab <<'EOF'
//192.168.50.1/timelapse  /mnt/timelapse  cifs  guest,_netdev,nofail,x-systemd.automount,uid=1000,gid=1000,iocharset=utf8,vers=3.0  0  0
EOF
fi

# Create credentials-free mount  
mount -a 2>/dev/null || warn "Could not mount Samba share — Pi 1 may not be running yet."

###############################################################################
# 6. Install Python services
###############################################################################
info "Installing Python services…"

cp "$SCRIPT_DIR/services/sync.py"       /opt/sync.py
cp "$SCRIPT_DIR/services/playback.py"   /opt/playback.py
cp "$SCRIPT_DIR/services/status_api.py" /opt/status_api.py
chmod +x /opt/sync.py /opt/playback.py /opt/status_api.py

###############################################################################
# 7. Install systemd units
###############################################################################
info "Installing systemd service units…"

cp "$SCRIPT_DIR/services/sync.service"       /etc/systemd/system/
cp "$SCRIPT_DIR/services/playback.service"   /etc/systemd/system/
cp "$SCRIPT_DIR/services/status_api.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable sync.service playback.service status_api.service

###############################################################################
# 8. Cron jobs
###############################################################################
info "Installing cron jobs…"

cp "$SCRIPT_DIR/services/render.sh" /opt/render.sh
chmod +x /opt/render.sh

CRON_TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "# timelapse-" > "$CRON_TMP" || true
cat >> "$CRON_TMP" <<'EOF'
0 6,18 * * * /opt/render.sh  # timelapse-render
EOF
crontab "$CRON_TMP"
rm -f "$CRON_TMP"

###############################################################################
# 9. Stability hardening
###############################################################################
info "Applying stability hardening…"

# Hardware watchdog
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=30
ShutdownWatchdogSec=60
EOF

# Volatile journal
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=50M
EOF

# tmpfs for /var/log and /tmp to reduce SD writes
if ! grep -q "tmpfs.*/var/log" /etc/fstab; then
    cat >> /etc/fstab <<'EOF'
tmpfs  /var/log  tmpfs  defaults,noatime,nosuid,nodev,size=50M  0  0
tmpfs  /tmp      tmpfs  defaults,noatime,nosuid,nodev,size=100M 0  0
EOF
fi

systemctl restart systemd-journald 2>/dev/null || true

###############################################################################
# 10. Final
###############################################################################
info "Starting services…"
systemctl restart chrony 2>/dev/null || true
systemctl start sync.service playback.service status_api.service 2>/dev/null || true

info "═══════════════════════════════════════════════════════"
info " Pi 2 (Display Pi) setup complete!"
info " IP:         192.168.50.20"
info " Status API: http://192.168.50.20:5000/status"
info " Samba mount: /mnt/timelapse"
info "═══════════════════════════════════════════════════════"
