#!/usr/bin/env bash
###############################################################################
# Pi 2 — Render Job (cron, twice daily at 06:00 and 18:00)
# Renders archival .mp4 from /data/cache/ at the current FPS.
# Keeps only the 3 most recent renders.
###############################################################################
set -euo pipefail

LOG_TAG="timelapse-render"
CACHE_DIR="/data/cache"
RENDERS_DIR="/data/renders"
CONFIG_LOCAL="/data/config_local.json"
CONFIG_REMOTE="/mnt/timelapse/../config.json"

log() { logger -t "$LOG_TAG" "$*"; echo "[$(date '+%F %T')] $*"; }

# Read FPS from config
FPS=25
VIDEO_BACKUP_ENABLED=true
for cfg_path in "$CONFIG_REMOTE" "$CONFIG_LOCAL"; do
    real_path=$(realpath "$cfg_path" 2>/dev/null) || continue
    if [[ -f "$real_path" ]]; then
        FPS=$(jq -r '.playback_fps // 25' "$real_path" 2>/dev/null) || FPS=25
        VIDEO_BACKUP_ENABLED=$(jq -r 'if has("ffmpeg_video_backup_enabled") then .ffmpeg_video_backup_enabled else true end' "$real_path" 2>/dev/null) || VIDEO_BACKUP_ENABLED=true
        break
    fi
done

if [[ "$VIDEO_BACKUP_ENABLED" != "true" ]]; then
    log "Optional FFmpeg video backup disabled in config — skipping render"
    exit 0
fi

# Build a flat directory of numbered symlinks from archive + current frames
RENDER_INPUT="/tmp/timelapse_render_input"
rm -rf "$RENDER_INPUT"
mkdir -p "$RENDER_INPUT"

COUNTER=0
# Archive sessions first (sorted by session directory name for chronological order)
ARCHIVE_DIR="${CACHE_DIR}/archive"
if [[ -d "$ARCHIVE_DIR" ]]; then
    for session_dir in $(find "$ARCHIVE_DIR" -mindepth 1 -maxdepth 1 -type d | sort); do
        for frame in $(find "$session_dir" -maxdepth 1 -name 'frame_*.jpg' | sort); do
            COUNTER=$((COUNTER + 1))
            ln -sf "$frame" "${RENDER_INPUT}/$(printf 'frame_%06d.jpg' "$COUNTER")"
        done
    done
fi

# Current session frames
for frame in $(find "$CACHE_DIR" -maxdepth 1 -name 'frame_*.jpg' | sort); do
    COUNTER=$((COUNTER + 1))
    ln -sf "$frame" "${RENDER_INPUT}/$(printf 'frame_%06d.jpg' "$COUNTER")"
done

FRAME_COUNT=$COUNTER
if [[ "$FRAME_COUNT" -lt 2 ]]; then
    log "Only $FRAME_COUNT frames — skipping render"
    rm -rf "$RENDER_INPUT"
    exit 0
fi

# Get session ID
SESSION_ID="unknown"
if [[ -f "$CACHE_DIR/session.id" ]]; then
    SESSION_ID=$(cat "$CACHE_DIR/session.id")
fi

mkdir -p "$RENDERS_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUTPUT="${RENDERS_DIR}/${SESSION_ID}_${TIMESTAMP}.mp4"
TMP_OUTPUT="${OUTPUT}.tmp"

log "Rendering $FRAME_COUNT frames at ${FPS}fps → $OUTPUT"

ffmpeg -y \
    -framerate "$FPS" \
    -pattern_type glob \
    -i "${RENDER_INPUT}/frame_*.jpg" \
    -vf "deflicker=mode=pm:size=10" \
    -c:v libx264 \
    -preset medium \
    -crf 20 \
    -pix_fmt yuv420p \
    -movflags +faststart \
    "$TMP_OUTPUT" 2>&1 | tail -5

if [[ -f "$TMP_OUTPUT" ]]; then
    mv "$TMP_OUTPUT" "$OUTPUT"
    log "Render complete: $OUTPUT"
else
    log "ERROR: Render failed!"
    rm -rf "$RENDER_INPUT"
    exit 1
fi

rm -rf "$RENDER_INPUT"

# Keep only 3 most recent renders (exclude current_preview.mp4)
cd "$RENDERS_DIR"
ls -t *.mp4 2>/dev/null | grep -v "current_preview" | tail -n +4 | while read -r old; do
    log "Removing old render: $old"
    rm -f "$old"
done

log "Render job finished"
