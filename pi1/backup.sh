#!/usr/bin/env bash
# Pi1 — Daily Backup
# Syncs /data/timelapse/ → /backup/ using rsync.
# Triggered daily by backup.timer systemd timer.
set -euo pipefail

SOURCE="/data/timelapse/"
DEST="/backup/"
DATE=$(date +%Y-%m-%d)
LOG_TAG="timelapse-backup"

log() { logger -t "$LOG_TAG" "$*"; echo "$(date): $*"; }

# Verify /backup is a real mount point (not just the SD card root)
if ! mountpoint -q /backup 2>/dev/null; then
    # Try to mount if in fstab
    mount /backup 2>/dev/null || true
    if ! mountpoint -q /backup 2>/dev/null; then
        log "ERROR: /backup is not a mount point. Backup skipped."
        exit 1
    fi
fi

# Verify source exists
if [ ! -d "$SOURCE" ]; then
    log "WARNING: Source $SOURCE does not exist. Nothing to back up."
    exit 0
fi

log "Starting daily backup: $SOURCE → $DEST"

# Run rsync (archive, verbose, update only newer files)
if rsync -av --update "$SOURCE" "${DEST}timelapse/"; then
    echo "$DATE $(date +%H:%M:%S)" > "${DEST}last_backup.txt"
    log "Backup complete: $DATE"
else
    log "ERROR: rsync failed with exit code $?"
    echo "backup_failed" > /data/backup_warning.flag
    exit 1
fi

# Remove warning flag on success
rm -f /data/backup_warning.flag
