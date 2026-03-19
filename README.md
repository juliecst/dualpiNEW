# Timelapse HQ — Dual Raspberry Pi Timelapse System

A modern, reliable, air-gapped timelapse capture and display system using two Raspberry Pi units communicating over a private WiFi AP.

## Architecture Overview

```
┌─────────────────────────────┐     WiFi AP (WPA2)      ┌──────────────────────────────┐
│  Pi1 — Display + Brain      │◄───────────────────────►│  Pi2 — Camera (headless)     │
│  192.168.50.1               │                          │  192.168.50.20               │
│                             │  HTTP GET /latest.jpg ──►│                              │
│  • Polls Pi2 every 5s       │                          │  • rpicam-still loop         │
│  • Saves timestamped JPEGs  │                          │  • saves to /tmp/latest.jpg  │
│  • mpv slideshow on display │                          │  • serves via HTTP :8080     │
│  • Daily rsync backup       │                          │  • tmpfs only, no USB        │
│  • Flask admin portal       │                          │                              │
└─────────────────────────────┘                          └──────────────────────────────┘
```

**Zero Samba. Zero RTSP. Zero fstab mounts. Zero file transfer pain.**

## Key Design Principles

- **No Samba/CIFS** — eliminated entirely
- **No RTSP/video stream** — HQ camera shoots stills directly via `rpicam-still`, preserving full sensor quality
- **No fstab network mounts** — Pi1 pulls over plain HTTP
- **Air-gapped** — private AP only, no internet required
- **Self-healing** — all services use `Restart=always`, boot-order tolerant
- **Atomic writes** — temp file + rename for all JPEG saves to survive power cuts
- **Session-based** — new calendar day = new subfolder, no manual intervention needed
- **Raspberry Pi OS Bookworm 64-bit**, `rpicam-still` (not legacy `raspistill`)

## Repository Structure

```
timelapse-hq-fork/
├── README.md
├── config/
│   └── config.yaml              # Shared defaults: capture interval, resolution, quality, backup schedule
├── pi1/
│   ├── setup.sh                 # Idempotent: install deps, format USBs, configure AP, enable services
│   ├── update.sh                # git pull + service restart
│   ├── grabber.py               # Main loop: HTTP poll → timestamped JPEG save
│   ├── grabber.service
│   ├── playback.service         # mpv fullscreen slideshow from /data/timelapse/current/*/*.jpg
│   ├── backup.sh                # rsync /data/timelapse/ → /backup/YYYY-MM-DD/
│   ├── backup.timer + backup.service
│   ├── hostapd.conf
│   ├── dnsmasq.conf
│   └── portal/                  # Optional Flask admin portal
│       ├── portal.py
│       └── portal.service
├── pi2/
│   ├── setup.sh                 # Install rpicam-apps, configure WiFi client, enable services
│   ├── update.sh
│   ├── camera_server.py         # rpicam-still loop + Python HTTP server serving /tmp/latest.jpg
│   ├── camera-server.service
│   └── wifi-retry.service
├── common/
│   └── disk_monitor.sh          # Flag file if disk > 85%
└── .gitignore
```

## Role Assignment

### Pi1 — Display + Brain + AP Master

| Function | Details |
|----------|---------|
| WiFi AP | `timelapse-ap`, subnet `192.168.50.0/24`, WPA2, password configurable |
| Image Grabber | Every N seconds (default: 5): fetches `http://192.168.50.20:8080/latest.jpg`, saves as `/data/timelapse/current/YYYY-MM-DD/HH-MM-SS.jpg` |
| Playback | `mpv` fullscreen slideshow on Waveshare round display |
| Backup | Daily `rsync` from `/data/timelapse/` to second USB stick at `/backup/` |
| Admin Portal | Optional Flask dashboard on port 80 |
| Storage | 1–2 USB sticks: `/data/` (working) and `/backup/` (backup) |
| Config | All config lives in `/data/config.yaml` |

### Pi2 — Camera (Headless Client)

| Function | Details |
|----------|---------|
| Camera | HQ Camera (Raspberry Pi HQ Camera or Module 3) |
| WiFi | Connects as client to Pi1's AP |
| Capture | `rpicam-still` loop → `/tmp/latest.jpg` (tmpfs, RAM only) |
| HTTP Server | Serves `/tmp/latest.jpg` on port 8080 |
| Services | Only two: `camera-server.service` + `wifi-retry.service` |

## Quick Start

### Prerequisites

- 2× Raspberry Pi 4 (or 5) with Raspberry Pi OS Bookworm 64-bit
- 1× Raspberry Pi HQ Camera (or Camera Module 3) attached to Pi2
- 1–2× USB sticks (exFAT formatted) for Pi1 storage
- 1× Display (HDMI or Waveshare round) connected to Pi1

### Pi2 Setup (Camera Node)

```bash
# SSH into Pi2
git clone <this-repo> /opt/timelapse-repo
cd /opt/timelapse-repo
sudo bash pi2/setup.sh
sudo reboot
```

### Pi1 Setup (Display + Brain)

```bash
# SSH into Pi1
git clone <this-repo> /opt/timelapse-repo
cd /opt/timelapse-repo
sudo bash pi1/setup.sh
sudo reboot
```

### Boot Sequence

1. **Pi1 boots first** → starts WiFi AP, DHCP, grabber (waits for Pi2)
2. **Pi2 boots** → connects to AP, starts camera capture + HTTP server
3. **Pi1 grabber** → detects Pi2, begins saving timestamped JPEGs
4. **Pi1 playback** → starts mpv slideshow once frames are available

## Configuration

All settings are in `config/config.yaml` (deployed to `/data/config.yaml` on Pi1):

```yaml
capture:
  interval_seconds: 5
  width: 4056
  height: 3040
  quality: 95

grabber:
  poll_interval_seconds: 5
  pi2_url: "http://192.168.50.20:8080/latest.jpg"

playback:
  fps: 25

network:
  ap_ssid: "timelapse-ap"
  ap_password: "changeme2"
```

Configuration can also be changed at runtime via the Flask admin portal at `http://192.168.50.1/`.

## API Endpoints

### Pi2 Camera Server (port 8080)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/latest.jpg` | GET | Serve the latest captured JPEG |
| `/health` | GET | JSON health check with last capture timestamp |

### Pi1 Admin Portal (port 80)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard with system status |
| `/config` | POST | Update configuration |
| `/backup` | POST | Trigger manual backup |
| `/api/status` | GET | JSON system status |
| `/api/health` | GET | JSON health check |

## Updating

On either Pi, run the update script to pull latest code and restart services:

```bash
sudo bash pi1/update.sh   # On Pi1
sudo bash pi2/update.sh   # On Pi2
```

## Self-Healing Features

- **systemd Restart=always** — all services auto-restart on failure with 10s backoff
- **WiFi retry service** — Pi2 continuously retries connection to Pi1's AP
- **Atomic writes** — temp file + rename prevents corrupt files on power loss
- **Boot-order tolerant** — Pi1 grabber gracefully handles Pi2 being offline
- **Disk monitoring** — cron job flags disk usage > 85% with hysteresis

## License

See repository license file for details.
