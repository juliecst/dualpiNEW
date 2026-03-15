# Timelapse Art Installation — Dual Raspberry Pi System

A production-ready, self-healing timelapse capture and display system designed for a 3-month unattended art installation. Two Raspberry Pi 4 units communicate over a private WiFi network with no internet connection.

| Component | Role |
|-----------|------|
| **Pi 1** (Camera Pi) | Captures photos, stores on USB, serves admin portal, runs Samba & NTP |
| **Pi 2** (Display Pi) | Syncs images, plays timelapse on Waveshare round display |

---

## Architecture Overview

```
┌─────────────────────────────┐         WiFi AP (WPA2)        ┌──────────────────────────────┐
│  Pi 1 — Camera Pi           │◄─────────────────────────────►│  Pi 2 — Display Pi           │
│  192.168.50.1               │   SSID: timelapse-ap          │  192.168.50.20               │
│                             │                                │                              │
│  • rpicam capture           │   Samba share ───────────────►│  • Image sync (rsync)        │
│  • Flask admin portal :80   │                                │  • mpv playback              │
│  • Samba shares             │   NTP time sync ─────────────►│  • Flask status API :5000    │
│  • Chrony NTP server        │                                │  • ffmpeg rendering          │
│  • USB Working + Backup     │   HTTP status polling ◄───────│                              │
└─────────────────────────────┘                                └──────────────────────────────┘
```

---

## First-Time Setup

### Prerequisites
- 2× Raspberry Pi 4 with Raspberry Pi OS Lite 64-bit (Bookworm)
- 1× Pi HQ Camera connected to Pi 1
- 2× USB sticks (Working + Backup) plugged into Pi 1
- 1× Waveshare round display connected to Pi 2
- No internet connection required

### Step 1: Flash Both SD Cards
Flash **Raspberry Pi OS Lite 64-bit (Bookworm)** to both SD cards. Enable SSH:
```bash
# On each SD card's boot partition:
touch /Volumes/bootfs/ssh
```

### Step 2: Set Up Pi 1 (Camera Pi) FIRST

> **Important:** Pi 1 must be fully configured and running before touching Pi 2, because Pi 2 connects to Pi 1's WiFi AP.

1. Connect Pi 1 to a monitor/keyboard (or temporary Ethernet for SSH)
2. Copy the `pi1/` folder to Pi 1:
   ```bash
   scp -r pi1/ pi@<pi1-ip>:~/pi1/
   ```
3. SSH in and run setup:
   ```bash
   ssh pi@<pi1-ip>
   sudo bash ~/pi1/setup.sh
   ```
4. The script will:
   - Install all dependencies
   - Detect and format both USB sticks as exFAT (with confirmation prompt)
   - Write UUIDs to `/etc/fstab`
   - Configure the WiFi Access Point
   - Set up chrony NTP, Samba, capture service, admin portal
   - Enable all systemd services and cron jobs
5. After setup, Pi 1 reboots and creates the `timelapse-ap` WiFi network

### Step 3: Set Up Pi 2 (Display Pi)

1. Connect Pi 2 to a monitor/keyboard (or temporary Ethernet for SSH)
2. Copy the `pi2/` folder to Pi 2:
   ```bash
   scp -r pi2/ pi@<pi2-ip>:~/pi2/
   ```
3. Optionally set WiFi credentials if changed from defaults:
   ```bash
   export WIFI_SSID="timelapse-ap"
   export WIFI_PASS="changeme2"
   ```
4. Run setup:
   ```bash
   ssh pi@<pi2-ip>
   sudo bash ~/pi2/setup.sh
   ```
5. Pi 2 will connect to Pi 1's AP, mount the Samba share, and start playback

### Daily power-off / power-on behavior

The installation is safe to switch off at night and power back on in the morning:

