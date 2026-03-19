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
import heapq
import socket
import urllib.error
import urllib.request
import shutil
import subprocess
import time
import functools
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
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
app.config["PROPAGATE_EXCEPTIONS"] = False

CONFIG_PATH = "/data/config.json"
CURRENT_DIR = "/data/timelapse/current"
ARCHIVE_DIR = "/data/timelapse/archive"
LAST_CAPTURE = "/data/last_capture.txt"
LAST_BACKUP = "/data/last_backup.txt"
BACKUP_WARNING = "/data/backup_warning.flag"
DISK_WARNING = "/data/disk_warning.flag"
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
DNSMASQ_LEASES = "/var/lib/misc/dnsmasq.leases"
CAMERA_PREVIEW_PATH = "/data/camera_preview.jpg"
WPA_SUPPLICANT_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
NETWORKMANAGER_IGNORE_WLAN0 = "/etc/NetworkManager/conf.d/10-ignore-wlan0.conf"
DHCPCD_CONF = "/etc/dhcpcd.conf"
PI2_API_PORT = 5000
PI2_DEFAULT_IP = "192.168.50.20"
# rpicam-still expects --timeout in milliseconds.
CAMERA_PREVIEW_TIMEOUT_MS = "1500"
# fdisk commands: new DOS table, new primary partition 1, type 7 (exFAT/HPFS), write.
USB_FDISK_SCRIPT = "o\nn\np\n1\n\n\nt\n7\nw\n"
AP_DHCPCD_BLOCK = """# BEGIN TIMELAPSE AP
# Timelapse AP — static IP for wlan0
interface wlan0
    static ip_address=192.168.50.1/24
    nohook wpa_supplicant
# END TIMELAPSE AP
"""
AP_DHCPCD_BLOCK_PATTERNS = [
    r"\n?# BEGIN TIMELAPSE AP\n# Timelapse AP — static IP for wlan0\ninterface wlan0\n\s*static ip_address=192\.168\.50\.1/24\n\s*nohook wpa_supplicant\n# END TIMELAPSE AP\n?",
    r"\n?# Timelapse AP — static IP for wlan0\ninterface wlan0\n\s*static ip_address=192\.168\.50\.1/24\n\s*nohook wpa_supplicant\n?",
]
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
        "ffmpeg_video_backup_enabled": True,
        "admin_password": "changeme",
        "wifi_ssid": "timelapse-ap",
        "wifi_password": "changeme2",
        "uplink_wifi_ssid": "",
        "uplink_wifi_password": "",
        "display_type": "hdmi",
        "pi2_ip": "",
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


