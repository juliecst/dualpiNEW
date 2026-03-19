#!/usr/bin/env python3
"""
Pi1 — Grabber
Main loop: every N seconds, HTTP-fetch the latest JPEG from Pi2's camera server
and save it as a timestamped file under /data/timelapse/current/YYYY-MM-DD/.

Features:
  - Atomic writes (temp file + rename)
  - Auto-creates daily subdirectories
  - Configurable poll interval and Pi2 URL
  - Resilient: logs errors and retries on next cycle
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")
DEFAULT_POLL_INTERVAL = 5
DEFAULT_PI2_URL = "http://192.168.50.20:8080/latest.jpg"
DEFAULT_OUTPUT_DIR = "/data/timelapse/current"
DEFAULT_TIMEOUT = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("grabber")


def load_config() -> dict:
    """Load grabber settings from config.yaml, with safe defaults."""
    defaults = {
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL,
        "pi2_url": DEFAULT_PI2_URL,
        "output_dir": DEFAULT_OUTPUT_DIR,
        "request_timeout_seconds": DEFAULT_TIMEOUT,
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        grabber_cfg = cfg.get("grabber", {})
        for key in defaults:
            if key in grabber_cfg:
                defaults[key] = grabber_cfg[key]
    except FileNotFoundError:
        log.warning("Config file %s not found, using defaults", CONFIG_PATH)
    except Exception as exc:
        log.warning("Error reading config: %s, using defaults", exc)
    return defaults


def fetch_and_save(pi2_url: str, output_dir: str, timeout: int) -> bool:
    """Fetch latest.jpg from Pi2 and save with timestamp. Returns True on success."""
    now = datetime.now()
    day_dir = os.path.join(output_dir, now.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)

    filename = now.strftime("%H-%M-%S") + ".jpg"
    final_path = os.path.join(day_dir, filename)
    tmp_path = final_path + ".tmp"

    try:
        response = urlopen(pi2_url, timeout=timeout)
        data = response.read()

        if len(data) < 1000:
            log.warning("Suspiciously small image (%d bytes), skipping", len(data))
            return False

        with open(tmp_path, "wb") as f:
            f.write(data)
        os.rename(tmp_path, final_path)  # atomic rename
        log.info("Saved %s (%d bytes)", final_path, len(data))
        return True

    except URLError as exc:
        log.error("HTTP fetch failed: %s", exc)
    except OSError as exc:
        log.error("File write error: %s", exc)
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
    finally:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return False


def main() -> None:
    log.info("Pi1 Grabber starting")
    consecutive_failures = 0

    while True:
        cfg = load_config()
        interval = max(1, int(cfg["poll_interval_seconds"]))
        pi2_url = cfg["pi2_url"]
        output_dir = cfg["output_dir"]
        timeout = int(cfg["request_timeout_seconds"])

        success = fetch_and_save(pi2_url, output_dir, timeout)
        if success:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures % 12 == 0:  # Every ~60s at 5s interval
                log.warning(
                    "Pi2 unreachable for %d consecutive attempts",
                    consecutive_failures,
                )

        time.sleep(interval)


if __name__ == "__main__":
    main()
