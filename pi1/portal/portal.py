#!/usr/bin/env python3
"""
Pi1 — Flask Admin Portal
Optional web-based admin interface on port 80 for managing the timelapse system.

Features:
  - View system status (disk, CPU, services)
  - Adjust capture interval, playback FPS
  - View latest captured images
  - Check Pi2 camera health
  - Trigger manual backup
"""

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "80"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("portal")

app = Flask(__name__)
app.secret_key = os.urandom(32)


def load_config() -> dict:
    """Load full config from YAML file."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("Error reading config: %s", exc)
        return {}


def save_config(cfg: dict) -> None:
    """Save config back to YAML file (atomic write)."""
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.rename(tmp_path, CONFIG_PATH)


def get_disk_usage(path: str) -> dict:
    """Get disk usage for a mount point."""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = (used / total * 100) if total > 0 else 0
        return {
            "total_gb": round(total / (1024**3), 1),
            "used_gb": round(used / (1024**3), 1),
            "free_gb": round(free / (1024**3), 1),
            "percent": round(pct, 1),
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0}


def get_cpu_temp() -> str:
    """Read Raspberry Pi CPU temperature."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(f.read().strip()) / 1000
        return f"{temp_c:.1f}°C"
    except Exception:
        return "N/A"


def get_service_status(name: str) -> str:
    """Get systemd service status."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def count_frames() -> int:
    """Count total JPEG files in timelapse directory."""
    timelapse_dir = Path("/data/timelapse/current")
    if not timelapse_dir.exists():
        return 0
    return sum(1 for _ in timelapse_dir.rglob("*.jpg"))


def check_pi2_health() -> dict:
    """Check Pi2 camera server health endpoint."""
    cfg = load_config()
    pi2_ip = cfg.get("network", {}).get("pi2_ip", "192.168.50.20")
    url = f"http://{pi2_ip}:8080/health"
    try:
        response = urlopen(url, timeout=5)
        return json.loads(response.read().decode())
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Timelapse Admin</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
               background: #1a1a2e; color: #e0e0e0; padding: 20px; }
        h1 { color: #00d4ff; margin-bottom: 20px; }
        h2 { color: #00d4ff; margin: 15px 0 10px; font-size: 1.1em; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 15px; margin-bottom: 20px; }
        .card { background: #16213e; border-radius: 8px; padding: 15px;
                border: 1px solid #0f3460; }
        .status { display: inline-block; padding: 2px 8px; border-radius: 4px;
                  font-size: 0.85em; font-weight: bold; }
        .status.active { background: #00c853; color: #000; }
        .status.inactive { background: #ff5252; color: #fff; }
        .status.unknown { background: #666; color: #fff; }
        label { display: block; margin: 8px 0 3px; font-size: 0.9em; color: #aaa; }
        input[type=number], input[type=text] {
            width: 100%; padding: 6px 10px; border-radius: 4px;
            border: 1px solid #0f3460; background: #1a1a2e; color: #e0e0e0; }
        button { padding: 8px 16px; border-radius: 4px; border: none;
                 background: #00d4ff; color: #000; cursor: pointer;
                 font-weight: bold; margin-top: 10px; }
        button:hover { background: #00b8d4; }
        .metric { font-size: 1.3em; font-weight: bold; color: #fff; }
        .metric-label { font-size: 0.8em; color: #888; }
        .bar { height: 8px; background: #333; border-radius: 4px; margin-top: 5px; }
        .bar-fill { height: 100%; border-radius: 4px; background: #00d4ff; }
        table { width: 100%; border-collapse: collapse; }
        td { padding: 4px 0; }
        td:first-child { color: #888; }
    </style>
</head>
<body>
    <h1>🎥 Timelapse Admin Portal</h1>
    <div class="grid">
        <div class="card">
            <h2>📊 System Status</h2>
            <table>
                <tr><td>CPU Temp</td><td>{{ cpu_temp }}</td></tr>
                <tr><td>Frames</td><td class="metric">{{ frame_count }}</td></tr>
                <tr><td>Uptime</td><td>{{ uptime }}</td></tr>
            </table>
        </div>
        <div class="card">
            <h2>💾 Disk Usage (/data)</h2>
            <div class="metric">{{ disk.used_gb }} / {{ disk.total_gb }} GB</div>
            <div class="bar"><div class="bar-fill" style="width:{{ disk.percent }}%"></div></div>
            <div class="metric-label">{{ disk.percent }}% used, {{ disk.free_gb }} GB free</div>
        </div>
        <div class="card">
            <h2>📷 Pi2 Camera</h2>
            <table>
                <tr><td>Status</td><td>
                    <span class="status {{ 'active' if pi2.status == 'ok' else 'inactive' }}">
                        {{ pi2.status }}
                    </span>
                </td></tr>
                <tr><td>Last Capture</td><td>{{ pi2.get('last_capture', 'N/A') }}</td></tr>
            </table>
        </div>
        <div class="card">
            <h2>⚙️ Services</h2>
            <table>
                {% for svc, status in services.items() %}
                <tr><td>{{ svc }}</td><td>
                    <span class="status {{ 'active' if status == 'active' else 'inactive' }}">
                        {{ status }}
                    </span>
                </td></tr>
                {% endfor %}
            </table>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>⚙️ Configuration</h2>
            <form method="POST" action="/config">
                <label>Capture Interval (seconds)</label>
                <input type="number" name="capture_interval" value="{{ config.capture.interval_seconds }}" min="1">
                <label>Poll Interval (seconds)</label>
                <input type="number" name="poll_interval" value="{{ config.grabber.poll_interval_seconds }}" min="1">
                <label>Playback FPS</label>
                <input type="number" name="playback_fps" value="{{ config.playback.fps }}" min="1" max="60">
                <label>WiFi SSID</label>
                <input type="text" name="wifi_ssid" value="{{ config.network.ap_ssid }}">
                <button type="submit">Save Configuration</button>
            </form>
        </div>
        <div class="card">
            <h2>🔧 Actions</h2>
            <form method="POST" action="/backup" style="display:inline">
                <button type="submit">Run Backup Now</button>
            </form>
        </div>
    </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    cfg = load_config()
    # Ensure nested dicts exist for template rendering
    cfg.setdefault("capture", {"interval_seconds": 5})
    cfg.setdefault("grabber", {"poll_interval_seconds": 5})
    cfg.setdefault("playback", {"fps": 25})
    cfg.setdefault("network", {"ap_ssid": "timelapse-ap"})

    try:
        uptime_str = subprocess.check_output(
            ["uptime", "-p"], text=True, timeout=5,
        ).strip()
    except Exception:
        uptime_str = "N/A"

    return render_template_string(
        DASHBOARD_HTML,
        config=cfg,
        cpu_temp=get_cpu_temp(),
        disk=get_disk_usage("/data"),
        frame_count=count_frames(),
        pi2=check_pi2_health(),
        uptime=uptime_str,
        services={
            "grabber": get_service_status("grabber"),
            "playback": get_service_status("playback"),
            "hostapd": get_service_status("hostapd"),
            "dnsmasq": get_service_status("dnsmasq"),
            "portal": get_service_status("portal"),
        },
    )


@app.route("/config", methods=["POST"])
def update_config():
    cfg = load_config()
    cfg.setdefault("capture", {})
    cfg.setdefault("grabber", {})
    cfg.setdefault("playback", {})
    cfg.setdefault("network", {})

    try:
        capture_interval = int(request.form.get("capture_interval", 5))
        cfg["capture"]["interval_seconds"] = max(1, capture_interval)
    except (ValueError, TypeError):
        pass

    try:
        poll_interval = int(request.form.get("poll_interval", 5))
        cfg["grabber"]["poll_interval_seconds"] = max(1, poll_interval)
    except (ValueError, TypeError):
        pass

    try:
        fps = int(request.form.get("playback_fps", 25))
        cfg["playback"]["fps"] = max(1, min(60, fps))
    except (ValueError, TypeError):
        pass

    ssid = request.form.get("wifi_ssid", "").strip()
    if ssid:
        cfg["network"]["ap_ssid"] = ssid

    save_config(cfg)
    log.info("Configuration updated via portal")
    return redirect(url_for("dashboard"))


@app.route("/backup", methods=["POST"])
def trigger_backup():
    try:
        subprocess.Popen(
            ["/opt/timelapse/backup.sh"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("Manual backup triggered")
    except Exception as exc:
        log.error("Failed to trigger backup: %s", exc)
    return redirect(url_for("dashboard"))


@app.route("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({
        "cpu_temp": get_cpu_temp(),
        "disk": get_disk_usage("/data"),
        "frame_count": count_frames(),
        "pi2_health": check_pi2_health(),
        "services": {
            "grabber": get_service_status("grabber"),
            "playback": get_service_status("playback"),
            "hostapd": get_service_status("hostapd"),
        },
    })


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Pi1 Admin Portal starting on port %d", PORTAL_PORT)
    app.run(host="0.0.0.0", port=PORTAL_PORT, debug=False)


if __name__ == "__main__":
    main()