- **Both Pis autostart automatically.** All capture, portal, sync, playback, WiFi retry, Samba, and NTP services are enabled through systemd during setup.
- **Boot order does not need to be perfect.** Pi 2 keeps retrying the WiFi link and re-attempts the Samba mount until Pi 1 has finished bringing up its access point and file share.
- **The handshake is session-based.** Pi 2 watches `session.id`; when it sees a new session it wipes its local cache and syncs the fresh frames, and when the session is unchanged it simply resumes syncing where it left off.
- **Sudden power cuts are survivable.** The services use atomic temp-file renames for config, capture, sync, and backup markers, so incomplete writes are discarded on the next boot. Capture resumes from the last fully written frame number in the current session.

### Re-enable autostart manually

If you ran `setup.sh`, autostart is already enabled on both Pis. If you ever disable services for maintenance and want to restore the normal boot behavior, run these commands:

#### Pi 1 autostart

```bash
ssh pi@192.168.50.1
sudo systemctl enable hostapd dnsmasq chrony smbd nmbd capture.service portal.service
sudo systemctl restart hostapd dnsmasq chrony smbd nmbd
sudo systemctl start capture.service portal.service
```

This makes Pi 1 bring up the AP, NTP server, Samba share, capture loop, and admin portal automatically at boot.

#### Pi 2 autostart

```bash
ssh pi@192.168.50.20
sudo systemctl enable wifi-retry.service chrony sync.service playback.service status_api.service
sudo systemctl start wifi-retry.service
sudo systemctl restart chrony
sudo systemctl start sync.service playback.service status_api.service
```

This makes Pi 2 reconnect to Pi 1 automatically, resync images, relaunch playback, and restore the status API on every boot.

---

## SSH Access Via the AP Network

Once both Pis are running, connect your laptop to the `timelapse-ap` WiFi network:

```bash
# Pi 1 (Camera Pi)
ssh pi@192.168.50.1

# Pi 2 (Display Pi)
ssh pi@192.168.50.20
```

Default WiFi password: `changeme2` (change via admin portal or config.json)

---

## Admin Portal

Open **http://192.168.50.1** in any browser while connected to the `timelapse-ap` WiFi.

Default admin password: `changeme`

The dashboard provides:
- **Capture settings:** numeric interval, exposure mode, and luma correction
- **Session management:** preview before archiving, archived-session labels, and start new session
- **Playback settings:** FPS selector, brightness test, restart playback, resync now, duration calculator, and an optional FFmpeg video-backup toggle
- **Admin & network settings:** update the portal password, see the current Pi 1 access IPs, change the AP SSID/password, and toggle Pi 1 between AP mode and an upstream WiFi client
- **Setup & maintenance:** capture a live camera preview, verify rpicam availability, and format/mount the two USB sticks later from the dashboard if setup started without them
- **System status:** uptime, CPU temp, disk usage, and WiFi health for both Pis
- **Backup monitoring:** live backup/disk health checks, frame growth chart, and storage estimates

---

## Changing Passwords

### Admin Password
Via the admin portal, or manually:
```bash
# On Pi 1:
sudo python3 -c "
import json, os
cfg = json.load(open('/data/config.json'))
cfg['admin_password'] = 'your-new-password'
tmp = '/data/config.json.tmp'
json.dump(cfg, open(tmp, 'w'), indent=2)
os.rename(tmp, '/data/config.json')
"
```

### WiFi Password
> **Warning:** Changing the WiFi password requires updating both Pis.

1. Update via the **Admin & Network** section in the admin portal, or edit `/data/config.json` on Pi 1
2. Re-run Pi 1's setup to apply to hostapd:
   ```bash
   sudo bash ~/pi1/setup.sh
   ```
3. On Pi 2, update `/etc/wpa_supplicant/wpa_supplicant.conf` with the new password:
   ```bash
   sudo nano /etc/wpa_supplicant/wpa_supplicant.conf
   # Change psk="new-password"
   sudo systemctl restart wpa_supplicant
   ```
   Or if using NetworkManager:
   ```bash
   sudo nmcli con modify timelapse-client wifi-sec.psk "new-password"
   sudo nmcli con up timelapse-client
   ```

---

## Manual Operations

### Trigger a Backup Manually
```bash
ssh pi@192.168.50.1
sudo /opt/backup.sh
```

