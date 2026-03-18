#!/usr/bin/env python3
"""
Pi 2 — Status API
Timelapse Art Installation

Flask app on port 5000 providing status information and
control endpoints for the playback service.
No authentication (isolated local network).
"""
import json
import glob
import os
import subprocess
import time
import threading
import logging
import sys

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [status-api] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("status-api")

app = Flask(__name__)

LOCAL_CACHE = "/data/cache"
LAST_SYNC_FILE = "/data/last_sync.txt"
MPV_SOCKET = "/tmp/mpv-socket"
CONFIG_LOCAL = "/data/config_local.json"
WIFI_STATUS_CACHE = {"timestamp": 0.0, "expected_ssid": None, "value": {"state": "warning", "message": "WiFi connection unavailable"}}
WIFI_STATUS_LOCK = threading.Lock()


def read_config() -> dict:
    defaults = {"playback_fps": 25, "display_brightness": 100, "wifi_ssid": "timelapse-ap"}
    for path in ["/mnt/timelapse/../config.json", CONFIG_LOCAL]:
        try:
            real = os.path.realpath(path)
            with open(real) as f:
                cfg = json.load(f)
            for k, v in defaults.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            continue
    return defaults


def get_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        return f"{d}d {h}h {m}m"
    except Exception:
        return "–"


def get_cpu_temp() -> str:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read().strip()) / 1000:.1f}°C"
    except Exception:
        return "–"


def get_disk_usage() -> tuple:
    try:
        st = os.statvfs("/data")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        return round(used / 1e9, 1), round(total / 1e9, 1)
    except Exception:
        return (0, 0)


def get_frame_info() -> tuple:
    """Return (current_frame_number, total_frames)."""
    frames = sorted(glob.glob(os.path.join(LOCAL_CACHE, "frame_*.jpg")))
    total = len(frames)
    current = 0
    if frames:
        try:
            base = os.path.basename(frames[-1])
            current = int(base.replace("frame_", "").replace(".jpg", ""))
        except ValueError:
            current = total
    return current, total


