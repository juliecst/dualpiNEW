#!/usr/bin/env bash
###############################################################################
# Pi 1 — Daily Backup Script (cron, 03:00)
# rsync /data/timelapse/ → /backup/timelapse/
###############################################################################
set -euo pipefail

LOG_TAG="timelapse-backup"
DATA_DIR="/data/timelapse"
BACKUP_DIR="/backup/timelapse"
LAST_BACKUP_DATA="/data/last_backup.txt"
LAST_BACKUP_BACKUP="/backup/last_backup.txt"
WARNING_FLAG="/data/backup_warning.flag"

log() { logger -t "$LOG_TAG" "$*"; echo "[$(date '+%F %T')] $*"; }

# Verify /backup/ is a real mount (not the SD card)
if ! mountpoint -q /backup; then
    log "ERROR: /backup is not a mount point — backup stick missing?"
    echo "Backup failed: /backup not mounted — $(date -Iseconds)" > "$WARNING_FLAG"
    exit 1
fi

# Verify the backup device is not the SD card (check device major number)
BACKUP_DEV=$(df --output=source /backup | tail -1)
ROOT_DEV=$(df --output=source / | tail -1)
if [[ "$BACKUP_DEV" == "$ROOT_DEV" ]]; then
    log "ERROR: /backup is on the same device as / — wrong mount!"
    echo "Backup failed: /backup on SD card — $(date -Iseconds)" > "$WARNING_FLAG"
    exit 1
fi

log "Starting backup: $DATA_DIR → $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"

if rsync -av --update "$DATA_DIR/" "$BACKUP_DIR/"; then
    NOW=$(date -Iseconds)
    log "Backup completed successfully at $NOW"

    # Write timestamps atomically
    TMP=$(mktemp)
    echo "$NOW" > "$TMP"
    cp "$TMP" "${LAST_BACKUP_DATA}.tmp"
    mv "${LAST_BACKUP_DATA}.tmp" "$LAST_BACKUP_DATA"
    cp "$TMP" "${LAST_BACKUP_BACKUP}.tmp"
    mv "${LAST_BACKUP_BACKUP}.tmp" "$LAST_BACKUP_BACKUP"
    rm -f "$TMP"

    # Clear warning flag
    rm -f "$WARNING_FLAG"
else
    log "ERROR: rsync failed!"
    echo "Backup failed: rsync error — $(date -Iseconds)" > "$WARNING_FLAG"
    exit 1
fi
