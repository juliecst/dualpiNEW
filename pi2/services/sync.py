#!/usr/bin/env python3
"""
Pi 2 — Image Sync Service
Timelapse Art Installation

Syncs frames from Pi 1's Samba share to local cache.
Pulls from both current/ and archive/ directories so
the timelapse video includes all available frames.
Detects session changes and wipes current-session cache accordingly.
Runs on a 60-second loop with heartbeat for watchdog monitoring.
"""
import glob
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
REMOTE_ARCHIVE = "/mnt/timelapse/archive"
REMOTE_MOUNT = "/mnt/timelapse"
LOCAL_CACHE = "/data/cache"
LOCAL_ARCHIVE = os.path.join(LOCAL_CACHE, "archive")
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
    except OSError as e:
        log.debug("Path check for %s raised %s", path, e)
        result.append(False)


def safe_file_exists(path: str, timeout: float = MOUNT_CHECK_TIMEOUT) -> bool:
    """Check if a file exists with a timeout to handle stale/hung mounts."""
    result = []
    t = threading.Thread(target=_check_path_exists, args=(path, result), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("Path check for %s timed out after %ds — mount may be stale", path, timeout)
        return False
    return bool(result and result[0])


def _safe_isdir(path: str, timeout: float = MOUNT_CHECK_TIMEOUT) -> bool:
    """Check if a directory is accessible with a timeout (avoids stale CIFS hangs)."""
    result = []

    def _check():
        try:
            result.append(os.path.isdir(path) and os.access(path, os.R_OK))
        except OSError:
            result.append(False)

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("Directory check for %s timed out after %ds — mount may be stale", path, timeout)
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
    """Ensure the Samba mount is available, even after cold boots/outages.

    Returns True when the mount point is accessible, regardless of whether
    session.id exists.  This lets the sync loop proceed and pull whatever
    frames are available (archive and/or current).
    """
    # Quick check: can we read the mount point directory?
    if _safe_isdir(REMOTE_MOUNT):
        return True

    # Check if mount point itself is hung (stale CIFS)
    mount_result = []

    def _check_ismount():
        try:
            mount_result.append(os.path.ismount(REMOTE_MOUNT))
        except OSError as e:
            log.debug("ismount check for %s raised %s", REMOTE_MOUNT, e)
            mount_result.append(False)

    t = threading.Thread(target=_check_ismount, daemon=True)
    t.start()
    t.join(MOUNT_CHECK_TIMEOUT)
    mount_hung = t.is_alive()

    if mount_hung:
        log.warning("Mount point %s appears stale/hung — recovering", REMOTE_MOUNT)
        recover_stale_mount()
        return _safe_isdir(REMOTE_MOUNT)

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

    if _safe_isdir(REMOTE_MOUNT):
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
            log.info("Pi 1 Samba share found — share may not be mounted yet")
        else:
            log.debug("smbclient probe: rc=%d", result.returncode)
    except Exception:
        pass

    return _safe_isdir(REMOTE_MOUNT)


def get_remote_session() -> str:
    return read_file_safe(REMOTE_SESSION)


def get_local_session() -> str:
    return read_file_safe(LOCAL_SESSION)


def wipe_cache():
    """Remove current-session files in local cache, preserving archive."""
    log.info("Wiping current-session cache…")
    if os.path.isdir(LOCAL_CACHE):
        for item in os.listdir(LOCAL_CACHE):
            if item == "archive":
                continue  # preserve synced archive sessions
            p = os.path.join(LOCAL_CACHE, item)
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
    os.makedirs(LOCAL_CACHE, exist_ok=True)


def sync_frames():
    """rsync new frames from remote current/ to local cache."""
    if not os.path.isdir(REMOTE_DIR):
        return True  # current dir may not exist yet
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


def sync_archive_sessions() -> int:
    """Sync archived sessions from Pi 1 to local cache/archive/ subdirectories.

    Returns total number of archive frames synced.
    """
    if not _safe_isdir(REMOTE_ARCHIVE, timeout=5):
        return 0

    os.makedirs(LOCAL_ARCHIVE, exist_ok=True)
    total_frames = 0

    try:
        remote_sessions = sorted(os.listdir(REMOTE_ARCHIVE))
    except OSError as e:
        log.warning("Could not list remote archive: %s", e)
        return 0

    for session_name in remote_sessions:
        remote_session_dir = os.path.join(REMOTE_ARCHIVE, session_name)
        if not os.path.isdir(remote_session_dir):
            continue

        local_session_dir = os.path.join(LOCAL_ARCHIVE, session_name)
        os.makedirs(local_session_dir, exist_ok=True)

        cmd = [
            "rsync", "-a", "--update",
            "--include=*.jpg",
            "--exclude=*",
            remote_session_dir + "/",
            local_session_dir + "/",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                log.warning("Archive rsync for %s returned %d: %s",
                            session_name, result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired:
            log.warning("Archive rsync for %s timed out", session_name)

        session_count = len(glob.glob(os.path.join(local_session_dir, "frame_*.jpg")))
        total_frames += session_count

    # Remove local archive sessions that no longer exist on remote
    try:
        local_sessions = set(os.listdir(LOCAL_ARCHIVE))
        remote_set = set(remote_sessions)
        for stale in local_sessions - remote_set:
            stale_path = os.path.join(LOCAL_ARCHIVE, stale)
            if os.path.isdir(stale_path):
                log.info("Removing stale archive session: %s", stale)
                shutil.rmtree(stale_path)
    except OSError:
        pass

    return total_frames


def count_all_frames() -> int:
    """Count all frames in cache (archive + current session)."""
    total = len(glob.glob(os.path.join(LOCAL_CACHE, "frame_*.jpg")))
    if os.path.isdir(LOCAL_ARCHIVE):
        for session_name in os.listdir(LOCAL_ARCHIVE):
            session_dir = os.path.join(LOCAL_ARCHIVE, session_name)
            if os.path.isdir(session_dir):
                total += len(glob.glob(os.path.join(session_dir, "frame_*.jpg")))
    return total


def main():
    log.info("Sync service starting…")
    os.makedirs(LOCAL_CACHE, exist_ok=True)
    os.makedirs(LOCAL_ARCHIVE, exist_ok=True)
    write_heartbeat()

    while True:
        write_heartbeat()
        try:
            if not remote_available():
                log.warning("Remote share not available yet — waiting for Pi 1 WiFi/share startup")
                write_heartbeat()
                time.sleep(SYNC_INTERVAL)
                continue

            # ── Sync archived sessions ──────────────────────────────────
            write_heartbeat()
            archive_count = sync_archive_sessions()
            if archive_count > 0:
                log.info("Archive sync: %d frames across archive sessions", archive_count)

            # ── Handle current session ──────────────────────────────────
            remote_sid = get_remote_session()
            local_sid = get_local_session()

            if remote_sid and remote_sid != local_sid:
                log.info("Session changed: '%s' → '%s' — wiping current cache", local_sid, remote_sid)
                wipe_cache()
                atomic_write(LOCAL_SESSION, remote_sid)

            log.info("Syncing frames…")
            write_heartbeat()
            if sync_frames():
                atomic_write(LAST_SYNC_FILE, datetime.now().isoformat())
                total = count_all_frames()
                log.info("Sync complete — %d total frames in cache", total)
            else:
                log.warning("Sync had issues — will retry next cycle")

        except Exception as e:
            log.error("Sync error: %s", e)

        write_heartbeat()
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