def get_session_id() -> str:
    try:
        with open(os.path.join(LOCAL_CACHE, "session.id")) as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def get_last_sync() -> str:
    try:
        with open(LAST_SYNC_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def get_wifi_ssid() -> str:
    try:
        result = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_wifi_ip() -> str:
    try:
        result = subprocess.run(["ip", "-4", "-o", "addr", "show", "wlan0"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return ""
        parts = result.stdout.split()
        if "inet" in parts:
            return parts[parts.index("inet") + 1].split("/", 1)[0]
    except Exception:
        return ""
    return ""


def get_wifi_status(expected_ssid: str) -> dict:
    now = time.time()
    with WIFI_STATUS_LOCK:
        if (
            WIFI_STATUS_CACHE["expected_ssid"] == expected_ssid
            and now - WIFI_STATUS_CACHE["timestamp"] < 5
        ):
            return dict(WIFI_STATUS_CACHE["value"])
    current_ssid = get_wifi_ssid()
    current_ip = get_wifi_ip()
    if current_ssid and current_ssid == expected_ssid:
        details = current_ssid
        if current_ip:
            details += f" ({current_ip})"
        value = {"state": "ok", "message": details}
    elif current_ssid:
        details = f"Connected to {current_ssid}"
        if current_ip:
            details += f" ({current_ip})"
        if expected_ssid:
            details += f", expected {expected_ssid}"
        value = {"state": "warning", "message": details}
    elif expected_ssid:
        value = {"state": "warning", "message": f"Not connected to {expected_ssid}"}
    else:
        value = {"state": "warning", "message": "WiFi connection unavailable"}
    with WIFI_STATUS_LOCK:
        WIFI_STATUS_CACHE.update({"timestamp": now, "expected_ssid": expected_ssid, "value": value})
    return dict(value)


def get_playback_state() -> str:
    """Check if mpv is running."""
    try:
        result = subprocess.run(["pgrep", "-f", "mpv.*timelapse"],
                                capture_output=True)
        if result.returncode == 0:
            if os.path.exists("/tmp/rendering_in_progress"):
                return "rendering"
            return "playing"
        return "stopped"
    except Exception:
        return "unknown"


def mpv_command(cmd: list):
    """Send command to mpv via IPC socket."""
    import socket as sock
    try:
        s = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
        s.settimeout(2)
        s.connect(MPV_SOCKET)
        payload = json.dumps({"command": cmd}) + "\n"
        s.sendall(payload.encode())
        resp = s.recv(4096)
        s.close()
        return json.loads(resp.decode().strip())
    except Exception as e:
        log.debug("mpv IPC error: %s", e)
        return {"error": str(e)}


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    try:
        cfg = read_config()
        frame_current, frame_total = get_frame_info()
        used, total = get_disk_usage()
        wifi_status = get_wifi_status(cfg.get("wifi_ssid", ""))
        return jsonify({
            "frame_current": frame_current,
            "frame_total": frame_total,
            "playback_state": get_playback_state(),
            "uptime": get_uptime(),
            "cpu_temp": get_cpu_temp(),
            "disk_used_gb": used,
            "disk_total_gb": total,
            "last_sync_timestamp": get_last_sync(),
            "session_id": get_session_id(),
            "fps": cfg.get("playback_fps", 25),
            "wifi_state": wifi_status["state"],
            "wifi_message": wifi_status["message"],
        })
    except Exception:
        log.exception("Status endpoint failed")
        return jsonify({"error": "Status temporarily unavailable"}), 500


@app.route("/display/brightness", methods=["POST"])
def set_brightness():
    """Set display brightness (0–100)."""
    data = request.get_json(force=True, silent=True) or {}
    value = data.get("value", 100)
    try:
        value = max(0, min(100, int(value)))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid brightness value"}), 400

    # Try setting brightness via sysfs (HDMI backlight)
    brightness_paths = [
        "/sys/class/backlight/rpi_backlight/brightness",
        "/sys/class/backlight/10-0045/brightness",
    ]
    for bp in brightness_paths:
        if os.path.exists(bp):
            try:
                # Read max brightness
                max_path = os.path.join(os.path.dirname(bp), "max_brightness")
                max_val = 255
                if os.path.exists(max_path):
                    with open(max_path) as f:
                        max_val = int(f.read().strip())
                actual = int(value / 100.0 * max_val)
                with open(bp, "w") as f:
                    f.write(str(actual))
                log.info("Brightness set to %d%% (%d/%d)", value, actual, max_val)
                return jsonify({"ok": True, "value": value})
            except Exception as e:
                log.warning("Failed to set brightness via %s: %s", bp, e)

    # Fallback: try xrandr
    try:
        subprocess.run(
            ["xrandr", "--output", "HDMI-1", "--brightness", str(value / 100.0)],
            capture_output=True, timeout=5)
        return jsonify({"ok": True, "value": value})
    except Exception:
        pass

    return jsonify({"error": "No supported brightness control found"}), 500


@app.route("/playback/pause", methods=["POST"])
def pause():
    result = mpv_command(["set_property", "pause", True])
    return jsonify({"ok": True, "mpv": result})


@app.route("/playback/resume", methods=["POST"])
def resume():
    result = mpv_command(["set_property", "pause", False])
    return jsonify({"ok": True, "mpv": result})


@app.route("/playback/reload", methods=["POST"])
def reload_playback():
    """Restart the playback service to re-read config."""
    result = subprocess.run(["systemctl", "restart", "playback.service"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "Failed to restart playback.service"}), 500
    return jsonify({"ok": True, "message": "Playback service restarting"})


@app.route("/sync/now", methods=["POST"])
def sync_now():
    result = subprocess.run(["systemctl", "restart", "sync.service"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "Failed to restart sync.service"}), 500
    return jsonify({"ok": True, "message": "Sync service restarting"})


@app.route("/health")
def health():
    """Lightweight health-check endpoint for monitoring."""
    checks = {}
    try:
        frame_current, frame_total = get_frame_info()
        checks["frame_total"] = frame_total
    except Exception:
        checks["frame_total"] = None
    try:
        checks["last_sync"] = get_last_sync() or None
    except Exception:
        checks["last_sync"] = None
    try:
        checks["playback_state"] = get_playback_state()
    except Exception:
        checks["playback_state"] = "unknown"
    return jsonify({"status": "ok", **checks})


@app.errorhandler(500)
def handle_500(exc):
    log.exception("Unhandled server error: %s", exc)
    return jsonify({"error": "Internal server error — check status-api logs"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
