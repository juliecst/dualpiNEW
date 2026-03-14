#!/usr/bin/env bash
###############################################################################
# Pi 1 — Disk Monitor (cron, every 6 hours)
# Writes/removes /data/disk_warning.flag based on usage threshold.
###############################################################################
set -euo pipefail

LOG_TAG="timelapse-diskmon"
DATA_MOUNT="/data"
WARNING_FLAG="/data/disk_warning.flag"
HIGH_THRESHOLD=85
LOW_THRESHOLD=80

log() { logger -t "$LOG_TAG" "$*"; }

if ! mountpoint -q "$DATA_MOUNT"; then
    log "WARNING: $DATA_MOUNT is not a mount point — skipping check"
    exit 0
fi

# Get usage percentage (integer)
USAGE=$(df --output=pcent "$DATA_MOUNT" | tail -1 | tr -d '% ')

log "Disk usage on $DATA_MOUNT: ${USAGE}%"

if [[ "$USAGE" -ge "$HIGH_THRESHOLD" ]]; then
    if [[ ! -f "$WARNING_FLAG" ]]; then
        echo "Disk usage ${USAGE}% — exceeded ${HIGH_THRESHOLD}% at $(date -Iseconds)" > "$WARNING_FLAG"
        log "WARNING: Disk usage ${USAGE}% — flag set"
    fi
elif [[ "$USAGE" -lt "$LOW_THRESHOLD" ]]; then
    if [[ -f "$WARNING_FLAG" ]]; then
        rm -f "$WARNING_FLAG"
        log "Disk usage ${USAGE}% — below ${LOW_THRESHOLD}%, flag cleared"
    fi
fi
