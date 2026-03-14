#!/usr/bin/env python3
"""
Pi 1 — Photo Capture Service
Timelapse Art Installation

Captures frames at a configurable interval using libcamera-still.
Supports auto and manual exposure modes, optional luma correction.
Polls config.json for live setting changes.
"""
import json
import os
import glob
import subprocess
import time
import logging
import sys
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [capture] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("capture")

CONFIG_PATH = "/data/config.json"
CURRENT_DIR = "/data/timelapse/current"
SESSION_FILE = os.path.join(CURRENT_DIR, "session.id")
LAST_CAPTURE_FILE = "/data/last_capture.txt"

# ── helpers ──────────────────────────────────────────────────────────────────

def read_config() -> dict:
    """Read config.json with sane defaults."""
    defaults = {
        "capture_interval_minutes": 5,
        "exposure_mode": "auto",
        "exposure_shutter_speed": 10000,
        "exposure_iso": 100,
        "luma_target": None,
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception as e:
        log.warning("Failed to read config: %s — using defaults", e)
        return defaults


def atomic_write(path: str, data: str):
    """Write data to path atomically via tmp+rename."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.rename(tmp, path)


def next_frame_number() -> int:
    """Find the highest existing frame number and return +1."""
    pattern = os.path.join(CURRENT_DIR, "frame_*.jpg")
    files = glob.glob(pattern)
    if not files:
        return 1
    nums = []
    for fp in files:
        base = os.path.basename(fp)
        try:
            n = int(base.replace("frame_", "").replace(".jpg", ""))
            nums.append(n)
        except ValueError:
            pass
    return max(nums) + 1 if nums else 1


def capture_frame(cfg: dict, frame_num: int) -> str:
    """Capture a single frame with libcamera-still. Returns output path."""
    fname = f"frame_{frame_num:06d}.jpg"
    tmp_path = os.path.join(CURRENT_DIR, fname + ".tmp")
    final_path = os.path.join(CURRENT_DIR, fname)

    cmd = [
        "libcamera-still",
        "--nopreview",
        "--timeout", "3000",       # 3 s AE/AWB convergence
        "-o", tmp_path,
        "--width", "4056",
        "--height", "3040",
        "-q", "95",
    ]

    if cfg["exposure_mode"] == "manual":
        shutter = cfg.get("exposure_shutter_speed", 10000)
        iso = cfg.get("exposure_iso", 100)
        # libcamera uses gain, not ISO directly.  ISO 100 ≈ gain 1.0
        gain = iso / 100.0
        cmd += ["--shutter", str(shutter), "--analoggain", str(gain)]

    log.info("Capturing frame %06d …", frame_num)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        log.error("libcamera-still failed: %s", result.stderr.strip())
        raise RuntimeError(f"Capture failed: {result.stderr.strip()}")

    os.rename(tmp_path, final_path)
    return final_path


def correct_luma(path: str, target: int):
    """Adjust average luminance of a JPEG toward target ±20."""
    try:
        from PIL import Image, ImageEnhance
        import numpy as np
    except ImportError:
        log.warning("Pillow/numpy not available — skipping luma correction")
        return

    img = Image.open(path)
    gray = img.convert("L")
    avg_luma = sum(gray.getdata()) / (gray.width * gray.height)

    if abs(avg_luma - target) <= 20:
        return  # already within tolerance

    # Compute gamma: target = avg_luma ^ gamma  →  gamma = log(target) / log(avg)
    if avg_luma < 1:
        avg_luma = 1
    gamma = max(0.1, min(5.0, (target / 255.0) / (avg_luma / 255.0)))

    # Apply gamma via point transform
    lut = [min(255, int(((i / 255.0) ** (1.0 / gamma)) * 255)) for i in range(256)]
    channels = img.split()
    corrected_channels = [ch.point(lut) for ch in channels]
    corrected = Image.merge(img.mode, corrected_channels)

    tmp = path + ".luma.tmp"
    corrected.save(tmp, "JPEG", quality=95)
    os.rename(tmp, path)
    new_avg = sum(corrected.convert("L").getdata()) / (gray.width * gray.height)
    log.info("Luma correction: %.1f → %.1f (target %d)", avg_luma, new_avg, target)


# ── main loop ────────────────────────────────────────────────────────────────

def main():
    log.info("Capture service starting…")

    # Ensure directory exists
    os.makedirs(CURRENT_DIR, exist_ok=True)

    # Ensure session.id exists
    if not os.path.isfile(SESSION_FILE):
        atomic_write(SESSION_FILE, datetime.now().strftime("%Y%m%d_%H%M%S"))
        log.info("Created new session.id")

    frame_num = next_frame_number()
    cfg = read_config()
    last_config_poll = time.time()

    log.info("Starting at frame %06d, interval %d min", frame_num, cfg["capture_interval_minutes"])

    while True:
        try:
            # Poll config every 60 seconds
            if time.time() - last_config_poll > 60:
                new_cfg = read_config()
                if new_cfg != cfg:
                    log.info("Config changed — applying new settings")
                    cfg = new_cfg
                last_config_poll = time.time()

            path = capture_frame(cfg, frame_num)

            # Optional luma correction
            luma_target = cfg.get("luma_target")
            if luma_target is not None:
                try:
                    luma_target = int(luma_target)
                    if 0 <= luma_target <= 255:
                        correct_luma(path, luma_target)
                except (ValueError, TypeError):
                    pass

            # Record last capture timestamp
            atomic_write(LAST_CAPTURE_FILE, datetime.now().isoformat())

            frame_num += 1
            interval_sec = cfg["capture_interval_minutes"] * 60
            log.info("Next capture in %d seconds", interval_sec)
            time.sleep(interval_sec)

        except KeyboardInterrupt:
            log.info("Shutting down capture service.")
            break
        except Exception as e:
            log.error("Capture error: %s — retrying in 30s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
