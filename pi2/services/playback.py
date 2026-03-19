#!/usr/bin/env python3
"""
Pi 2 — Playback Service
Timelapse Art Installation

Plays timelapse frames on the Waveshare round display using mpv.
- FPS ≤ 30: image slideshow via mpv playlist with --image-display-duration
- FPS > 30: renders to .mp4 with ffmpeg (deflicker) and plays the video
Polls config.json every 30 seconds for changes.
Uses mpv IPC socket for playlist reload and playback control.

On cold boot, starts as soon as the first frame appears in /data/cache/.
Supports both HDMI and DRM/KMS (Waveshare round SPI) displays.
"""
import json
import glob
import os
import shutil
import subprocess
import time
import signal
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [playback] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("playback")

LOCAL_CACHE = "/data/cache"
LOCAL_ARCHIVE = os.path.join(LOCAL_CACHE, "archive")
RENDERS_DIR = "/data/renders"
PLAYLIST_PATH = "/tmp/timelapse_playlist.txt"
MPV_SOCKET = "/tmp/mpv-socket"
CONFIG_REMOTE = "/mnt/timelapse/../config.json"  # via Samba
CONFIG_LOCAL = "/data/config_local.json"
RENDERING_FLAG = "/tmp/rendering_in_progress"

DEFAULT_FPS = 25
DEFAULT_BRIGHTNESS = 100


def read_config() -> dict:
    """Read config from remote (Samba) or fall back to local cache."""
    defaults = {
        "playback_fps": DEFAULT_FPS,
        "display_brightness": DEFAULT_BRIGHTNESS,
        "display_type": "hdmi",
    }
    # Try remote config first
    for path in ["/mnt/timelapse/../config.json", "/data/config_local.json"]:
        try:
            real = os.path.realpath(path)
            with open(real) as f:
                cfg = json.load(f)
            # Cache locally
            if path != CONFIG_LOCAL:
                tmp = CONFIG_LOCAL + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f)
                os.rename(tmp, CONFIG_LOCAL)
            for k, v in defaults.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            continue
    return defaults


def get_sorted_frames() -> list:
    """Return sorted list of all frame paths (archive sessions first, then current).

    Archive sessions are sorted by session directory name (which is
    date-based) so frames play back in chronological order.
    """
    frames = []
    # Archived sessions come first
    if os.path.isdir(LOCAL_ARCHIVE):
        for session_name in sorted(os.listdir(LOCAL_ARCHIVE)):
            session_dir = os.path.join(LOCAL_ARCHIVE, session_name)
            if os.path.isdir(session_dir):
                frames.extend(sorted(glob.glob(os.path.join(session_dir, "frame_*.jpg"))))
    # Current session frames follow
    frames.extend(sorted(glob.glob(os.path.join(LOCAL_CACHE, "frame_*.jpg"))))
    return frames


def write_playlist(frames: list):
    """Write mpv playlist file atomically."""
    tmp = PLAYLIST_PATH + ".tmp"
    with open(tmp, "w") as f:
        for frame in frames:
            f.write(frame + "\n")
    os.rename(tmp, PLAYLIST_PATH)


def mpv_command(cmd: list):
    """Send a command to mpv via IPC socket."""
    import socket as sock
    try:
        s = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
        s.settimeout(2)
        s.connect(MPV_SOCKET)
        payload = json.dumps({"command": cmd}) + "\n"
        s.sendall(payload.encode())
        s.close()
    except Exception as e:
        log.debug("mpv IPC error: %s", e)


