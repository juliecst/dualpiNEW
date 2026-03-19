#!/usr/bin/env python3
"""
Pi1 — Camera Server
Runs two threads:
  1. Capture thread: calls rpicam-still in a loop, saves to /tmp/latest.jpg
  2. HTTP server thread: serves /tmp/latest.jpg on port 8080

Atomic writes: captures to /tmp/latest_new.jpg then renames to /tmp/latest.jpg.
On failure: logs error, keeps serving previous frame, retries next cycle.
"""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")
DEFAULT_CAPTURE_INTERVAL = 5
DEFAULT_WIDTH = 4056
DEFAULT_HEIGHT = 3040
DEFAULT_QUALITY = 95
DEFAULT_TIMEOUT_MS = 1000
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
LATEST_JPG = "/tmp/latest.jpg"
LATEST_TMP = "/tmp/latest_new.jpg"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("camera_server")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
last_capture_time: str = ""
capture_lock = threading.Lock()


def load_config() -> dict:
    """Load capture settings from config.yaml, with safe defaults."""
    defaults = {
        "interval_seconds": DEFAULT_CAPTURE_INTERVAL,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "quality": DEFAULT_QUALITY,
        "timeout_ms": DEFAULT_TIMEOUT_MS,
        "extra_args": "",
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        capture_cfg = cfg.get("capture", {})
        for key in defaults:
            if key in capture_cfg:
                defaults[key] = capture_cfg[key]
    except FileNotFoundError:
        log.warning("Config file %s not found, using defaults", CONFIG_PATH)
    except Exception as exc:
        log.warning("Error reading config: %s, using defaults", exc)
    return defaults


# ---------------------------------------------------------------------------
# Capture thread
# ---------------------------------------------------------------------------
def capture_loop() -> None:
    """Continuously capture JPEG frames with rpicam-still."""
    global last_capture_time
    while True:
        cfg = load_config()
        interval = max(1, int(cfg["interval_seconds"]))
        cmd = [
            "rpicam-still",
            "--width", str(cfg["width"]),
            "--height", str(cfg["height"]),
            "--quality", str(cfg["quality"]),
            "--timeout", str(cfg["timeout_ms"]),
            "--output", LATEST_TMP,
            "--nopreview",
        ]
        extra = str(cfg.get("extra_args", "")).strip()
        if extra:
            cmd.extend(extra.split())

        try:
            log.info("Capturing: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and os.path.isfile(LATEST_TMP):
                os.rename(LATEST_TMP, LATEST_JPG)  # atomic on same filesystem
                with capture_lock:
                    last_capture_time = datetime.now(timezone.utc).isoformat()
                log.info("Capture OK → %s", LATEST_JPG)
            else:
                log.error(
                    "rpicam-still failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
        except subprocess.TimeoutExpired:
            log.error("rpicam-still timed out")
        except Exception as exc:
            log.error("Capture error: %s", exc)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class CameraHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler serving latest.jpg and health endpoint."""

    def do_GET(self):  # noqa: N802 — required by BaseHTTPRequestHandler interface
        if self.path == "/latest.jpg":
            self._serve_image()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_error(404, "Not Found")

    def _serve_image(self):
        try:
            with open(LATEST_JPG, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(503, "No image captured yet")
        except Exception as exc:
            log.error("Error serving image: %s", exc)
            self.send_error(500, "Internal Server Error")

    def _serve_health(self):
        with capture_lock:
            ts = last_capture_time
        body = json.dumps({
            "status": "ok",
            "last_capture": ts,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        """Suppress default stderr logging; use our logger instead."""
        log.debug("HTTP %s", fmt % args)


def http_server_loop() -> None:
    """Start HTTP server on port 8080."""
    server = HTTPServer(("0.0.0.0", HTTP_PORT), CameraHandler)
    log.info("HTTP server listening on port %d", HTTP_PORT)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Pi1 Camera Server starting (port=%d)", HTTP_PORT)

    # Start HTTP server in a daemon thread
    http_thread = threading.Thread(target=http_server_loop, daemon=True)
    http_thread.start()

    # Run capture loop in main thread (blocks forever)
    capture_loop()


if __name__ == "__main__":
    main()
