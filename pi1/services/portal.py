#!/usr/bin/env python3
"""
Pi 1 — Captive Portal / Admin Web UI
Timelapse Art Installation

Flask app on port 80 with session-cookie auth.
Serves the single-page admin dashboard for all configuration,
session management, and system monitoring.
"""
import json
import os
import glob
import shutil
import subprocess
import time
import functools
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, jsonify, send_file, abort, flash,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [portal] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("portal")

app = Flask(__name__)
app.secret_key = os.urandom(32)

CONFIG_PATH = "/data/config.json"
CURRENT_DIR = "/data/timelapse/current"
ARCHIVE_DIR = "/data/timelapse/archive"
LAST_CAPTURE = "/data/last_capture.txt"
LAST_BACKUP = "/data/last_backup.txt"
BACKUP_WARNING = "/data/backup_warning.flag"
DISK_WARNING = "/data/disk_warning.flag"
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
SESSION_NOTE_FILE = "session_note.txt"

# ── config helpers ───────────────────────────────────────────────────────────

def read_config() -> dict:
    defaults = {
        "capture_interval_minutes": 5,
        "exposure_mode": "auto",
        "exposure_shutter_speed": 10000,
        "exposure_iso": 100,
        "luma_target": None,
        "playback_fps": 25,
        "display_brightness": 100,
        "admin_password": "changeme",
        "wifi_ssid": "timelapse-ap",
        "wifi_password": "changeme2",
        "display_type": "hdmi",
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return defaults


def write_config(cfg: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.rename(tmp, CONFIG_PATH)


def clamp_int(value, default: int, minimum: int = None, maximum: int = None, allowed=None) -> int:
    """Parse an int and constrain it to a safe range or set of values."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if allowed is not None and parsed not in allowed:
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def allowed_value(value, default, allowed):
    """Return a value only if it is explicitly allowed."""
    return value if value in allowed else default


def sanitize_ssid(value: str, default: str):
    """Trim and validate an SSID without being overly restrictive."""
    ssid = (value or "").strip()
    if not ssid:
        return default, "WiFi SSID cannot be blank, so the previous SSID was kept."
    if len(ssid) > 32 or any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid):
        return default, "WiFi SSID must be 1-32 printable characters, so the previous SSID was kept."
    return ssid, None


# ── auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ── system info helpers ──────────────────────────────────────────────────────

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


def get_disk_usage(path: str) -> dict:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        return {
            "path": path,
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "free_gb": round(free / 1e9, 1),
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent": round(used / total * 100, 1) if total else 0,
        }
    except Exception:
        return {
            "path": path,
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "percent": 0,
        }


def count_frames(directory: str) -> int:
    return len(glob.glob(os.path.join(directory, "frame_*.jpg")))


def read_timestamp_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def get_session_id() -> str:
    try:
        with open(os.path.join(CURRENT_DIR, "session.id")) as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def read_key_value_file(path: str) -> dict:
    values = {}
    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"')
    except Exception:
        return {}
    return values


def build_status(level: str, label: str, message: str) -> dict:
    badge_class = {
        "ok": "badge-ok",
        "warning": "badge-warn",
        "error": "badge-err",
    }.get(level, "badge-off")
    return {
        "level": level,
        "label": label,
        "message": message,
        "badge_class": badge_class,
        "needs_attention": level in {"warning", "error"},
    }


def get_pi1_wifi_status(cfg: dict) -> dict:
    applied = read_key_value_file(HOSTAPD_CONF)
    applied_ssid = applied.get("ssid", "")
    applied_password = applied.get("wpa_passphrase", "")
    configured_ssid = cfg.get("wifi_ssid", "")
    configured_password = cfg.get("wifi_password", "")
    if not applied_ssid:
        return build_status(
            "warning",
            "Unavailable",
            "Pi 1 setup still needs to be run once so the access-point settings can be applied.",
        )
    if applied_ssid != configured_ssid or applied_password != configured_password:
        return build_status(
            "warning",
            "Pending",
            f"Pi 1 setup still needs rerun to switch the AP from “{applied_ssid}” to “{configured_ssid}”.",
        )
    return build_status("ok", "Applied", f"Pi 1 is already serving the “{configured_ssid}” access point.")


def get_backup_status(last_backup: str) -> dict:
    if not os.path.ismount("/backup"):
        return build_status("error", "Missing", "Backup stick is not mounted at /backup.")
    try:
        if os.stat("/backup").st_dev == os.stat("/").st_dev:
            return build_status("error", "Wrong disk", "Backup path points to the same device as the SD card.")
    except Exception:
        return build_status("error", "Unavailable", "Backup device health could not be verified.")
    if not last_backup:
        return build_status("warning", "Waiting", "Backup stick is ready, but no successful backup has been recorded yet.")
    try:
        last_backup_dt = datetime.fromisoformat(last_backup)
    except Exception:
        return build_status("warning", "Unknown", f"Backup timestamp “{last_backup}” could not be parsed.")
    backup_age = datetime.now() - last_backup_dt
    if backup_age > timedelta(hours=26):
        return build_status("warning", "Stale", f"Last successful backup was {last_backup_dt.isoformat(timespec='minutes')}.")
    return build_status("ok", "Healthy", f"Last successful backup finished {last_backup_dt.isoformat(timespec='minutes')}.")


def get_disk_status(disk_data: dict) -> dict:
    data_path = disk_data.get("path", "/data")
    if not os.path.ismount(data_path):
        return build_status("error", "Missing", f"Working storage is not mounted at {data_path}.")
    percent = disk_data.get("percent", 0)
    if percent >= 92:
        return build_status("error", "Critical", f"Working stick is {percent:.1f}% full.")
    if percent >= 85:
        return build_status("warning", "Tight", f"Working stick is {percent:.1f}% full.")
    return build_status("ok", "Healthy", f"Working stick has {disk_data.get('free_gb', 0):.1f} GB free.")


def read_session_note(session_dir: str) -> str:
    try:
        with open(os.path.join(session_dir, SESSION_NOTE_FILE)) as f:
            return f.read().strip()
    except Exception:
        return ""


def sanitize_session_note(value: str) -> str:
    note = " ".join((value or "").split())
    note = "".join(ch for ch in note if ch.isprintable())
    return note[:80]


def get_archive_path(name: str):
    candidate = os.path.abspath(os.path.join(ARCHIVE_DIR, os.path.basename(name or "")))
    archive_root = os.path.abspath(ARCHIVE_DIR)
    if os.path.commonpath([candidate, archive_root]) != archive_root or not os.path.isdir(candidate):
        return None
    return candidate


def short_session_label(name: str) -> str:
    return name[4:8] if len(name) >= 8 else name


def estimate_frame_size(directory: str, sample_size: int = 24) -> int:
    frames = sorted(glob.glob(os.path.join(directory, "frame_*.jpg")))
    if not frames:
        return 0
    sample = frames[-sample_size:]
    sizes = []
    for frame in sample:
        try:
            sizes.append(os.path.getsize(frame))
        except OSError:
            continue
    return int(sum(sizes) / len(sizes)) if sizes else 0


def build_storage_projection(frames: int, archives: list, disk_data: dict, capture_interval_minutes: int) -> dict:
    average_frame_bytes = estimate_frame_size(CURRENT_DIR)
    remaining_frames = int(disk_data.get("free_bytes", 0) / average_frame_bytes) if average_frame_bytes else 0
    remaining_hours = round((remaining_frames * capture_interval_minutes) / 60.0, 1) if remaining_frames else 0
    remaining_days = round(remaining_hours / 24.0, 1) if remaining_hours else 0
    chart = []
    recent_archives = list(reversed(archives[:5]))
    for archive in recent_archives:
        chart.append({
            "label": short_session_label(archive["name"]),
            "frames": archive["frames"],
        })
    chart.append({"label": "now", "frames": frames})
    max_frames = max((point["frames"] for point in chart), default=0)
    return {
        "average_frame_mb": round(average_frame_bytes / 1e6, 2) if average_frame_bytes else 0,
        "remaining_frames": remaining_frames,
        "remaining_hours": remaining_hours,
        "remaining_days": remaining_days,
        "chart": chart,
        "chart_max_frames": max_frames,
    }


def list_archives() -> list:
    archives = []
    if os.path.isdir(ARCHIVE_DIR):
        for name in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
            d = os.path.join(ARCHIVE_DIR, name)
            if os.path.isdir(d):
                archives.append({
                    "name": name,
                    "frames": count_frames(d),
                    "date": name,
                    "note": read_session_note(d),
                })
    return archives


def next_session_id() -> str:
    """Generate a session ID that preserves the existing format unless needed."""
    base_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = base_id
    suffix = 1
    while os.path.exists(os.path.join(ARCHIVE_DIR, candidate)):
        candidate = f"{base_id}_{suffix}"
        suffix += 1
    return candidate


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        cfg = read_config()
        if request.form.get("password") == cfg["admin_password"]:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Invalid password."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    cfg = read_config()
    frames = count_frames(CURRENT_DIR)
    last_cap = read_timestamp_file(LAST_CAPTURE)
    last_bak = read_timestamp_file(LAST_BACKUP)
    session_id = get_session_id()
    archives = list_archives()
    disk_data = get_disk_usage("/data")
    disk_backup = get_disk_usage("/backup")
    backup_status = get_backup_status(last_bak)
    disk_status = get_disk_status(disk_data)
    wifi_status = get_pi1_wifi_status(cfg)
    storage_projection = build_storage_projection(
        frames,
        archives,
        disk_data,
        cfg["capture_interval_minutes"],
    )

    fps_options = [6, 12, 18, 25, 30, 48, 60, 120]
    # Compute durations for each FPS option
    fps_durations = {}
    for fps in fps_options:
        if fps > 0:
            dur = frames / fps
            fps_durations[fps] = round(dur, 1)
        else:
            fps_durations[fps] = 0

    return render_template_string(
        DASHBOARD_HTML,
        cfg=cfg,
        frames=frames,
        last_capture=last_cap,
        last_backup=last_bak,
        session_id=session_id,
        archives=archives,
        disk_data=disk_data,
        disk_backup=disk_backup,
        uptime=get_uptime(),
        cpu_temp=get_cpu_temp(),
        backup_status=backup_status,
        disk_status=disk_status,
        wifi_status=wifi_status,
        storage_projection=storage_projection,
        next_session_preview=next_session_id(),
        fps_options=fps_options,
        fps_durations=fps_durations,
    )


@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    cfg = read_config()
    old_cfg = dict(cfg)
    data = request.form
    section = data.get("section", "settings")
    messages = []

    # Capture settings
    if "capture_interval_minutes" in data:
        cfg["capture_interval_minutes"] = clamp_int(
            data["capture_interval_minutes"],
            cfg["capture_interval_minutes"],
            allowed={1, 5, 10, 15, 30},
        )
    if "exposure_mode" in data:
        cfg["exposure_mode"] = allowed_value(data["exposure_mode"], cfg["exposure_mode"], {"auto", "manual"})
    if "exposure_shutter_speed" in data:
        cfg["exposure_shutter_speed"] = clamp_int(
            data["exposure_shutter_speed"],
            cfg["exposure_shutter_speed"],
            minimum=100,
            maximum=200000,
        )
    if "exposure_iso" in data:
        cfg["exposure_iso"] = clamp_int(
            data["exposure_iso"],
            cfg["exposure_iso"],
            minimum=100,
            maximum=3200,
        )
    if "luma_enabled" in data:
        if data["luma_enabled"] == "on":
            cfg["luma_target"] = clamp_int(
                data.get("luma_target", 128),
                cfg["luma_target"] if cfg.get("luma_target") is not None else 128,
                minimum=0,
                maximum=255,
            )
        else:
            cfg["luma_target"] = None
    elif "luma_enabled" in data or "luma_target" in data:
        cfg["luma_target"] = None

    # Playback settings
    if "playback_fps" in data:
        cfg["playback_fps"] = clamp_int(
            data["playback_fps"],
            cfg["playback_fps"],
            allowed={6, 12, 18, 25, 30, 48, 60, 120},
        )
    if "display_brightness" in data:
        cfg["display_brightness"] = clamp_int(
            data["display_brightness"],
            cfg["display_brightness"],
            minimum=0,
            maximum=100,
        )

    # Admin settings
    if "admin_password" in data and data["admin_password"]:
        cfg["admin_password"] = data["admin_password"]
        if cfg["admin_password"] != old_cfg.get("admin_password"):
            messages.append(("success", "Admin password updated."))
    wifi_changed = False
    if "wifi_ssid" in data:
        wifi_ssid, ssid_error = sanitize_ssid(data["wifi_ssid"], old_cfg.get("wifi_ssid", cfg["wifi_ssid"]))
        if ssid_error:
            messages.append(("warning", ssid_error))
        elif wifi_ssid != old_cfg.get("wifi_ssid"):
            wifi_changed = True
            cfg["wifi_ssid"] = wifi_ssid
    if "wifi_password" in data and data["wifi_password"]:
        if len(data["wifi_password"]) < 8:
            messages.append(("warning", "WiFi password must be at least 8 characters, so the previous password was kept."))
        else:
            if data["wifi_password"] != old_cfg.get("wifi_password"):
                wifi_changed = True
            cfg["wifi_password"] = data["wifi_password"]
    if wifi_changed:
        messages.append(("warning", "WiFi settings saved. Re-run Pi 1 setup and update Pi 2 so it can reconnect."))

    write_config(cfg)
    if not messages:
        messages.append(("success", f"{section.capitalize()} settings saved."))
    for category, message in messages:
        flash(message, category)
    return redirect(url_for("dashboard"))


@app.route("/api/new_session", methods=["POST"])
@login_required
def new_session():
    """Archive current session and start fresh."""
    session_id = get_session_id()
    frame_count = count_frames(CURRENT_DIR)

    if frame_count > 0:
        archive_dest = os.path.join(ARCHIVE_DIR, session_id)
        os.makedirs(archive_dest, exist_ok=True)
        # Move all files from current to archive
        for f in os.listdir(CURRENT_DIR):
            src = os.path.join(CURRENT_DIR, f)
            dst = os.path.join(archive_dest, f)
            shutil.move(src, dst)

    # Create fresh session
    os.makedirs(CURRENT_DIR, exist_ok=True)
    new_id = next_session_id()
    tmp = os.path.join(CURRENT_DIR, "session.id.tmp")
    with open(tmp, "w") as f:
        f.write(new_id)
    os.rename(tmp, os.path.join(CURRENT_DIR, "session.id"))

    # Restart capture service to pick up fresh session
    subprocess.run(["systemctl", "restart", "capture.service"], capture_output=True)

    flash("Started a new session and archived the previous frames.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/archive_note", methods=["POST"])
@login_required
def archive_note():
    archive_name = request.form.get("archive_name", "")
    archive_dir = get_archive_path(archive_name)
    if not archive_dir:
        flash("Archive not found.", "error")
        return redirect(url_for("dashboard"))
    note = sanitize_session_note(request.form.get("note", ""))
    note_path = os.path.join(archive_dir, SESSION_NOTE_FILE)
    if note:
        tmp = note_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(note)
        os.rename(tmp, note_path)
        flash(f"Saved label for {archive_name}.", "success")
    else:
        try:
            os.remove(note_path)
        except FileNotFoundError:
            pass
        flash(f"Cleared label for {archive_name}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/thumbnail")
@login_required
def thumbnail():
    """Return the last captured frame as a thumbnail."""
    frames = sorted(glob.glob(os.path.join(CURRENT_DIR, "frame_*.jpg")))
    if not frames:
        abort(404)
    return send_file(frames[-1], mimetype="image/jpeg")


@app.route("/api/pi1_status")
@login_required
def pi1_status():
    """JSON status for AJAX polling."""
    cfg = read_config()
    return jsonify({
        "uptime": get_uptime(),
        "cpu_temp": get_cpu_temp(),
        "disk_data": get_disk_usage("/data"),
        "disk_backup": get_disk_usage("/backup"),
        "frames": count_frames(CURRENT_DIR),
        "last_capture": read_timestamp_file(LAST_CAPTURE),
        "last_backup": read_timestamp_file(LAST_BACKUP),
        "backup_warning": os.path.isfile(BACKUP_WARNING),
        "disk_warning": os.path.isfile(DISK_WARNING),
        "session_id": get_session_id(),
    })


@app.route("/generate_204")
@app.route("/hotspot-detect.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_portal_detect():
    """Captive portal detection endpoints — redirect to dashboard."""
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ── HTML templates ───────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timelapse Admin — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2rem;width:100%;max-width:380px}
h1{font-size:1.4rem;margin-bottom:1.5rem;text-align:center;color:#58a6ff}
input{width:100%;padding:.7rem;margin-bottom:1rem;border:1px solid #30363d;border-radius:6px;
background:#0d1117;color:#c9d1d9;font-size:1rem}
button{width:100%;padding:.7rem;background:#238636;color:#fff;border:none;border-radius:6px;
font-size:1rem;cursor:pointer;font-weight:600}
button:hover{background:#2ea043}
.error{color:#f85149;text-align:center;margin-bottom:1rem;font-size:.9rem}
</style></head>
<body><div class="card">
<h1>🎬 Timelapse Admin</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="POST">
<input type="password" name="password" placeholder="Admin password" autofocus required>
<button type="submit">Login</button>
</form></div></body></html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timelapse Admin Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0d1117;color:#c9d1d9;padding:1rem;max-width:960px;margin:0 auto;line-height:1.45}
h1{color:#58a6ff;margin-bottom:.5rem;font-size:1.5rem}
h2{color:#58a6ff;font-size:1.1rem;margin:1.5rem 0 .75rem;border-bottom:1px solid #30363d;padding-bottom:.3rem}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1rem;box-shadow:0 8px 24px rgba(1,4,9,.18)}
label{display:block;font-size:.85rem;color:#8b949e;margin-bottom:.3rem}
select,input[type=range],input[type=number],input[type=text],input[type=password]{
width:100%;padding:.4rem;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;margin-bottom:.75rem}
input[type=range]{padding:0}
button,.btn{display:inline-block;padding:.5rem 1rem;background:#238636;color:#fff;border:none;
border-radius:6px;cursor:pointer;font-size:.9rem;font-weight:600;text-decoration:none;margin-right:.5rem;margin-bottom:.5rem}
button:hover,.btn:hover{background:#2ea043}
.btn-danger{background:#da3633}
.btn-danger:hover{background:#f85149}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:12px;font-size:.75rem;font-weight:600}
.badge-ok{background:#238636;color:#fff}
.badge-warn{background:#d29922;color:#000}
.badge-err{background:#da3633;color:#fff}
.badge-off{background:#484f58;color:#c9d1d9}
.stat{font-size:1.3rem;font-weight:700;color:#f0f6fc}
.stat-label{font-size:.75rem;color:#8b949e}
.stat-row{display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:.5rem}
.stat-box{text-align:center}
.thumb{max-width:100%;border-radius:6px;margin-top:.5rem;border:1px solid #30363d}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{padding:.4rem .6rem;border-bottom:1px solid #21262d;text-align:left}
th{color:#8b949e;font-weight:600}
.bar-chart{display:flex;align-items:flex-end;gap:4px;height:80px;margin:.5rem 0}
.bar{background:#238636;border-radius:3px 3px 0 0;min-width:28px;text-align:center;
font-size:.65rem;color:#fff;position:relative;transition:height .3s}
.bar span{position:absolute;top:-16px;left:0;right:0;font-size:.65rem;color:#8b949e}
.hidden{display:none}
.toggle-manual{transition:max-height .3s ease;overflow:hidden;max-height:0}
.toggle-manual.show{max-height:300px}
#pi2-status .offline{opacity:.4}
.warning-banner{background:#d29922;color:#000;padding:.5rem 1rem;border-radius:6px;margin-bottom:1rem;font-weight:600}
.error-banner{background:#da3633;color:#fff;padding:.5rem 1rem;border-radius:6px;margin-bottom:1rem;font-weight:600}
.success-banner{background:#1f6feb;color:#fff;padding:.5rem 1rem;border-radius:6px;margin-bottom:1rem;font-weight:600}
.helper{font-size:.8rem;color:#8b949e;margin-top:-.35rem;margin-bottom:.75rem;line-height:1.4}
.stack{display:flex;flex-direction:column;gap:.75rem}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.actions button,.actions .btn{margin:0}
.table-wrap{overflow-x:auto}
.archive-note-form{min-width:220px}
.archive-note-row{display:flex;gap:.5rem;align-items:flex-start}
.archive-note-row input{margin-bottom:0}
.status-summary{display:flex;flex-wrap:wrap;gap:.5rem;margin:.75rem 0}
.notice-card{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:.85rem;margin-top:.85rem}
.notice-title{font-size:.8rem;color:#8b949e;margin-bottom:.25rem;text-transform:uppercase;letter-spacing:.04em}
.compact-list{display:grid;gap:.35rem;font-size:.9rem}
.muted{color:#8b949e}
.mobile-stack{display:flex;flex-wrap:wrap;gap:.75rem}
.mobile-stack > *{flex:1 1 220px}
@media(max-width:820px){
  body{padding:.75rem}
  .stat-row{gap:.75rem}
  .stat-box{flex:1 1 120px}
}
@media(max-width:640px){
  body{padding:.5rem}
  h1{font-size:1.3rem}
  h2{font-size:1rem}
  button,.btn{width:100%;margin-right:0}
  .actions{flex-direction:column;align-items:stretch}
  .archive-note-row{flex-direction:column}
  th,td{padding:.45rem}
}
</style></head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
<h1>🎬 Timelapse Dashboard</h1>
<a href="/logout" class="btn" style="background:#484f58">Logout</a>
</div>

{% with flashes = get_flashed_messages(with_categories=true) %}
{% if flashes %}
  {% for category, message in flashes %}
  <div class="{{ 'warning-banner' if category == 'warning' else 'success-banner' if category == 'success' else 'error-banner' }}">{{ message }}</div>
  {% endfor %}
{% endif %}
{% endwith %}

{% if wifi_status.needs_attention %}
<div class="{{ 'warning-banner' if wifi_status.level == 'warning' else 'error-banner' }}">📶 {{ wifi_status.message }}</div>
{% endif %}
{% if backup_status.needs_attention %}
<div class="{{ 'warning-banner' if backup_status.level == 'warning' else 'error-banner' }}">💾 {{ backup_status.message }}</div>
{% endif %}
{% if disk_status.needs_attention %}
<div class="{{ 'warning-banner' if disk_status.level == 'warning' else 'error-banner' }}">🧮 {{ disk_status.message }}</div>
{% endif %}

<!-- ─── CAPTURE SETTINGS ─── -->
<h2>📷 Capture Settings</h2>
<form method="POST" action="/api/config" class="card">
  <input type="hidden" name="section" value="capture">
  <div class="grid">
    <div>
      <label>Capture Interval</label>
      <select name="capture_interval_minutes">
        {% for v in [1,5,10,15,30] %}
        <option value="{{v}}" {{'selected' if cfg.capture_interval_minutes==v}}>{{v}} min</option>
        {% endfor %}
      </select>
    </div>
    <div>
      <label>Exposure Mode</label>
      <select name="exposure_mode" id="exposure-mode" onchange="toggleManual()">
        <option value="auto" {{'selected' if cfg.exposure_mode=='auto'}}>Auto</option>
        <option value="manual" {{'selected' if cfg.exposure_mode=='manual'}}>Manual</option>
      </select>
    </div>
  </div>
  <div id="manual-controls" class="toggle-manual {{'show' if cfg.exposure_mode=='manual'}}">
    <div class="grid">
      <div>
        <label>Shutter Speed (µs): <span id="shutter-val">{{cfg.exposure_shutter_speed}}</span></label>
        <input type="range" name="exposure_shutter_speed" min="100" max="200000" step="100"
               value="{{cfg.exposure_shutter_speed}}" oninput="document.getElementById('shutter-val').textContent=this.value">
      </div>
      <div>
        <label>ISO: <span id="iso-val">{{cfg.exposure_iso}}</span></label>
        <input type="range" name="exposure_iso" min="100" max="3200" step="100"
               value="{{cfg.exposure_iso}}" oninput="document.getElementById('iso-val').textContent=this.value">
      </div>
    </div>
  </div>
  <div class="grid">
    <div>
      <label><input type="checkbox" name="luma_enabled" value="on" id="luma-toggle"
        {{'checked' if cfg.luma_target is not none}} onchange="document.getElementById('luma-slider').classList.toggle('hidden',!this.checked)">
        Luma Target</label>
      <div id="luma-slider" class="{{'hidden' if cfg.luma_target is none}}">
        <label>Target: <span id="luma-val">{{cfg.luma_target or 128}}</span></label>
        <input type="range" name="luma_target" min="0" max="255" value="{{cfg.luma_target or 128}}"
               oninput="document.getElementById('luma-val').textContent=this.value">
      </div>
    </div>
  </div>
  <button type="submit">Save Capture Settings</button>
</form>

<!-- ─── CURRENT SESSION ─── -->
<h2>🎞️ Current Session</h2>
<div class="card">
  <div class="stat-row">
    <div class="stat-box"><div class="stat" id="session-frames">{{ frames }}</div><div class="stat-label">Frames</div></div>
    <div class="stat-box"><div class="stat">{{ cfg.playback_fps }} fps</div><div class="stat-label">Playback FPS</div></div>
    <div class="stat-box"><div class="stat" id="session-duration">{{ "%.1f"|format(frames / cfg.playback_fps) if cfg.playback_fps > 0 else 0 }}s</div><div class="stat-label">Video Duration</div></div>
    <div class="stat-box"><div class="stat" id="session-id">{{ session_id }}</div><div class="stat-label">Session ID</div></div>
  </div>
  <div style="font-size:.85rem;color:#8b949e">Last capture: <span id="session-last-capture">{{ last_capture or "–" }}</span></div>
  <img src="/api/thumbnail" alt="Latest frame" class="thumb" id="latest-thumb" onerror="this.style.display='none'">
  <div class="notice-card">
    <div class="notice-title">New session preview</div>
    <div class="compact-list">
      <div><strong>{{ session_id }}</strong> will be archived with {{ frames }} frames.</div>
      <div>At {{ cfg.playback_fps }} fps, that archive will play for {{ "%.1f"|format(frames / cfg.playback_fps) if cfg.playback_fps > 0 else 0 }}s.</div>
      <div>The fresh capture session will begin as <strong>{{ next_session_preview }}</strong>.</div>
    </div>
  </div>
  <div style="margin-top:1rem" class="actions">
    <button class="btn-danger" type="button" onclick="confirmNewSession({{ frames }}, '{{ session_id }}', '{{ next_session_preview }}')">
      Start New Session</button>
    <form id="new-session-form" method="POST" action="/api/new_session" style="display:none"></form>
  </div>
</div>

<!-- ─── ARCHIVED SESSIONS ─── -->
{% if archives %}
<h2>📁 Archived Sessions</h2>
<div class="card">
<div class="table-wrap">
<table>
<thead><tr><th>Session</th><th>Frames</th><th>Duration @{{ cfg.playback_fps }}fps</th><th>Note / Label</th></tr></thead>
<tbody>
{% for a in archives %}
<tr><td>{{ a.name }}</td><td>{{ a.frames }}</td>
<td>{{ "%.1f"|format(a.frames / cfg.playback_fps) if cfg.playback_fps > 0 else "–" }}s</td>
<td>
  <form method="POST" action="/api/archive_note" class="archive-note-form">
    <input type="hidden" name="archive_name" value="{{ a.name }}">
    <div class="archive-note-row">
      <input type="text" name="note" value="{{ a.note|e }}" maxlength="80" placeholder="Optional note or label">
      <button type="submit">Save</button>
    </div>
  </form>
</td></tr>
{% endfor %}
</tbody></table></div></div>
{% endif %}

<h2>📈 Session Growth & Storage</h2>
<div class="card">
  <div class="mobile-stack">
    <div>
      <label>Recent frame counts</label>
      <div class="bar-chart">
        {% set max_growth = storage_projection.chart_max_frames if storage_projection.chart_max_frames > 0 else 1 %}
        {% for point in storage_projection.chart %}
        <div class="bar" style="height:{{ (point.frames / max_growth * 100) if max_growth > 0 else 0 }}%;flex:1">
          <span>{{ point.frames }}</span>
          {{ point.label }}
        </div>
        {% endfor %}
      </div>
      <div class="helper">Recent archived sessions plus the current “now” session.</div>
    </div>
    <div class="stack">
      <div class="stat-row">
        <div class="stat-box"><div class="stat">{{ storage_projection.average_frame_mb }}</div><div class="stat-label">Avg Frame MB</div></div>
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_frames }}</div><div class="stat-label">Frames Remaining</div></div>
      </div>
      <div class="stat-row">
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_hours }}</div><div class="stat-label">Hours Left</div></div>
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_days }}</div><div class="stat-label">Days Left</div></div>
      </div>
      <div class="helper">Estimate uses recent JPG sizes from the current session and the free space on the working stick.</div>
    </div>
  </div>
</div>

<!-- ─── PLAYBACK SETTINGS ─── -->
<h2>▶️ Playback Settings</h2>
<form method="POST" action="/api/config" class="card">
  <input type="hidden" name="section" value="playback">
  <div class="grid">
    <div>
      <label>Playback FPS</label>
      <select name="playback_fps">
        {% for v in fps_options %}
        <option value="{{v}}" {{'selected' if cfg.playback_fps==v}}>{{v}} fps</option>
        {% endfor %}
      </select>
    </div>
    <div>
      <label>Display Brightness: <span id="bright-val">{{cfg.display_brightness}}</span>%</label>
      <input type="range" name="display_brightness" min="0" max="100"
             value="{{cfg.display_brightness}}" oninput="document.getElementById('bright-val').textContent=this.value">
    </div>
  </div>

  <label style="margin-top:.5rem;font-size:.85rem;color:#8b949e">Video duration at each FPS ({{ frames }} frames):</label>
  <div class="bar-chart">
    {% set max_dur = fps_durations.values()|max if fps_durations.values()|list else 1 %}
    {% for fps, dur in fps_durations.items() %}
    <div class="bar" style="height:{{ (dur / max_dur * 100) if max_dur > 0 else 0 }}%;flex:1">
      <span>{{ dur }}s</span>
      {{ fps }}
    </div>
    {% endfor %}
  </div>

  <div class="actions">
    <button type="submit">Save Playback Settings</button>
    <button type="button" onclick="setBrightness(true)" style="background:#1f6feb">Brightness Test on Pi 2</button>
    <button type="button" onclick="restartPlayback()" style="background:#8957e5">Restart Playback</button>
    <button type="button" onclick="resyncNow()" style="background:#0969da">Resync Now</button>
  </div>
  <div class="helper">Remote actions run on Pi 2 over the local installation network.</div>
</form>

<!-- ─── ADMIN & NETWORK SETTINGS ─── -->
<h2>🔐 Admin & Network</h2>
<form method="POST" action="/api/config" class="card">
  <input type="hidden" name="section" value="network">
  <div class="grid">
    <div class="stack">
      <div>
        <label>Admin Password</label>
        <input type="password" name="admin_password" autocomplete="new-password" placeholder="Leave blank to keep the current password">
        <div class="helper">Only enter a value if you want to change the portal login.</div>
      </div>
      <div>
        <label>WiFi SSID</label>
        <input type="text" name="wifi_ssid" value="{{ cfg.wifi_ssid|e }}" maxlength="32" pattern=".*\S+.*" required>
      </div>
    </div>
    <div class="stack">
      <div>
        <label>WiFi Password</label>
        <input type="password" name="wifi_password" autocomplete="new-password" placeholder="Leave blank to keep the current WiFi password">
        <div class="helper">If you change this, Pi 1 must be reconfigured and Pi 2 must be updated to reconnect.</div>
      </div>
      <div class="helper" style="margin-top:1.6rem">Current access point: <strong>{{ cfg.wifi_ssid|e }}</strong> at <strong>192.168.50.1</strong>.</div>
    </div>
  </div>
  <div class="notice-card">
    <div class="status-summary">
      <span class="badge {{ wifi_status.badge_class }}">WiFi {{ wifi_status.label }}</span>
      <span class="badge {{ backup_status.badge_class }}">Backup {{ backup_status.label }}</span>
      <span class="badge {{ disk_status.badge_class }}">Storage {{ disk_status.label }}</span>
    </div>
    <div class="compact-list muted">
      <div>{{ wifi_status.message }}</div>
      <div>{{ backup_status.message }}</div>
      <div>{{ disk_status.message }}</div>
    </div>
  </div>
  <button type="submit">Save Admin / WiFi Settings</button>
</form>

<!-- ─── SYSTEM STATUS ─── -->
<h2>🖥️ System Status — Pi 1 (Camera)</h2>
<div class="card" id="pi1-status">
  <div class="stat-row">
    <div class="stat-box"><div class="stat" id="p1-uptime">{{ uptime }}</div><div class="stat-label">Uptime</div></div>
    <div class="stat-box"><div class="stat" id="p1-temp">{{ cpu_temp }}</div><div class="stat-label">CPU Temp</div></div>
  </div>
  <div class="status-summary">
    <span class="badge {{ backup_status.badge_class }}">Backup {{ backup_status.label }}</span>
    <span class="badge {{ disk_status.badge_class }}">Storage {{ disk_status.label }}</span>
  </div>
  <div class="stat-row">
    <div class="stat-box">
      <div class="stat" id="p1-disk-data">{{ disk_data.used_gb }}/{{ disk_data.total_gb }} GB</div>
      <div class="stat-label">Working Stick ({{ disk_data.percent }}%)</div>
    </div>
    <div class="stat-box">
      <div class="stat" id="p1-disk-backup">{{ disk_backup.used_gb }}/{{ disk_backup.total_gb }} GB</div>
      <div class="stat-label">Backup Stick ({{ disk_backup.percent }}%)</div>
    </div>
  </div>
  <div style="font-size:.85rem;color:#8b949e">Last backup: <span id="p1-backup">{{ last_backup or "–" }}</span></div>
</div>

<h2>📺 System Status — Pi 2 (Display)</h2>
<div class="card" id="pi2-status">
  <div id="pi2-offline" class="hidden" style="text-align:center;padding:1rem">
    <span class="badge badge-off">Pi 2 Offline</span>
  </div>
  <div id="pi2-online">
    <div class="stat-row">
      <div class="stat-box"><div class="stat" id="p2-frame">–</div><div class="stat-label">Current / Total Frames</div></div>
      <div class="stat-box"><div class="stat" id="p2-state">–</div><div class="stat-label">Playback State</div></div>
      <div class="stat-box"><div class="stat" id="p2-fps">–</div><div class="stat-label">FPS</div></div>
    </div>
    <div class="stat-row">
      <div class="stat-box"><div class="stat" id="p2-uptime">–</div><div class="stat-label">Uptime</div></div>
      <div class="stat-box"><div class="stat" id="p2-temp">–</div><div class="stat-label">CPU Temp</div></div>
      <div class="stat-box"><div class="stat" id="p2-disk">–</div><div class="stat-label">Disk Usage</div></div>
    </div>
    <div style="font-size:.85rem;color:#8b949e">Last sync: <span id="p2-sync">–</span></div>
    <div style="font-size:.85rem;color:#8b949e">WiFi: <span id="p2-wifi">–</span></div>
  </div>
</div>

<div style="text-align:center;padding:2rem 0;font-size:.75rem;color:#484f58">
  Timelapse Art Installation — Admin Portal
</div>

<script>
function toggleManual(){
  var m=document.getElementById('manual-controls');
  m.classList.toggle('show',document.getElementById('exposure-mode').value==='manual');
}

function confirmNewSession(frames,currentId,nextId){
  var message='Archive session "'+currentId+'" ('+frames+' frames) and start a fresh session as "'+nextId+'"?';
  if(confirm(message)){document.getElementById('new-session-form').submit()}
}

function refreshThumbnail(hasFrames){
  var thumb=document.getElementById('latest-thumb');
  if(!hasFrames){
    thumb.style.display='none';
    return;
  }
  thumb.style.display='';
  thumb.src='/api/thumbnail?ts='+Date.now();
}

function runPi2Action(path, successMessage){
  return fetch('http://192.168.50.20:5000'+path,{method:'POST'})
    .then(r=>r.json().catch(()=>({})).then(data=>({ok:r.ok,data:data})))
    .then(({ok,data})=>{
      if(!ok || data.error){throw new Error(data.error||'Pi 2 action failed')}
      if(successMessage){alert(successMessage)}
      return data;
    })
    .catch(err=>{alert(err.message||'Failed to reach Pi 2');throw err;});
}

function setBrightness(showSuccess){
  var v=document.querySelector('[name=display_brightness]').value;
  fetch('http://192.168.50.20:5000/display/brightness',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({value:parseInt(v)})
  })
    .then(r=>r.json().catch(()=>({})).then(data=>({ok:r.ok,data:data})))
    .then(({ok,data})=>{
      if(!ok || data.error){throw new Error(data.error||'Brightness update failed')}
      if(showSuccess){alert('Pi 2 brightness test sent.')}
    })
    .catch(err=>alert(err.message||'Failed to reach Pi 2'));
}

function restartPlayback(){
  runPi2Action('/playback/reload','Pi 2 playback is restarting.');
}

function resyncNow(){
  runPi2Action('/sync/now','Pi 2 sync service restarted for an immediate resync.');
}

// Poll Pi 2 status every 10 seconds
function pollPi2(){
  var ctrl=new AbortController();
  var tid=setTimeout(()=>ctrl.abort(),2000);
  fetch('http://192.168.50.20:5000/status',{signal:ctrl.signal})
    .then(r=>{clearTimeout(tid);return r.json()})
    .then(d=>{
      document.getElementById('pi2-offline').classList.add('hidden');
      document.getElementById('pi2-online').classList.remove('offline');
      document.getElementById('p2-frame').textContent=d.frame_current+' / '+d.frame_total;
      document.getElementById('p2-state').textContent=d.playback_state||'–';
      document.getElementById('p2-fps').textContent=(d.fps||'–')+' fps';
      document.getElementById('p2-uptime').textContent=d.uptime||'–';
      document.getElementById('p2-temp').textContent=d.cpu_temp||'–';
      document.getElementById('p2-disk').textContent=(d.disk_used_gb||0)+'/'+(d.disk_total_gb||0)+' GB';
      document.getElementById('p2-sync').textContent=d.last_sync_timestamp||'–';
      document.getElementById('p2-wifi').textContent=d.wifi_message||'–';
    })
    .catch(()=>{
      clearTimeout(tid);
      document.getElementById('pi2-offline').classList.remove('hidden');
      document.getElementById('pi2-online').classList.add('offline');
      ['p2-frame','p2-state','p2-fps','p2-uptime','p2-temp','p2-disk','p2-sync','p2-wifi'].forEach(id=>{
        document.getElementById(id).textContent='–';
      });
    });
}

// Poll Pi 1 status every 30s
function pollPi1(){
  fetch('/api/pi1_status').then(r=>r.json()).then(d=>{
    var fps={{ cfg.playback_fps|int }};
    document.getElementById('p1-uptime').textContent=d.uptime;
    document.getElementById('p1-temp').textContent=d.cpu_temp;
    document.getElementById('p1-disk-data').textContent=d.disk_data.used_gb+'/'+d.disk_data.total_gb+' GB';
    document.getElementById('p1-disk-backup').textContent=d.disk_backup.used_gb+'/'+d.disk_backup.total_gb+' GB';
    document.getElementById('p1-backup').textContent=d.last_backup||'–';
    document.getElementById('session-frames').textContent=d.frames;
    document.getElementById('session-id').textContent=d.session_id||'–';
    document.getElementById('session-last-capture').textContent=d.last_capture||'–';
    document.getElementById('session-duration').textContent=(fps>0?((d.frames||0)/fps).toFixed(1):'0.0')+'s';
    refreshThumbnail((d.frames||0)>0);
  }).catch(()=>{});
}

setInterval(pollPi2, 10000);
setInterval(pollPi1, 30000);
pollPi2();
pollPi1();
</script>
</body></html>"""

# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