def validate_ip_or_hostname(value: str) -> bool:
    """Return True if *value* looks like a valid IPv4 address or hostname."""
    value = (value or "").strip()
    if not value:
        return True  # empty means "auto-discover"
    # IPv4 check
    parts = value.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            pass
    # Simple hostname check (letters, digits, hyphens, dots — no consecutive dots)
    return bool(re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$', value))


def run_command(cmd, timeout: int = 15, check: bool = False, input_text: str = None):
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def read_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def write_text(path: str, value: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(value)
    os.replace(tmp, path)


def get_interface_addresses(interface: str) -> dict:
    addresses = {"ipv4": [], "ipv6": []}
    for family, key in (("-4", "ipv4"), ("-6", "ipv6")):
        try:
            result = run_command(["ip", "-o", family, "addr", "show", "dev", interface], timeout=5)
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            token = "inet6" if family == "-6" else "inet"
            if token not in parts:
                continue
            address = parts[parts.index(token) + 1].split("/", 1)[0]
            if family == "-6" and address.startswith("fe80:"):
                continue
            addresses[key].append(address)
    return addresses


def get_current_wifi_ssid() -> str:
    try:
        result = run_command(["iwgetid", "-r"], timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def is_service_active(name: str) -> bool:
    try:
        result = run_command(["systemctl", "is-active", name], timeout=5)
        return result.returncode == 0 and result.stdout.strip() == "active"
    except Exception:
        return False


def get_systemd_service_state(name: str) -> dict:
    """Return systemd ActiveState and SubState for a service unit."""
    try:
        result = run_command(
            ["systemctl", "show", name, "--property=ActiveState,SubState,NRestarts"],
            timeout=5,
        )
        props = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        return props
    except Exception:
        return {"ActiveState": "unknown", "SubState": "unknown"}


def get_pi1_service_statuses() -> dict:
    """Check health of Pi 1 systemd services."""
    services = {}
    for name in ["capture.service", "portal.service"]:
        svc = get_systemd_service_state(name)
        active = svc.get("ActiveState", "unknown")
        sub = svc.get("SubState", "unknown")
        restarts = svc.get("NRestarts", "0")
        if active == "active":
            state = "ok"
            message = f"Running ({sub})"
        elif active in ("activating", "reloading"):
            state = "warning"
            message = f"Starting ({sub})"
        else:
            state = "error"
            message = f"Not running ({active}/{sub})"
        services[name.replace(".service", "")] = {
            "state": state,
            "message": message,
            "restarts": restarts,
        }
    return services


def get_pi1_network_mode() -> str:
    if is_service_active("hostapd"):
        return "ap"
    if get_current_wifi_ssid():
        return "wifi-client"
    return "unknown"


def get_access_addresses(current_host: str) -> list:
    seen = set()
    values = []
    for address in [current_host] + get_interface_addresses("wlan0")["ipv4"] + get_interface_addresses("eth0")["ipv4"]:
        address = (address or "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        values.append(address)
    return values


def get_pi1_network_summary(cfg: dict, current_host: str) -> dict:
    wlan0 = get_interface_addresses("wlan0")
    mode = get_pi1_network_mode()
    uplink_ssid = get_current_wifi_ssid()
    mode_label = {
        "ap": "AP Mode",
        "wifi-client": "Wi-Fi Client",
        "unknown": "Unknown",
    }.get(mode, "Unknown")
    message = f"Pi 1 is currently running in {mode_label.lower()}."
    if mode == "ap":
        ap_name = cfg.get("wifi_ssid", "timelapse-ap")
        if wlan0["ipv4"]:
            message = f'Pi 1 is serving the "{ap_name}" access point on {", ".join(wlan0["ipv4"])}.'
        else:
            message = f'Pi 1 should be serving the "{ap_name}" access point, but wlan0 does not currently show an IPv4 address.'
    elif mode == "wifi-client" and uplink_ssid:
        if wlan0["ipv4"]:
            message = f'Pi 1 is joined to Wi-Fi network "{uplink_ssid}" on {", ".join(wlan0["ipv4"])}.'
        else:
            message = f'Pi 1 is trying to join Wi-Fi network "{uplink_ssid}", but wlan0 does not currently show an IPv4 address.'
    return {
        "mode": mode,
        "mode_label": mode_label,
        "wlan0_ipv4": wlan0["ipv4"],
        "wlan0_ipv6": wlan0["ipv6"],
        "eth0_ipv4": get_interface_addresses("eth0")["ipv4"],
        "current_host": current_host,
        "access_hosts": get_access_addresses(current_host),
        "message": message,
        "uplink_ssid": uplink_ssid,
    }


def get_pi2_api_candidates() -> list:
    candidates = []
    seen = set()

    def add_candidate(host: str):
        host = (host or "").strip()
        if not host or host in seen:
            return
        seen.add(host)
        candidates.append(host)

    # 1. User-configured IP/hostname takes highest priority
    cfg_ip = read_config().get("pi2_ip", "").strip()
    if cfg_ip:
        add_candidate(cfg_ip)

    # 2. mDNS hostname (avahi)
    add_candidate("pi2-display.local")

    # 3. Derive from Pi 1's wlan0 subnet
    pi1_wlan = get_interface_addresses("wlan0")["ipv4"]
    if pi1_wlan:
        parts = pi1_wlan[0].split(".")
        if len(parts) == 4:
            add_candidate(".".join(parts[:3] + ["20"]))
    # 4. DHCP leases
    try:
        with open(DNSMASQ_LEASES) as f:
            for line in f:
                lease = line.split()
                if len(lease) >= 3:
                    add_candidate(lease[2])
    except Exception:
        pass
    # 5. ARP neighbor scan
    try:
        result = run_command(["ip", "neigh", "show", "dev", "wlan0"], timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts:
                    add_candidate(parts[0])
    except Exception:
        pass
    # 6. Hardcoded default
    add_candidate(PI2_DEFAULT_IP)
    return candidates


def _resolve_host_prefer_ipv4(host: str, port: int) -> list:
    """Resolve *host* via getaddrinfo, returning (addr, port, is_ipv6) tuples
    with IPv4 addresses listed first.  On Raspberry Pi OS Bookworm, Avahi/mDNS
    may return an IPv6 link-local address for ``pi2-display.local``; placing
    IPv4 first avoids 'connection refused' when the remote Flask server is
    only listening on IPv4 (older setups) while still working when the
    remote listens on dual-stack.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    ipv4 = []
    ipv6 = []
    seen = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if addr in seen:
            continue
        seen.add(addr)
        if family == socket.AF_INET:
            ipv4.append((addr, port, False))
        elif family == socket.AF_INET6:
            ipv6.append((addr, port, True))
    return ipv4 + ipv6


def proxy_pi2_request(path: str, method: str = "GET", payload: dict = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    last_error = "Pi 2 is unreachable."
    tried_hosts = []
    tried_addrs = []
    # Overall deadline keeps total proxy time under the JS polling timeout
    # (8 s) even when many candidate hosts are tried.
    deadline = time.time() + 6
    for host in get_pi2_api_candidates():
        if time.time() > deadline:
            break
        tried_hosts.append(host)
        # Resolve the hostname ourselves so we can try IPv4 first (the
        # status-api on Pi 2 listens on dual-stack, but legacy setups may
        # only have IPv4).  Also wrap bare IPv6 addresses in brackets for
        # the URL.
        resolved_targets = _resolve_host_prefer_ipv4(host, PI2_API_PORT)
        if not resolved_targets:
            resolved_targets = [(host, PI2_API_PORT, False)]
        for addr, port, is_ipv6 in resolved_targets:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            tried_addrs.append(addr)
            # Wrap raw IPv6 addresses in brackets for HTTP URLs
            url_host = f"[{addr}]" if is_ipv6 else addr
            url = f"http://{url_host}:{port}{path}"
            per_host_timeout = min(3, max(0.5, remaining))
            try:
                request_obj = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(request_obj, timeout=per_host_timeout) as response:
                    raw = response.read().decode() or "{}"
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        data.setdefault("pi2_host", host)
                    return data, response.status
            except urllib.error.HTTPError as exc:
                try:
                    err_body = exc.read().decode()
                    err_data = json.loads(err_body)
                    if isinstance(err_data, dict):
                        err_data.setdefault("pi2_host", host)
                    return err_data, exc.code
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    last_error = str(exc)
            except Exception as exc:
                last_error = str(exc)
    log.debug("proxy_pi2_request failed for %s — hosts %s, addrs %s — %s", path, tried_hosts, tried_addrs, last_error)
    return {"error": last_error, "tried_hosts": tried_hosts}, 502


def get_rpicam_status() -> dict:
    binary = shutil.which("rpicam-still")
    if not binary:
        return build_status(
            "error",
            "Missing",
            "rpicam-still is not installed on Pi 1. Pi 1 setup installs it in step 1 before the AP is created.",
        )
    preview_exists = os.path.isfile(CAMERA_PREVIEW_PATH)
    message = "rpicam-still is installed on Pi 1 and ready for a live preview capture."
    if preview_exists:
        message = "rpicam-still is installed. The most recent preview image is shown below."
    status = build_status("ok", "Ready", message)
    status["preview_available"] = preview_exists
    status["preview_timestamp"] = int(os.path.getmtime(CAMERA_PREVIEW_PATH)) if preview_exists else 0
    return status


def capture_camera_preview():
    binary = shutil.which("rpicam-still")
    if not binary:
        raise RuntimeError("rpicam-still is not installed on Pi 1 yet.")
    tmp_path = CAMERA_PREVIEW_PATH + ".tmp"
    cmd = [
        binary,
        "--nopreview",
        "--timeout", CAMERA_PREVIEW_TIMEOUT_MS,
        "--width", "1280",
        "--height", "720",
        "--immediate",
        "-o", tmp_path,
    ]
    result = run_command(cmd, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "rpicam-still preview failed")
    os.replace(tmp_path, CAMERA_PREVIEW_PATH)


def update_hostapd_credentials(cfg: dict):
    content = read_text(HOSTAPD_CONF)
    if not content:
        return
    ssid = cfg.get("wifi_ssid", "timelapse-ap")
    password = cfg.get("wifi_password", "changeme2")
    content = re.sub(r"^ssid=.*$", f"ssid={ssid}", content, flags=re.MULTILINE)
    content = re.sub(r"^wpa_passphrase=.*$", f"wpa_passphrase={password}", content, flags=re.MULTILINE)
    write_text(HOSTAPD_CONF, content)


def set_ap_dhcpcd_enabled(enabled: bool):
    content = read_text(DHCPCD_CONF)
    for pattern in AP_DHCPCD_BLOCK_PATTERNS:
        content = re.sub(pattern, "\n", content, flags=re.MULTILINE)
    if enabled:
        content = content.rstrip() + "\n\n" + AP_DHCPCD_BLOCK
    write_text(DHCPCD_CONF, content.strip() + "\n")


def ensure_networkmanager_ignore_wlan0(ignore: bool):
    os.makedirs(os.path.dirname(NETWORKMANAGER_IGNORE_WLAN0), exist_ok=True)
    if ignore:
        write_text(NETWORKMANAGER_IGNORE_WLAN0, "[keyfile]\nunmanaged-devices=interface-name:wlan0\n")
    else:
        try:
            os.remove(NETWORKMANAGER_IGNORE_WLAN0)
        except FileNotFoundError:
            pass


def write_uplink_wifi_config(cfg: dict):
    uplink_ssid = (cfg.get("uplink_wifi_ssid") or "").strip()
    uplink_password = cfg.get("uplink_wifi_password") or ""
    if not uplink_ssid:
        raise ValueError("Save the upstream Wi-Fi SSID before switching Pi 1 out of AP mode.")
    if len(uplink_password) < 8:
        raise ValueError("The upstream Wi-Fi password must be at least 8 characters long.")
    os.makedirs(os.path.dirname(WPA_SUPPLICANT_CONF), exist_ok=True)
    write_text(
        WPA_SUPPLICANT_CONF,
        "\n".join(
            [
                "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
                "update_config=1",
                "country=US",
                "",
                "network={",
                f'    ssid="{uplink_ssid}"',
                f'    psk="{uplink_password}"',
                "    key_mgmt=WPA-PSK",
                "    priority=1",
                "}",
                "",
            ]
        ),
    )
    if shutil.which("nmcli"):
        run_command(["nmcli", "con", "delete", "timelapse-uplink"], timeout=10)
        run_command(
            [
                "nmcli",
                "con",
                "add",
                "con-name",
                "timelapse-uplink",
                "type",
                "wifi",
                "ifname",
                "wlan0",
                "ssid",
                uplink_ssid,
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                uplink_password,
                "ipv4.method",
                "auto",
                "connection.autoconnect",
                "yes",
            ],
            timeout=15,
        )


def switch_pi1_network_mode(mode: str, cfg: dict):
    if mode not in {"ap", "wifi-client"}:
        raise ValueError("Unsupported Pi 1 network mode requested.")
    if mode == "wifi-client":
        write_uplink_wifi_config(cfg)
        set_ap_dhcpcd_enabled(False)
        ensure_networkmanager_ignore_wlan0(False)
        run_command(["systemctl", "stop", "hostapd", "dnsmasq"], timeout=15)
        run_command(["systemctl", "enable", "--now", "wpa_supplicant"], timeout=15)
        if shutil.which("nmcli"):
            run_command(["systemctl", "restart", "NetworkManager"], timeout=20)
            run_command(["nmcli", "con", "up", "timelapse-uplink"], timeout=20)
        run_command(["systemctl", "restart", "dhcpcd"], timeout=20)
    else:
        update_hostapd_credentials(cfg)
        set_ap_dhcpcd_enabled(True)
        ensure_networkmanager_ignore_wlan0(True)
        run_command(["systemctl", "disable", "--now", "wpa_supplicant"], timeout=15)
        if shutil.which("nmcli"):
            run_command(["systemctl", "restart", "NetworkManager"], timeout=20)
        run_command(["systemctl", "restart", "dhcpcd"], timeout=20)
        run_command(["systemctl", "restart", "hostapd", "dnsmasq"], timeout=20)
    time.sleep(2)


def create_timelapse_directories():
    os.makedirs("/data/timelapse/current", exist_ok=True)
    os.makedirs("/data/timelapse/archive", exist_ok=True)
    os.makedirs("/data/renders", exist_ok=True)
    os.makedirs("/backup/timelapse/current", exist_ok=True)
    os.makedirs("/backup/timelapse/archive", exist_ok=True)
    run_command(["chown", "-R", "1000:1000", "/data", "/backup"], timeout=20)


def list_usb_devices() -> list:
    result = run_command(["lsblk", "-bdnpo", "NAME,TRAN,SIZE"], timeout=10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "lsblk failed")
    devices = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "usb":
            continue
        try:
            devices.append({"path": parts[0], "size": int(parts[2])})
        except ValueError:
            continue
    return sorted(devices, key=lambda device: device["size"], reverse=True)


def get_device_partition(dev: str) -> str:
    result = run_command(["lsblk", "-lnpo", "NAME,TYPE", dev], timeout=10, check=True)
    partitions = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "part":
            partitions.append(parts[0])
    if not partitions:
        raise RuntimeError(f"Partitioning {dev} did not create a usable partition.")
    return partitions[0]


def update_fstab_mounts(working_uuid: str, backup_uuid: str):
    current_lines = []
    for line in read_text("/etc/fstab").splitlines():
        if "/data " in line or "/backup " in line:
            continue
        current_lines.append(line)
    current_lines.append(f"UUID={working_uuid}  /data    exfat  defaults,nofail,uid=1000,gid=1000,dmask=0022,fmask=0133  0  0")
    current_lines.append(f"UUID={backup_uuid}   /backup  exfat  defaults,nofail,uid=1000,gid=1000,dmask=0022,fmask=0133  0  0")
    write_text("/etc/fstab", "\n".join(line for line in current_lines if line.strip()) + "\n")


def format_and_mount_usb_sticks():
    if os.path.ismount("/data") and os.path.ismount("/backup"):
        create_timelapse_directories()
        return "USB sticks are already mounted at /data and /backup."
    usb_devices = list_usb_devices()
    if len(usb_devices) < 2:
        raise RuntimeError("Two USB sticks are required before formatting storage from the dashboard.")
    working_dev = usb_devices[0]["path"]
    backup_dev = usb_devices[1]["path"]
    for dev in [working_dev, backup_dev]:
        run_command(["wipefs", "-a", dev], timeout=20, check=True)
        fdisk_result = run_command(["fdisk", dev], timeout=20, input_text=USB_FDISK_SCRIPT)
        if fdisk_result.returncode != 0:
            raise RuntimeError(fdisk_result.stderr.strip() or f"fdisk failed while preparing {dev}.")
        time.sleep(1)
        partition = get_device_partition(dev)
        run_command(["mkfs.exfat", "-n", "TIMELAPSE", partition], timeout=60, check=True)
    time.sleep(1)
    working_part = get_device_partition(working_dev)
    backup_part = get_device_partition(backup_dev)
    working_uuid = run_command(["blkid", "-s", "UUID", "-o", "value", working_part], timeout=10, check=True).stdout.strip()
    backup_uuid = run_command(["blkid", "-s", "UUID", "-o", "value", backup_part], timeout=10, check=True).stdout.strip()
    update_fstab_mounts(working_uuid, backup_uuid)
    os.makedirs("/data", exist_ok=True)
    os.makedirs("/backup", exist_ok=True)
    run_command(["mount", "-a"], timeout=20, check=True)
    create_timelapse_directories()
    return f"USB setup complete. Working storage is {working_dev}; backup storage is {backup_dev}."


# ── auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 403
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
            "Pi 1 hostapd access-point settings have not been written yet.",
        )
    if applied_ssid != configured_ssid or applied_password != configured_password:
        return build_status(
            "warning",
            "Pending",
            f'Pi 1 hostapd is still serving "{applied_ssid}", while the saved dashboard settings expect "{configured_ssid}".',
        )
    return build_status("ok", "Applied", f'Pi 1 is already serving the "{configured_ssid}" access point.')


def get_backup_status(last_backup: str) -> dict:
    if not os.path.ismount("/backup"):
        return build_status("error", "Missing", "Backup stick is not mounted at /backup.")
    try:
        if os.stat("/backup").st_dev == os.stat("/").st_dev:
            return build_status("error", "Wrong disk", "Backup path points to the same device as the SD card.")
    except Exception:
        log.warning("Backup device health check failed", exc_info=True)
        return build_status("error", "Unavailable", "Backup device health could not be verified.")
    if not last_backup:
        return build_status("warning", "Waiting", "Backup stick is ready, but no successful backup has been recorded yet.")
    try:
        last_backup_dt = datetime.fromisoformat(last_backup)
    except Exception:
        log.warning("Backup timestamp %r could not be parsed", last_backup)
        return build_status("warning", "Unknown", f'Backup timestamp "{last_backup}" could not be parsed.')
    try:
        if last_backup_dt.tzinfo is None:
            last_backup_dt = last_backup_dt.replace(tzinfo=timezone.utc)
        backup_age = datetime.now(timezone.utc) - last_backup_dt
    except TypeError:
        log.warning("Backup age calculation failed due to tz mismatch", exc_info=True)
        backup_age = None
    if backup_age is None:
        return build_status("warning", "Unknown", f"Backup timestamp recorded but age could not be calculated.")
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
    frames = heapq.nlargest(sample_size, glob.glob(os.path.join(directory, "frame_*.jpg")))
    if not frames:
        return 0
    sizes = []
    for frame in frames:
        try:
            sizes.append(os.path.getsize(frame))
        except OSError:
            continue
    return int(sum(sizes) / len(sizes)) if sizes else 0


def build_storage_projection(frames: int, archives: list, disk_data: dict, capture_interval_minutes: int) -> dict:
    average_frame_bytes = estimate_frame_size(CURRENT_DIR)
    remaining_frames = int(disk_data.get("free_bytes", 0) / average_frame_bytes) if average_frame_bytes else None
    remaining_hours = round((remaining_frames * capture_interval_minutes) / 60.0, 1) if remaining_frames is not None else None
    remaining_days = round(remaining_hours / 24.0, 1) if remaining_hours is not None else None
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
        "estimate_message": (
            "Estimate uses recent JPG sizes from the current session and the free space on the working stick."
            if average_frame_bytes
            else "Storage estimate becomes available after a few frames have been captured."
        ),
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
    _safe_status = build_status("off", "–", "Status unavailable.")

    try:
        frames = count_frames(CURRENT_DIR)
    except Exception:
        log.exception("Failed to count frames")
        frames = 0

    try:
        last_cap = read_timestamp_file(LAST_CAPTURE)
    except Exception:
        log.warning("Failed to read last capture timestamp")
        last_cap = ""

    try:
        last_bak = read_timestamp_file(LAST_BACKUP)
    except Exception:
        log.warning("Failed to read last backup timestamp")
        last_bak = ""

    try:
        session_id = get_session_id()
    except Exception:
        log.warning("Failed to read session ID")
        session_id = "unknown"

    try:
        archives = list_archives()
    except Exception:
        log.exception("Failed to list archives")
        archives = []

    try:
        disk_data = get_disk_usage("/data")
    except Exception:
        log.exception("Failed to get /data disk usage")
        disk_data = {
            "path": "/data", "total_gb": 0, "used_gb": 0, "free_gb": 0,
            "total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent": 0,
        }

    try:
        disk_backup = get_disk_usage("/backup")
    except Exception:
        log.exception("Failed to get /backup disk usage")
        disk_backup = {
            "path": "/backup", "total_gb": 0, "used_gb": 0, "free_gb": 0,
            "total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent": 0,
        }

    try:
        backup_status = get_backup_status(last_bak)
    except Exception:
        log.exception("Backup status check failed")
        backup_status = _safe_status

    try:
        disk_status = get_disk_status(disk_data)
    except Exception:
        log.exception("Disk status check failed")
        disk_status = _safe_status

    try:
        wifi_status = get_pi1_wifi_status(cfg)
    except Exception:
        log.exception("WiFi status check failed")
        wifi_status = _safe_status

    try:
        current_host = (request.host.split(":", 1)[0] or "").strip("[]")
        network_info = get_pi1_network_summary(cfg, current_host)
    except Exception:
        log.exception("Network summary failed")
        current_host = "localhost"
        network_info = {
            "mode": "unknown", "mode_label": "Unknown",
            "wlan0_ipv4": [], "wlan0_ipv6": [], "eth0_ipv4": [],
            "current_host": current_host, "access_hosts": [current_host],
            "message": "Network information unavailable.", "uplink_ssid": "",
        }

    try:
        camera_status = get_rpicam_status()
    except Exception:
        log.exception("Camera status check failed")
        camera_status = build_status("off", "–", "Camera status unavailable.")
        camera_status["preview_available"] = False
        camera_status["preview_timestamp"] = 0

    try:
        storage_projection = build_storage_projection(
            frames, archives, disk_data, cfg["capture_interval_minutes"],
        )
    except Exception:
        log.exception("Storage projection failed")
        storage_projection = {
            "average_frame_mb": 0, "remaining_frames": None,
            "remaining_hours": None, "remaining_days": None,
            "estimate_message": "Storage estimate is temporarily unavailable.",
            "chart": [{"label": "now", "frames": frames}], "chart_max_frames": max(frames, 1),
        }

    try:
        next_preview = next_session_id()
    except Exception:
        log.warning("Failed to generate next session ID")
        next_preview = "unknown"

    fps_options = [6, 12, 18, 25, 30, 48, 60, 120]
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
        network_info=network_info,
        camera_status=camera_status,
        storage_projection=storage_projection,
        next_session_preview=next_preview,
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
            minimum=1,
            maximum=1440,
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
    if "ffmpeg_video_backup_present" in data:
        video_backup_enabled = data.get("ffmpeg_video_backup_enabled") == "on"
        cfg["ffmpeg_video_backup_enabled"] = video_backup_enabled
        if video_backup_enabled != old_cfg.get("ffmpeg_video_backup_enabled", True):
            state = "enabled" if video_backup_enabled else "disabled"
            messages.append(("success", f"Optional FFmpeg video backups {state}."))

    # Admin settings
    if "admin_password" in data and data["admin_password"]:
        cfg["admin_password"] = data["admin_password"]
        if cfg["admin_password"] != old_cfg.get("admin_password"):
            messages.append(("success", "Admin password updated."))
    if "pi2_ip" in data:
        pi2_ip = data["pi2_ip"].strip()
        if pi2_ip and not validate_ip_or_hostname(pi2_ip):
            messages.append(("warning", "Pi 2 address must be a valid IPv4 address or hostname — previous value kept."))
        else:
            cfg["pi2_ip"] = pi2_ip
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
    if "uplink_wifi_ssid" in data:
        uplink_ssid, uplink_error = sanitize_ssid(data["uplink_wifi_ssid"], old_cfg.get("uplink_wifi_ssid", ""))
        if data["uplink_wifi_ssid"].strip() == "":
            cfg["uplink_wifi_ssid"] = ""
        elif uplink_error:
            messages.append(("warning", uplink_error.replace("WiFi SSID", "Upstream WiFi SSID")))
        else:
            cfg["uplink_wifi_ssid"] = uplink_ssid
    if "uplink_wifi_password" in data:
        uplink_password = data["uplink_wifi_password"]
        if uplink_password:
            if len(uplink_password) < 8:
                messages.append(("warning", "Upstream WiFi password must be at least 8 characters, so the previous value was kept."))
            else:
                cfg["uplink_wifi_password"] = uplink_password
    if wifi_changed:
        try:
            update_hostapd_credentials(cfg)
            if get_pi1_network_mode() == "ap":
                run_command(["systemctl", "restart", "hostapd"], timeout=15)
        except Exception:
            log.exception("Failed to apply hostapd credential changes")
            messages.append(("error", "WiFi credential update failed — check portal logs."))
        else:
            messages.append(("warning", "Access-point settings saved on Pi 1. Update Pi 2 too if it needs to reconnect with the new AP password."))

    try:
        write_config(cfg)
    except Exception:
        log.exception("Failed to write config")
        flash("Could not save settings — check portal logs.", "error")
        return redirect(url_for("dashboard"))
    if not messages:
        messages.append(("success", f"{section.capitalize()} settings saved."))
    for category, message in messages:
        flash(message, category)
    return redirect(url_for("dashboard"))


@app.route("/api/network_mode", methods=["POST"])
@login_required
def network_mode():
    cfg = read_config()
    mode = request.form.get("mode", "")
    try:
        switch_pi1_network_mode(mode, cfg)
    except ValueError as exc:
        flash(str(exc), "warning")
    except Exception as exc:
        flash(f"Could not switch Pi 1 network mode: {exc}", "error")
    else:
        label = "Wi-Fi client" if mode == "wifi-client" else "access point"
        flash(f"Pi 1 switched to {label} mode.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/camera_preview", methods=["POST"])
@login_required
def camera_preview():
    try:
        capture_camera_preview()
    except Exception as exc:
        flash(f"Camera preview failed: {exc}", "error")
    else:
        flash("Captured a fresh camera preview on Pi 1.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/camera_preview_image")
@login_required
def camera_preview_image():
    if not os.path.isfile(CAMERA_PREVIEW_PATH):
        abort(404)
    return send_file(CAMERA_PREVIEW_PATH, mimetype="image/jpeg")


@app.route("/api/setup_usb", methods=["POST"])
@login_required
def setup_usb():
    try:
        message = format_and_mount_usb_sticks()
    except Exception as exc:
        flash(f"USB setup failed: {exc}", "error")
    else:
        flash(message, "success")
    return redirect(url_for("dashboard"))


@app.route("/api/pi2/status")
@login_required
def proxy_pi2_status():
    data, status = proxy_pi2_request("/status")
    return jsonify(data), status


@app.route("/api/pi2/playback/reload", methods=["POST"])
@login_required
def proxy_pi2_reload():
    data, status = proxy_pi2_request("/playback/reload", method="POST")
    return jsonify(data), status


@app.route("/api/pi2/sync/now", methods=["POST"])
@login_required
def proxy_pi2_sync():
    data, status = proxy_pi2_request("/sync/now", method="POST")
    return jsonify(data), status


@app.route("/api/pi2/display/brightness", methods=["POST"])
@login_required
def proxy_pi2_brightness():
    payload = request.get_json(force=True, silent=True) or {}
    data, status = proxy_pi2_request("/display/brightness", method="POST", payload=payload)
    return jsonify(data), status


@app.route("/api/new_session", methods=["POST"])
@login_required
def new_session():
    """Archive current session and start fresh."""
    try:
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
        try:
            subprocess.run(["systemctl", "restart", "capture.service"], capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("Timed out restarting capture.service after new session")
        except Exception:
            log.warning("Failed to restart capture.service after new session", exc_info=True)

        flash("Started a new session and archived the previous frames.", "success")
    except Exception as exc:
        log.exception("New session creation failed")
        flash(f"New session failed: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.route("/api/archive_note", methods=["POST"])
@login_required
def archive_note():
    try:
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
    except Exception as exc:
        log.exception("Archive note update failed")
        flash(f"Could not update archive label: {exc}", "error")
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
    try:
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
            "services": get_pi1_service_statuses(),
        })
    except Exception:
        log.exception("Pi 1 status poll failed")
        return jsonify({"error": "Status temporarily unavailable"}), 500


@app.route("/generate_204")
@app.route("/hotspot-detect.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_portal_detect():
    """Captive portal detection endpoints — redirect to dashboard."""
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ── health & error handlers ──────────────────────────────────────────────────


@app.route("/health")
def health():
    """Lightweight health-check endpoint for monitoring."""
    checks = {}
    try:
        checks["frames"] = count_frames(CURRENT_DIR)
    except Exception:
        checks["frames"] = None
    try:
        checks["last_capture"] = read_timestamp_file(LAST_CAPTURE) or None
    except Exception:
        checks["last_capture"] = None
    try:
        last_bak = read_timestamp_file(LAST_BACKUP)
        backup_info = get_backup_status(last_bak)
        checks["backup_ok"] = backup_info.get("level") == "ok"
        checks["backup_label"] = backup_info.get("label", "Unknown")
    except Exception:
        checks["backup_ok"] = False
        checks["backup_label"] = "Error"
    try:
        checks["data_mounted"] = os.path.ismount("/data")
    except Exception:
        checks["data_mounted"] = False
    try:
        checks["backup_mounted"] = os.path.ismount("/backup")
    except Exception:
        checks["backup_mounted"] = False
    all_ok = (
        checks.get("data_mounted") is True
        and checks.get("backup_mounted") is True
        and checks.get("backup_ok") is True
    )
    return jsonify({"status": "ok" if all_ok else "degraded", **checks})


ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timelapse Admin — Error</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2rem;width:100%;max-width:480px;text-align:center}
h1{font-size:1.4rem;margin-bottom:1rem;color:#f85149}
p{margin-bottom:1rem;color:#8b949e;font-size:.95rem}
a{color:#58a6ff;text-decoration:none;font-weight:600}
a:hover{text-decoration:underline}
code{background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:.15rem .4rem;font-size:.85rem}
</style></head>
<body><div class="card">
<h1>{{ title }}</h1>
<p>{{ message }}</p>
<a href="/">← Back to Dashboard</a>
</div></body></html>"""


@app.errorhandler(404)
def handle_404(exc):
    return render_template_string(
        ERROR_HTML,
        title="Page Not Found",
        message="The page you requested does not exist.",
    ), 404


@app.errorhandler(500)
def handle_500(exc):
    log.exception("Unhandled server error: %s", exc)
    return render_template_string(
        ERROR_HTML,
        title="Something Went Wrong",
        message="An unexpected error occurred. Check the portal logs for details.",
    ), 500


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
      <label for="capture-interval">Capture Interval</label>
      <input id="capture-interval" type="number" name="capture_interval_minutes" min="1" max="1440" step="1" value="{{ cfg.capture_interval_minutes }}" aria-describedby="capture-interval-help">
      <div class="helper" id="capture-interval-help">Enter any whole number from 1 to 1440 minutes.</div>
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
    <button class="btn-danger" type="button" onclick='confirmNewSession({{ frames|tojson }}, {{ session_id|tojson }}, {{ next_session_preview|tojson }})'>
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
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_frames if storage_projection.remaining_frames is not none else "–" }}</div><div class="stat-label">Frames Remaining</div></div>
      </div>
      <div class="stat-row">
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_hours if storage_projection.remaining_hours is not none else "–" }}</div><div class="stat-label">Hours Left</div></div>
        <div class="stat-box"><div class="stat">{{ storage_projection.remaining_days if storage_projection.remaining_days is not none else "–" }}</div><div class="stat-label">Days Left</div></div>
      </div>
      <div class="helper">{{ storage_projection.estimate_message }}</div>
    </div>
  </div>
</div>

<!-- ─── PLAYBACK SETTINGS ─── -->
<h2>▶️ Playback Settings</h2>
<form method="POST" action="/api/config" class="card">
  <input type="hidden" name="section" value="playback">
  <input type="hidden" name="ffmpeg_video_backup_present" value="1">
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

  <label class="notice-card" style="display:flex;gap:.75rem;align-items:flex-start;margin-top:1rem">
    <input type="checkbox" name="ffmpeg_video_backup_enabled" value="on" {{ 'checked' if cfg.ffmpeg_video_backup_enabled else '' }} style="margin-top:.25rem">
    <span>
      <strong>Keep Pi 2's optional FFmpeg video backup job enabled</strong>
      <span class="helper" style="display:block;margin-top:.35rem">
        This controls the twice-daily archival MP4 render on Pi 2. Turn it off if morning startup and live playback are more important than keeping optional video backups. Live playback still uses FFmpeg automatically when you choose a playback rate above 30 fps.
      </span>
    </span>
  </label>

  <div class="actions">
    <button type="submit">Save Playback Settings</button>
    <button type="button" onclick="setBrightness(true)" style="background:#1f6feb">Brightness Test on Pi 2</button>
    <button type="button" onclick="restartPlayback()" style="background:#8957e5">Restart Playback</button>
    <button type="button" onclick="resyncNow()" style="background:#0969da">Resync Now</button>
  </div>
  <div class="helper">Remote actions run on Pi 2 through Pi 1, so they keep working even if Pi 2 is not using the default hard-coded address.</div>
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
        <label for="pi2-ip-input">Pi 2 IP Address</label>
        <input type="text" id="pi2-ip-input" name="pi2_ip" value="{{ cfg.pi2_ip|e }}" maxlength="63" placeholder="Auto-discover (leave blank) or e.g. 192.168.50.20">
        <div class="helper">Set Pi 2's IP address or hostname manually. Leave blank to auto-discover via mDNS, DHCP leases, and ARP scan.</div>
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
        <div class="helper">If you change this, Pi 2 must also be updated to reconnect to the AP.</div>
      </div>
      <div>
        <label>Upstream WiFi SSID</label>
        <input type="text" name="uplink_wifi_ssid" value="{{ cfg.uplink_wifi_ssid|e }}" maxlength="32" placeholder="Optional internet WiFi for updates">
      </div>
      <div>
        <label>Upstream WiFi Password</label>
        <input type="password" name="uplink_wifi_password" autocomplete="new-password" placeholder="Leave blank to keep the current upstream password">
        <div class="helper">Save these credentials before switching Pi 1 from AP mode back to a normal WiFi network for updates.</div>
      </div>
    </div>
  </div>
  <div class="helper">
    Dashboard access right now:
    {% for host in network_info.access_hosts %}
      <strong>http://{{ host }}</strong>{% if not loop.last %}, {% endif %}
    {% endfor %}
    {% if not network_info.access_hosts %}<strong>localhost</strong>{% endif %}
  </div>
  <div class="notice-card">
    <div class="status-summary">
      <span class="badge {{ wifi_status.badge_class }}">WiFi {{ wifi_status.label }}</span>
      <span class="badge {{ 'badge-ok' if network_info.mode == 'ap' else 'badge-warn' if network_info.mode == 'wifi-client' else 'badge-off' }}">Pi 1 {{ network_info.mode_label }}</span>
      <span class="badge {{ backup_status.badge_class }}">Backup {{ backup_status.label }}</span>
      <span class="badge {{ disk_status.badge_class }}">Storage {{ disk_status.label }}</span>
    </div>
    <div class="compact-list muted">
      <div>{{ wifi_status.message }}</div>
      <div>{{ network_info.message }}</div>
      {% if network_info.wlan0_ipv6 %}
      <div>Pi 1 IPv6 on wlan0: {{ network_info.wlan0_ipv6|join(', ') }}</div>
      {% endif %}
      <div>{{ backup_status.message }}</div>
      <div>{{ disk_status.message }}</div>
    </div>
  </div>
  <div class="actions" style="margin-top:.85rem">
    <button type="submit">Save Admin / WiFi Settings</button>
    <button type="button" onclick="switchPi1Mode('wifi-client')" style="background:#d29922;color:#000">Switch Pi 1 to WiFi Client</button>
    <button type="button" onclick="switchPi1Mode('ap')" style="background:#1f6feb">Return Pi 1 to AP Mode</button>
  </div>
</form>

<h2>🧰 Setup & Maintenance</h2>
<div class="card">
  <div class="grid">
    <div class="stack">
      <div class="notice-card">
        <div class="notice-title">Camera preview</div>
        <div class="status-summary">
          <span class="badge {{ camera_status.badge_class }}">Camera {{ camera_status.label }}</span>
        </div>
        <div class="compact-list muted">
          <div>{{ camera_status.message }}</div>
          <div>Pi 1 setup installs camera dependencies first, while internet is still available, before the AP services are configured.</div>
        </div>
        <div class="actions" style="margin-top:.85rem">
          <form method="POST" action="/api/camera_preview" style="width:100%">
            <button type="submit">Capture Preview on Pi 1</button>
          </form>
        </div>
        {% if camera_status.preview_available %}
        <img src="/api/camera_preview_image?ts={{ camera_status.preview_timestamp }}" alt="Camera preview" class="thumb">
        {% endif %}
      </div>
    </div>
    <div class="stack">
      <div class="notice-card">
        <div class="notice-title">USB storage setup</div>
        <div class="status-summary">
          <span class="badge {{ disk_status.badge_class }}">Working USB {{ disk_status.label }}</span>
          <span class="badge {{ backup_status.badge_class }}">Backup USB {{ backup_status.label }}</span>
        </div>
        <div class="compact-list muted">
          <div>If setup started before both USB sticks were connected, you can finish the storage setup from here later.</div>
          <div>This formats the two largest USB drives as exFAT and mounts them as <strong>/data</strong> and <strong>/backup</strong>.</div>
        </div>
        <div class="warning-banner" role="alert" style="margin-top:.85rem">Formatting USB storage erases both selected drives. Only run this after confirming the correct sticks are connected.</div>
        <div class="actions" style="margin-top:.85rem">
          <form method="POST" action="/api/setup_usb" onsubmit="return confirm('Format the two largest USB drives as /data and /backup? This erases them.');" style="width:100%">
            <button type="submit" class="btn-danger">Format / Mount USB Sticks</button>
          </form>
        </div>
      </div>
    </div>
  </div>
</div>

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
  <div class="status-summary" id="p1-services">
    <span class="badge badge-off" id="p1-svc-capture">Capture –</span>
    <span class="badge badge-off" id="p1-svc-portal">Portal –</span>
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
    <div id="pi2-offline-hint" style="font-size:.8rem;color:#8b949e;margin-top:.5rem">
      Could not reach Pi 2 status API. {% if cfg.pi2_ip %}Configured address: <strong>{{ cfg.pi2_ip|e }}</strong>.{% else %}No address configured — using auto-discovery.{% endif %}
      Set the IP in <em>Admin & Network</em> if Pi 2 has a different address.
    </div>
  </div>
  <div id="pi2-online">
    <div class="stat-row">
      <div class="stat-box"><div class="stat" id="p2-frame">–</div><div class="stat-label">Current / Total Frames</div></div>
      <div class="stat-box"><div class="stat" id="p2-state">–</div><div class="stat-label">Playback State</div></div>
      <div class="stat-box"><div class="stat" id="p2-fps">–</div><div class="stat-label">FPS</div></div>
    </div>
    <div class="status-summary" id="p2-services">
      <span class="badge badge-off" id="p2-svc-sync">Sync –</span>
      <span class="badge badge-off" id="p2-svc-playback">Playback –</span>
      <span class="badge badge-off" id="p2-svc-status_api">API –</span>
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

function parsePi2Response(response){
  return response.json().catch(()=>({})).then(data=>({ok:response.ok,data:data}));
}

function svcBadgeClass(state){
  if(state==='ok') return 'badge-ok';
  if(state==='warning') return 'badge-warn';
  if(state==='error') return 'badge-err';
  return 'badge-off';
}

function updateServiceBadge(id, name, svc){
  var el=document.getElementById(id);
  if(!el||!svc) return;
  el.className='badge '+svcBadgeClass(svc.state);
  var label=name;
  if(svc.state==='ok') label+=' ✓';
  else if(svc.state==='error') label+=' ✗';
  else if(svc.state==='warning') label+=' ⚠';
  if(svc.restarts && parseInt(svc.restarts,10)>0) label+=' ('+svc.restarts+'×)';
  el.textContent=label;
  el.title=svc.message||'';
}

function runPi2Action(path, successMessage){
  return fetch(path,{method:'POST'})
    .then(parsePi2Response)
    .then(({ok,data})=>{
      if(!ok || data.error){throw new Error(data.error||'Pi 2 action failed')}
      if(successMessage){alert(successMessage)}
      return data;
    })
    .catch(err=>alert(err.message||'Failed to reach Pi 2'));
}

function setBrightness(showSuccess){
  var v=document.querySelector('[name=display_brightness]').value;
  fetch('/api/pi2/display/brightness',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({value:parseInt(v)})
  })
    .then(parsePi2Response)
    .then(({ok,data})=>{
      if(!ok || data.error){throw new Error(data.error||'Brightness update failed')}
      if(showSuccess){alert('Pi 2 brightness test sent.')}
    })
    .catch(err=>alert(err.message||'Failed to reach Pi 2'));
}

