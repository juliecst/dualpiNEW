# Timelapse HQ — Dual Raspberry Pi Timelapse System

A modern, reliable, air-gapped timelapse capture and display system using two Raspberry Pi units communicating over a private WiFi AP.

## Architecture Overview

```
┌─────────────────────────────┐     WiFi AP (WPA2)      ┌──────────────────────────────┐
│  Pi1 — Camera + AP Brain    │◄───────────────────────►│  Pi2 — Display (brain)       │
│  192.168.50.1               │                          │  192.168.50.20               │
│                             │  HTTP GET /latest.jpg ◄──│                              │
│  • rpicam-still loop        │                          │  • Polls Pi1 every 5s        │
│  • saves to /tmp/latest.jpg │                          │  • Saves timestamped JPEGs   │
│  • serves via HTTP :8080    │                          │  • mpv slideshow on display  │
│  • WiFi AP (hostapd)        │                          │  • Daily rsync backup        │
│  • tmpfs only, no USB       │                          │  • Flask admin portal        │
└─────────────────────────────┘                          └──────────────────────────────┘
```

**Zero Samba. Zero RTSP. Zero fstab mounts. Zero file transfer pain.**

## Key Design Principles

- **No Samba/CIFS** — eliminated entirely
- **No RTSP/video stream** — HQ camera shoots stills directly via `rpicam-still`, preserving full sensor quality
- **No fstab network mounts** — Pi2 pulls over plain HTTP
- **Air-gapped** — private AP only, no internet required
- **Self-healing** — all services use `Restart=always`, boot-order tolerant
- **Atomic writes** — temp file + rename for all JPEG saves to survive power cuts
- **Session-based** — new calendar day = new subfolder, no manual intervention needed
- **Raspberry Pi OS Bookworm 64-bit**, `rpicam-still` (not legacy `raspistill`)
- **Pi 3 AP host** — 2.4 GHz 802.11n only; Pi 4 display node connects as WiFi client

## Repository Structure

```
timelapse-hq-fork/
├── README.md
├── config/
│   └── config.yaml              # Shared defaults: capture interval, resolution, quality, backup schedule
├── pi1/
│   ├── setup.sh                 # Idempotent: install deps, configure AP, enable camera-server
│   ├── update.sh                # git pull + service restart
│   ├── camera_server.py         # rpicam-still loop + Python HTTP server serving /tmp/latest.jpg
│   ├── camera-server.service
│   ├── hostapd.conf
│   └── dnsmasq.conf
├── pi2/
│   ├── setup.sh                 # Install deps, format USBs, configure WiFi client, enable services
│   ├── update.sh
│   ├── grabber.py               # Main loop: HTTP poll → timestamped JPEG save
│   ├── grabber.service
│   ├── playback.service         # mpv fullscreen slideshow from /data/timelapse/current/*/*.jpg
│   ├── backup.sh                # rsync /data/timelapse/ → /backup/YYYY-MM-DD/
│   ├── backup.timer + backup.service
│   ├── wifi-retry.service
│   └── portal/                  # Optional Flask admin portal
│       ├── portal.py
│       └── portal.service
├── common/
│   └── disk_monitor.sh          # Flag file if disk > 85%
└── .gitignore
```

## Role Assignment

### Pi1 — Camera + AP Master

| Function | Details |
|----------|---------|
| WiFi AP | `timelapse-ap`, subnet `192.168.50.0/24`, WPA2, 2.4 GHz only (Pi 3 limitation), password configurable |
| Camera | HQ Camera (Raspberry Pi HQ Camera or Module 3) |
| Capture | `rpicam-still` loop → `/tmp/latest.jpg` (tmpfs, RAM only) |
| HTTP Server | Serves `/tmp/latest.jpg` on port 8080 |
| Services | `camera-server.service` + `hostapd` + `dnsmasq` |

### Pi2 — Display + Brain (WiFi Client)

| Function | Details |
|----------|---------|
| WiFi | Connects as client to Pi1's AP |
| Image Grabber | Every N seconds (default: 5): fetches `http://192.168.50.1:8080/latest.jpg`, saves as `/data/timelapse/current/YYYY-MM-DD/HH-MM-SS.jpg` |
| Playback | `mpv` fullscreen slideshow on Waveshare round display |
| Backup | Daily `rsync` from `/data/timelapse/` to second USB stick at `/backup/` |
| Admin Portal | Optional Flask dashboard on port 80 |
| Storage | 1–2 USB sticks: `/data/` (working) and `/backup/` (backup) |
| Config | All config lives in `/data/config.yaml` |

## Quick Start

### Prerequisites

- 1× Raspberry Pi 3 (Pi1 — Camera + AP) with Raspberry Pi OS Bookworm 64-bit
- 1× Raspberry Pi 4 (Pi2 — Display) with Raspberry Pi OS Bookworm 64-bit
- 1× Raspberry Pi HQ Camera (or Camera Module 3) attached to Pi1
- 1–2× USB sticks (exFAT formatted) for Pi2 storage
- 1× Display (HDMI or Waveshare round) connected to Pi2

### Pi1 Setup (Camera + AP)

```bash
# SSH into Pi1
git clone <this-repo> /opt/timelapse-repo
cd /opt/timelapse-repo
sudo bash pi1/setup.sh
sudo reboot
```

### Pi2 Setup (Display + Brain)

```bash
# SSH into Pi2
git clone <this-repo> /opt/timelapse-repo
cd /opt/timelapse-repo
sudo bash pi2/setup.sh
sudo reboot
```

### Boot Sequence

1. **Pi1 boots first** → starts WiFi AP, camera capture + HTTP server
2. **Pi2 boots** → connects to AP, starts grabber (waits for Pi1)
3. **Pi2 grabber** → detects Pi1, begins saving timestamped JPEGs
4. **Pi2 playback** → starts mpv slideshow once frames are available

## Configuration

All settings are in `config/config.yaml` (deployed to `/data/config.yaml` on Pi2):

```yaml
capture:
  interval_seconds: 5
  width: 4056
  height: 3040
  quality: 95

grabber:
  poll_interval_seconds: 5
  pi1_url: "http://192.168.50.1:8080/latest.jpg"

playback:
  fps: 25

network:
  ap_ssid: "timelapse-ap"
  ap_password: "changeme2"
```

Configuration can also be changed at runtime via the Flask admin portal at `http://192.168.50.20/`.

## API Endpoints

### Pi1 Camera Server (port 8080)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/latest.jpg` | GET | Serve the latest captured JPEG |
| `/health` | GET | JSON health check with last capture timestamp |

### Pi2 Admin Portal (port 80)

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
- **Boot-order tolerant** — Pi2 grabber gracefully handles Pi1 being offline
- **Disk monitoring** — cron job flags disk usage > 85% with hysteresis

## License

See repository license file for details.
