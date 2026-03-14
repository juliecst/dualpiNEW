#!/usr/bin/env python3
"""
Pi 2 — Image Sync Service
Timelapse Art Installation

Syncs frames from Pi 1's Samba share to local cache.
Detects session changes and wipes cache accordingly.
Runs on a 60-second loop.
"""
import json
import os
import shutil
import subprocess
import time
import logging
import sys
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
SYNC_INTERVAL = 60  # seconds


def atomic_write(path: str, data: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.rename(tmp, path)


def read_file_safe(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def remote_available() -> bool:
    """Ensure the Samba mount is available, even after cold boots/outages."""
    if os.path.isfile(REMOTE_SESSION):
        return True

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

    return os.path.isfile(REMOTE_SESSION)


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

    while True:
        try:
            if not remote_available():
                log.warning("Remote share not available yet — waiting for Pi 1 WiFi/share startup")
                time.sleep(SYNC_INTERVAL)
                continue

            remote_sid = get_remote_session()
            local_sid = get_local_session()

            if remote_sid and remote_sid != local_sid:
                log.info("Session changed: '%s' → '%s' — wiping cache", local_sid, remote_sid)
                wipe_cache()
                atomic_write(LOCAL_SESSION, remote_sid)

            log.info("Syncing frames…")
            if sync_frames():
                atomic_write(LAST_SYNC_FILE, datetime.now().isoformat())
                local_frames = len([f for f in os.listdir(LOCAL_CACHE) if f.endswith(".jpg")])
                log.info("Sync complete — %d frames in cache", local_frames)
            else:
                log.warning("Sync had issues — will retry next cycle")

        except Exception as e:
            log.error("Sync error: %s", e)

        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