### Check Backup Status
```bash
cat /data/last_backup.txt
ls -la /data/backup_warning.flag 2>/dev/null && echo "WARNING: backup issue" || echo "Backup OK"
```

### Restart Capture Service
```bash
ssh pi@192.168.50.1
sudo systemctl restart capture.service
```

### Restart Playback Service
```bash
ssh pi@192.168.50.20
sudo systemctl restart playback.service
```

### View Service Logs
```bash
# Pi 1
journalctl -u capture.service -f
journalctl -u portal.service -f

# Pi 2
journalctl -u sync.service -f
journalctl -u playback.service -f
journalctl -u status_api.service -f
```

---

## USB Stick Recovery

### If a USB Stick Fails

1. **Identify the failed stick:** Check if `/data` or `/backup` is mounted:
   ```bash
   df -h /data /backup
   ```
2. **Replace the stick:** Plug in a new USB stick
3. **Find its UUID:**
   ```bash
   sudo blkid
   # Look for the new device, e.g., /dev/sda1
   ```
4. **Format as exFAT:**
   ```bash
   sudo mkfs.exfat -n TIMELAPSE /dev/sdX1
   ```
5. **Get the new UUID:**
   ```bash
   sudo blkid /dev/sdX1
   ```
6. **Update fstab:**
   ```bash
   sudo nano /etc/fstab
   # Replace the old UUID with the new one on the /data or /backup line
   ```
7. **Mount and recreate directories:**
   ```bash
   sudo mount -a
   sudo mkdir -p /data/timelapse/current /data/timelapse/archive /data/renders
   # If this was the working stick, restore from backup:
   sudo rsync -av /backup/timelapse/ /data/timelapse/
   ```

### Finding USB Stick UUIDs
```bash
# List all block devices with UUIDs
sudo blkid

# More detailed view
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINT
```

---

## Accessing Archived Sessions

### Via Samba from a Laptop

1. Connect your laptop to the `timelapse-ap` WiFi
2. Access the Samba share:
   - **macOS Finder:** ⌘K → `smb://192.168.50.1/timelapse`
   - **Windows Explorer:** `\\192.168.50.1\timelapse`
   - **Linux:** `smb://192.168.50.1/timelapse`
3. Browse `archive/` for past sessions, `current/` for the active session

### Via SCP
```bash
# Copy a specific archived session
scp -r pi@192.168.50.1:/data/timelapse/archive/20240301_090000/ ./local_copy/

# Copy all archives
scp -r pi@192.168.50.1:/data/timelapse/archive/ ./all_archives/

# Copy rendered videos
scp pi@192.168.50.1:/data/renders/*.mp4 ./renders/
```

---

## Configuration Reference

All settings live in `/data/config.json` on Pi 1. The admin portal reads and writes this file. Pi 2 polls it every 30 seconds.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `capture_interval_minutes` | int | `5` | Minutes between captures (any whole number from 1 to 1440) |
| `exposure_mode` | string | `"auto"` | `"auto"` or `"manual"` |
| `exposure_shutter_speed` | int | `10000` | Shutter speed in µs (manual mode) |
| `exposure_iso` | int | `100` | ISO sensitivity (manual mode) |
| `luma_target` | int\|null | `null` | Target avg luminance 0–255, or null to disable |
| `playback_fps` | int | `25` | Playback frame rate |
| `display_brightness` | int | `100` | Display brightness 0–100% |
| `ffmpeg_video_backup_enabled` | bool | `true` | Enables Pi 2's optional twice-daily FFmpeg archival MP4 render job |
| `admin_password` | string | `"changeme"` | Admin portal password |
| `wifi_ssid` | string | `"timelapse-ap"` | WiFi AP SSID |
| `wifi_password` | string | `"changeme2"` | WiFi AP WPA2 password |
| `uplink_wifi_ssid` | string | `""` | Optional upstream WiFi SSID used when toggling Pi 1 out of AP mode for updates |
| `uplink_wifi_password` | string | `""` | Optional upstream WiFi password used with `uplink_wifi_ssid` |
| `display_type` | string | `"hdmi"` | `"hdmi"` or `"spi"` for Waveshare display |

---