function restartPlayback(){
  runPi2Action('/api/pi2/playback/reload','Pi 2 playback is restarting.');
}

function resyncNow(){
  runPi2Action('/api/pi2/sync/now','Pi 2 sync service restarted for an immediate resync.');
}

function switchPi1Mode(mode){
  var message=mode==='wifi-client'
    ? 'Switch Pi 1 out of AP mode and back onto the saved WiFi network? The dashboard may disappear until Pi 1 reconnects on its new address.'
    : 'Return Pi 1 to access-point mode?';
  if(!confirm(message)){return}
  var form=document.createElement('form');
  form.method='POST';
  form.action='/api/network_mode';
  var input=document.createElement('input');
  input.type='hidden';
  input.name='mode';
  input.value=mode;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}

// Poll Pi 2 status every 10 seconds
function pollPi2(){
  var ctrl=new AbortController();
  var tid=setTimeout(()=>ctrl.abort(),8000);
  fetch('/api/pi2/status',{signal:ctrl.signal})
    .then(r=>{clearTimeout(tid);return r.json().then(d=>({ok:r.ok,data:d}))})
    .then(({ok,data:d})=>{
      if(!ok || d.error){
        document.getElementById('pi2-offline').classList.remove('hidden');
        document.getElementById('pi2-online').classList.add('offline');
        var hint=document.getElementById('pi2-offline-hint');
        if(hint && d.tried_hosts){hint.textContent='Tried: '+d.tried_hosts.join(', ')+'. Last error: '+(d.error||'unknown')+'. Set the IP in Admin & Network if Pi 2 has a different address.';}
        ['p2-frame','p2-state','p2-fps','p2-uptime','p2-temp','p2-disk','p2-sync','p2-wifi'].forEach(id=>{
          document.getElementById(id).textContent='–';
        });
        return;
      }
      document.getElementById('pi2-offline').classList.add('hidden');
      document.getElementById('pi2-online').classList.remove('offline');
      document.getElementById('p2-frame').textContent=d.frame_current+' / '+d.frame_total;
      document.getElementById('p2-state').textContent=d.playback_state||'–';
      document.getElementById('p2-fps').textContent=(d.fps||'–')+' fps';
      document.getElementById('p2-uptime').textContent=d.uptime||'–';
      document.getElementById('p2-temp').textContent=d.cpu_temp||'–';
      document.getElementById('p2-disk').textContent=(d.disk_used_gb||0)+'/'+(d.disk_total_gb||0)+' GB';
      document.getElementById('p2-sync').textContent=d.last_sync_timestamp||'–';
      var pi2WifiText=d.wifi_message||'–';
      if(d.pi2_host){pi2WifiText+=' via '+d.pi2_host}
      document.getElementById('p2-wifi').textContent=pi2WifiText;
      if(d.services){
        updateServiceBadge('p2-svc-sync','Sync',d.services.sync);
        updateServiceBadge('p2-svc-playback','Playback',d.services.playback);
        updateServiceBadge('p2-svc-status_api','API',d.services.status_api);
      }
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
    if(d.services){
      updateServiceBadge('p1-svc-capture','Capture',d.services.capture);
      updateServiceBadge('p1-svc-portal','Portal',d.services.portal);
    }
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
