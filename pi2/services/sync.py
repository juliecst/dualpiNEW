#!/usr/bin/env python3
"""
Pi 2 — Image Sync Service
Timelapse Art Installation

Syncs frames from Pi 1's Samba share to local cache.
Detects session changes and wipes cache accordingly.
Runs on a 60-second loop with heartbeat for watchdog monitoring.
"""
import json
import os
import shutil
import subprocess
import time
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sync] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sync")

REMOTE_DIR = "/mnt/timelapse/current"
REMOTE_MOUNT = "/mnt/timelapse"
LOCAL_CACHE = "/data/cache"
LOCAL_SESSION = os.path.join(LOCAL_CACHE, "session.id")
REMOTE_SESSION = os.path.join(REMOTE_DIR, "session.id")
LAST_SYNC_FILE = "/data/last_sync.txt"
HEARTBEAT_FILE = "/data/sync_heartbeat.txt"
SYNC_INTERVAL = 60  # seconds
MOUNT_CHECK_TIMEOUT = 10  # seconds — max time to wait for a mount probe


def atomic_write(path: str, data: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.rename(tmp, path)


def write_heartbeat():
    """Write current timestamp to heartbeat file so monitors can detect hangs."""
    try:
        atomic_write(HEARTBEAT_FILE, datetime.now().isoformat())
    except Exception:
        pass


def read_file_safe(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _check_path_exists(path: str, result: list):
    """Target for threaded file check — avoids blocking on stale CIFS mounts."""
    try:
        result.append(os.path.isfile(path))
    except Exception:
        result.append(False)


def safe_path_exists(path: str, timeout: float = MOUNT_CHECK_TIMEOUT) -> bool:
    """Check if a file exists with a timeout to handle stale/hung mounts."""
    result = []
    t = threading.Thread(target=_check_path_exists, args=(path, result), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("Path check for %s timed out after %ds — mount may be stale", path, timeout)
        return False
    return bool(result and result[0])


def recover_stale_mount():
    """Attempt to unmount a stale/hung CIFS mount and remount it."""
    log.warning("Attempting to recover stale mount at %s", REMOTE_MOUNT)
    try:
        subprocess.run(
            ["umount", "-l", REMOTE_MOUNT],
            capture_output=True, text=True, timeout=10,
        )
        log.info("Lazy-unmounted %s", REMOTE_MOUNT)
    except Exception as e:
        log.warning("Lazy unmount failed: %s", e)
    time.sleep(1)
    try:
        result = subprocess.run(
            ["mount", REMOTE_MOUNT],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("Remounted %s after recovery", REMOTE_MOUNT)
            return True
        log.warning("Remount returned %d: %s", result.returncode, result.stderr.strip())
    except Exception as e:
        log.warning("Remount after recovery failed: %s", e)
    return False


def remote_available() -> bool:
    """Ensure the Samba mount is available, even after cold boots/outages."""
    if safe_path_exists(REMOTE_SESSION):
        return True

    # Check if mount point itself is hung (stale CIFS)
    result = []
    t = threading.Thread(
        target=lambda: result.append(os.path.ismount(REMOTE_MOUNT)),
        daemon=True,
    )
    t.start()
    t.join(MOUNT_CHECK_TIMEOUT)
    mount_hung = t.is_alive()

    if mount_hung:
        log.warning("Mount point %s appears stale/hung — recovering", REMOTE_MOUNT)
        recover_stale_mount()
        if safe_path_exists(REMOTE_SESSION):
            return True
        return False

    # Try mounting (handles automount and fstab-based mounts)
    try:
        result = subprocess.run(
            ["mount", REMOTE_MOUNT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("Mounted Pi 1 timelapse share at %s", REMOTE_MOUNT)
        elif result.stderr.strip():
            log.warning("Mount retry returned %d: %s", result.returncode, result.stderr.strip())
    except Exception as e:
        log.warning("Mount retry failed: %s", e)

    if safe_path_exists(REMOTE_SESSION):
        return True

    # Fall back: verify Pi 1 is reachable and Samba share exists
    try:
        result = subprocess.run(
            ["smbclient", "-N", "-L", "//192.168.50.1", "--timeout=5"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "timelapse" in result.stdout.lower():
            log.info("Pi 1 Samba share found — share may not have session.id yet")
        else:
            log.debug("smbclient probe: rc=%d", result.returncode)
    except Exception:
        pass

    return safe_path_exists(REMOTE_SESSION)


def get_remote_session() -> str:
    return read_file_safe(REMOTE_SESSION)


def get_local_session() -> str:
    return read_file_safe(LOCAL_SESSION)


def wipe_cache():
    """Remove all files in local cache."""
    log.info("Wiping local cache…")
    if os.path.isdir(LOCAL_CACHE):
        for item in os.listdir(LOCAL_CACHE):
            p = os.path.join(LOCAL_CACHE, item)
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
    os.makedirs(LOCAL_CACHE, exist_ok=True)


def sync_frames():
    """rsync new frames from remote to local cache."""
    cmd = [
        "rsync", "-a", "--update",
        "--include=*.jpg",
        "--include=session.id",
        "--exclude=*",
        REMOTE_DIR + "/",
        LOCAL_CACHE + "/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        log.warning("rsync returned %d: %s", result.returncode, result.stderr.strip())
        return False
    return True


def main():
    log.info("Sync service starting…")
    os.makedirs(LOCAL_CACHE, exist_ok=True)
    write_heartbeat()

    while True:
        write_heartbeat()
        try:
            if not remote_available():
                log.warning("Remote share not available yet — waiting for Pi 1 WiFi/share startup")
                write_heartbeat()
                time.sleep(SYNC_INTERVAL)
                continue

            remote_sid = get_remote_session()
            local_sid = get_local_session()

            if remote_sid and remote_sid != local_sid:
                log.info("Session changed: '%s' → '%s' — wiping cache", local_sid, remote_sid)
                wipe_cache()
                atomic_write(LOCAL_SESSION, remote_sid)

            log.info("Syncing frames…")
            write_heartbeat()
            if sync_frames():
                atomic_write(LAST_SYNC_FILE, datetime.now().isoformat())
                local_frames = len([f for f in os.listdir(LOCAL_CACHE) if f.endswith(".jpg")])
                log.info("Sync complete — %d frames in cache", local_frames)
            else:
                log.warning("Sync had issues — will retry next cycle")

        except Exception as e:
            log.error("Sync error: %s", e)

        write_heartbeat()
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