## Storage Layout

### Pi 1
```
/data/            ← USB Working stick (exFAT)
  timelapse/
    current/
      session.id
      frame_000001.jpg
      frame_000002.jpg
      ...
    archive/
      20240301_090000/
        session.id
        frame_000001.jpg
        ...
  renders/
  config.json
  last_capture.txt
  last_backup.txt
  backup_warning.flag  (only if backup failed)
  disk_warning.flag    (only if disk > 85%)

/backup/          ← USB Backup stick (exFAT)
  timelapse/
    current/
    archive/
  last_backup.txt
```

### Pi 2
```
/mnt/timelapse/   ← Samba mount from Pi 1 (read-only)
/data/
  cache/          ← Local copy of current session frames
    session.id
    frame_000001.jpg
    ...
  renders/        ← ffmpeg output
  config_local.json  ← Cached copy of config
  last_sync.txt
```

---

## Services Reference

### Pi 1 Services
| Service | Type | Description |
|---------|------|-------------|
| `capture.service` | systemd | Photo capture loop |
| `portal.service` | systemd | Flask admin UI on port 80 |
| `hostapd` | systemd | WiFi access point |
| `dnsmasq` | systemd | DHCP + DNS for AP |
| `chrony` | systemd | NTP server |
| `smbd` / `nmbd` | systemd | Samba file shares |
| backup cron | cron 03:00 | Daily rsync to backup stick |
| disk monitor cron | cron 6h | Disk usage flag management |

### Pi 2 Services
| Service | Type | Description |
|---------|------|-------------|
| `sync.service` | systemd | Image sync from Pi 1 (60s interval) |
| `playback.service` | systemd | mpv playback on display |
| `status_api.service` | systemd | Flask status API on port 5000 |
| `wifi-retry.service` | systemd | Exponential backoff WiFi reconnect |
| render cron | cron 06:00/18:00 | Archival .mp4 render |

---

## Troubleshooting

### Pi 2 Can't Connect to WiFi
```bash
# Check if AP is broadcasting
sudo iwlist wlan0 scan | grep timelapse

# Force reconnect
sudo wpa_cli -i wlan0 reconnect

# Check logs
journalctl -u wifi-retry.service -f
```

### No Frames Being Captured
```bash
# Check if camera is detected
rpicam-hello --list-cameras

# Check capture service
sudo systemctl status capture.service
journalctl -u capture.service --no-pager -n 50
```

### Display Not Showing Anything
```bash
# Check playback service
sudo systemctl status playback.service
journalctl -u playback.service --no-pager -n 50

# Check if frames exist locally
ls /data/cache/frame_*.jpg | wc -l

# Check if mpv is running
pgrep -fa mpv
```

### Samba Mount Not Working on Pi 2
```bash
# Test connectivity
ping -c 3 192.168.50.1

# Try manual mount
sudo mount -t cifs //192.168.50.1/timelapse /mnt/timelapse -o guest,vers=3.0

# Check mount
df -h /mnt/timelapse
ls /mnt/timelapse/current/
```

---

## File Manifest

```
pi1/
  setup.sh              ← Full Pi 1 setup (run as root)
  hostapd.conf          ← WiFi AP config
  dnsmasq.conf          ← DHCP/DNS config
  smb.conf              ← Samba shares config
  chrony.conf           ← NTP server config
  fstab_entries.txt     ← Reference fstab lines for USB sticks
  services/
    capture.py          ← Photo capture service
    capture.service     ← systemd unit
    portal.py           ← Flask admin dashboard (templates inline)
    portal.service      ← systemd unit
    backup.sh           ← Daily backup cron script
    disk_monitor.sh     ← Disk usage monitor cron script

pi2/
  setup.sh              ← Full Pi 2 setup (run as root)
  fstab_entries.txt     ← Reference fstab lines
  services/
    sync.py             ← Image sync service
    sync.service        ← systemd unit
    playback.py         ← mpv playback controller
    playback.service    ← systemd unit
    render.sh           ← Archival render cron script
    status_api.py       ← Flask status API
    status_api.service  ← systemd unit
```
