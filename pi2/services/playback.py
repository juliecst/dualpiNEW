#!/usr/bin/env python3
"""
Pi 2 — Playback Service
Timelapse Art Installation

Plays timelapse frames on the Waveshare round display using mpv.
- FPS ≤ 30: image slideshow via mpv playlist with --image-display-duration
- FPS > 30: renders to .mp4 with ffmpeg (deflicker) and plays the video
Polls config.json every 30 seconds for changes.
Uses mpv IPC socket for playlist reload and playback control.
"""
import json
import glob
import os
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
    """Return sorted list of frame paths in cache."""
    frames = sorted(glob.glob(os.path.join(LOCAL_CACHE, "frame_*.jpg")))
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

    # Display-specific options
    if display_type == "spi":
        cmd += ["--vo=drm", "--drm-connector=0"]
    else:
        cmd += [
            "--fullscreen",
            "--no-border",
            "--gpu-context=drm" if os.path.exists("/dev/dri/card0") else "--vo=gpu",
        ]

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

    if display_type == "spi":
        cmd += ["--vo=drm", "--drm-connector=0"]
    else:
        cmd += [
            "--fullscreen",
            "--no-border",
            "--gpu-context=drm" if os.path.exists("/dev/dri/card0") else "--vo=gpu",
        ]

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

    try:
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-pattern_type", "glob",
            "-i", os.path.join(LOCAL_CACHE, "frame_*.jpg"),
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


def main():
    log.info("Playback service starting…")
    os.makedirs(LOCAL_CACHE, exist_ok=True)
    os.makedirs(RENDERS_DIR, exist_ok=True)

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
