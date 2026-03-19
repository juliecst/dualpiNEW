#!/usr/bin/env bash
# Common — Disk Monitor
# Monitors disk usage and sets a warning flag file if usage exceeds threshold.
# Intended to be run via cron (e.g., every 6 hours).
# Uses hysteresis: sets flag at 85%, clears at 80%.
set -euo pipefail

MOUNT_POINT="${1:-/data}"
FLAG_FILE="${MOUNT_POINT}/disk_warning.flag"
HIGH_THRESHOLD=85
LOW_THRESHOLD=80
LOG_TAG="disk-monitor"

log() { logger -t "$LOG_TAG" "$*"; echo "$(date): $*"; }

# Get current usage percentage
if ! mountpoint -q "$MOUNT_POINT" 2>/dev/null && [ ! -d "$MOUNT_POINT" ]; then
    log "WARNING: $MOUNT_POINT does not exist or is not mounted"
    exit 1
fi

USAGE=$(df "$MOUNT_POINT" | tail -1 | awk '{print $5}' | tr -d '%')

if [ "$USAGE" -ge "$HIGH_THRESHOLD" ]; then
    if [ ! -f "$FLAG_FILE" ]; then
        echo "disk_usage=${USAGE}% at $(date)" > "$FLAG_FILE"
        log "WARNING: $MOUNT_POINT usage at ${USAGE}% (threshold: ${HIGH_THRESHOLD}%)"
    fi
elif [ "$USAGE" -lt "$LOW_THRESHOLD" ]; then
    if [ -f "$FLAG_FILE" ]; then
        rm -f "$FLAG_FILE"
        log "OK: $MOUNT_POINT usage back to ${USAGE}% (cleared warning)"
    fi
fi