def mpv_running() -> bool:
    """Check if mpv process is running."""
    try:
        result = subprocess.run(["pgrep", "-f", "mpv.*timelapse"],
                                capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def kill_mpv():
    """Kill any running mpv instances."""
    subprocess.run(["pkill", "-f", "mpv.*timelapse"], capture_output=True)
    time.sleep(1)


def detect_drm_connector() -> str:
    """Auto-detect the active DRM connector for the Waveshare round display.

    Scans /sys/class/drm/ for connected outputs and returns the connector
    name suitable for mpv's --drm-connector option.  Falls back to an
    empty string (let mpv pick) if nothing is detected.
    """
    drm_base = "/sys/class/drm"
    if not os.path.isdir(drm_base):
        return ""
    connectors = []
    try:
        for entry in sorted(os.listdir(drm_base)):
            status_path = os.path.join(drm_base, entry, "status")
            if not os.path.isfile(status_path):
                continue
            with open(status_path) as f:
                status = f.read().strip()
            if status == "connected":
                # entry looks like "card0-DSI-1" or "card1-HDMI-A-1"
                # mpv wants the part after "cardN-"
                parts = entry.split("-", 1)
                if len(parts) == 2:
                    connectors.append(parts[1])
    except Exception as e:
        log.debug("DRM connector detection error: %s", e)
    # Prefer DSI/DPI connectors (typical for Waveshare round), then any
    for conn in connectors:
        if conn.startswith("DSI") or conn.startswith("DPI"):
            log.info("Auto-detected DRM connector: %s", conn)
            return conn
    if connectors:
        log.info("Auto-detected DRM connector (first available): %s", connectors[0])
        return connectors[0]
    return ""


def build_mpv_display_args(display_type: str) -> list:
    """Return mpv arguments for the configured display type."""
    if display_type == "spi":
        args = ["--vo=drm"]
        connector = detect_drm_connector()
        if connector:
            args.append(f"--drm-connector={connector}")
        return args

    # HDMI (default)
    if os.path.exists("/dev/dri/card0"):
        return ["--fullscreen", "--no-border", "--gpu-context=drm"]
    return ["--fullscreen", "--no-border", "--vo=gpu"]


def start_mpv_slideshow(fps: int, display_type: str):
    """Start mpv in slideshow mode for FPS ≤ 30."""
    if fps <= 0:
        fps = 1
    duration = 1.0 / fps

    cmd = [
        "mpv",
        "--really-quiet",
        "--no-terminal",
        f"--input-ipc-server={MPV_SOCKET}",
        f"--image-display-duration={duration:.6f}",
        "--loop-playlist=inf",
        "--no-osc",
        "--no-input-default-bindings",
        f"--playlist={PLAYLIST_PATH}",
        "--title=timelapse-playback",
    ]

    cmd += build_mpv_display_args(display_type)

    log.info("Starting mpv slideshow (FPS=%d, duration=%.4fs)", fps, duration)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def start_mpv_video(video_path: str, display_type: str):
    """Start mpv playing a rendered video."""
    cmd = [
        "mpv",
        "--really-quiet",
        "--no-terminal",
        f"--input-ipc-server={MPV_SOCKET}",
        "--loop=inf",
        "--no-osc",
        "--no-input-default-bindings",
        video_path,
        "--title=timelapse-playback",
    ]

    cmd += build_mpv_display_args(display_type)

    log.info("Starting mpv video: %s", video_path)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def render_preview(fps: int) -> str:
    """Render frames to preview .mp4 with deflicker. Returns path or empty string."""
    frames = get_sorted_frames()
    if not frames:
        return ""

    os.makedirs(RENDERS_DIR, exist_ok=True)
    output = os.path.join(RENDERS_DIR, "current_preview.mp4")
    tmp_output = output + ".tmp.mp4"

    log.info("Rendering %d frames at %d fps with deflicker…", len(frames), fps)

    # Create rendering flag
    with open(RENDERING_FLAG, "w") as f:
        f.write("rendering")

    # Build a temp directory with numbered symlinks so ffmpeg can use glob
    render_input = "/tmp/timelapse_render_input"
    try:
        if os.path.isdir(render_input):
            shutil.rmtree(render_input)
        os.makedirs(render_input)
        for i, frame in enumerate(frames, 1):
            os.symlink(os.path.abspath(frame),
                        os.path.join(render_input, f"frame_{i:06d}.jpg"))

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-pattern_type", "glob",
            "-i", os.path.join(render_input, "frame_*.jpg"),
            "-vf", "deflicker=mode=pm:size=10",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            tmp_output,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            os.rename(tmp_output, output)
            log.info("Render complete: %s", output)
            return output
        else:
            log.error("ffmpeg failed: %s", result.stderr[-500:] if result.stderr else "")
            return ""
    except subprocess.TimeoutExpired:
        log.error("ffmpeg render timed out after 30 minutes")
        return ""
    finally:
        # Clean up
        if os.path.exists(tmp_output):
            os.remove(tmp_output)
        if os.path.exists(RENDERING_FLAG):
            os.remove(RENDERING_FLAG)
        if os.path.isdir(render_input):
            shutil.rmtree(render_input)


def wait_for_frames():
    """Block until at least one frame appears in cache.

    Logs progress every 30 seconds so the journal shows the service is
    alive and waiting rather than silently idle.
    """
    log.info("Waiting for frames in %s …", LOCAL_CACHE)
    waited = 0
    while True:
        if get_sorted_frames():
            return
        if waited % 30 == 0 and waited > 0:
            log.info("Still waiting for first frame (%ds elapsed)…", waited)
        time.sleep(5)
        waited += 5


def main():
    log.info("Playback service starting…")
    os.makedirs(LOCAL_CACHE, exist_ok=True)
    os.makedirs(RENDERS_DIR, exist_ok=True)

    # On cold boot, block until sync has delivered at least one frame
    wait_for_frames()

    cfg = read_config()
    current_fps = cfg["playback_fps"]
    display_type = cfg.get("display_type", "hdmi")
    mpv_proc = None
    mode = None  # "slideshow" or "video"
    last_frame_count = 0
    last_config_poll = 0
    last_render_time = 0

    def cleanup(signum=None, frame=None):
        nonlocal mpv_proc
        log.info("Shutting down playback…")
        kill_mpv()
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    while True:
        try:
            now = time.time()

            # Poll config every 30 seconds
            if now - last_config_poll > 30:
                new_cfg = read_config()
                new_fps = new_cfg.get("playback_fps", DEFAULT_FPS)
                new_display = new_cfg.get("display_type", "hdmi")

                if new_fps != current_fps or new_display != display_type:
                    log.info("Config changed: FPS %d→%d, display %s→%s",
                             current_fps, new_fps, display_type, new_display)
                    current_fps = new_fps
                    display_type = new_display
                    kill_mpv()
                    mpv_proc = None
                    mode = None

                last_config_poll = now

            frames = get_sorted_frames()
            frame_count = len(frames)

            if frame_count == 0:
                log.debug("No frames in cache — waiting…")
                time.sleep(10)
                continue

            # Determine playback mode
            target_mode = "slideshow" if current_fps <= 30 else "video"

            if target_mode == "slideshow":
                # Write/update playlist
                write_playlist(frames)

                if mode != "slideshow" or not mpv_running():
                    kill_mpv()
                    mpv_proc = start_mpv_slideshow(current_fps, display_type)
                    mode = "slideshow"
                elif frame_count != last_frame_count:
                    # Reload playlist via IPC
                    write_playlist(frames)
                    mpv_command(["loadlist", PLAYLIST_PATH, "replace"])
                    log.info("Playlist reloaded: %d frames", frame_count)

            elif target_mode == "video":
                preview = os.path.join(RENDERS_DIR, "current_preview.mp4")

                # Render if needed (debounced: at most once every 10 minutes)
                need_render = (
                    not os.path.isfile(preview) or
                    (frame_count != last_frame_count and now - last_render_time > 600)
                )

                if need_render and not os.path.exists(RENDERING_FLAG):
                    kill_mpv()
                    mode = None
                    rendered = render_preview(current_fps)
                    last_render_time = now
                    if rendered:
                        preview = rendered

                if os.path.isfile(preview) and not os.path.exists(RENDERING_FLAG):
                    if mode != "video" or not mpv_running():
                        kill_mpv()
                        mpv_proc = start_mpv_video(preview, display_type)
                        mode = "video"

            last_frame_count = frame_count
            time.sleep(5)

        except Exception as e:
            log.error("Playback error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
