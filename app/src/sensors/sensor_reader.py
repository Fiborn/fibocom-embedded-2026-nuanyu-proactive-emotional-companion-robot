# -*- coding: utf-8 -*-
"""SensorReader — background thread that runs the ESP32-S3 USB receiver.

Delegates raw USB I/O to a subprocess (sc171_usb_receiver.py) so that:
  - A libusb crash cannot take down the main Python process.
  - The already-verified receiver logic is reused verbatim.
  - The main thread only parses JSON Lines — never touches ctypes/libusb.

Failure isolation (from AI_HANDOFF.md):
  - Sensor disconnection → connected=False, no crash.
  - Subprocess death → auto-restart with exponential backoff.
  - Malformed JSON → skipped, error counter incremented.
  - null values → normalized to 0 by SensorState.
"""

import json
import os
import subprocess
import sys
import threading
from typing import Optional

from src.sensors.sensor_state import SensorState, get_sensor_state

# The application's own directory (this file lives at app/src/sensors/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Configurable via environment ─────────────────────────────────

SENSOR_ENABLED = os.environ.get("SENSOR_ENABLED", "true").strip().lower() == "true"

# Where to find the USB receiver script.
_RECEIVER_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sc171_usb_receiver.py"),
    os.path.join(APP_DIR, "src", "sensors", "sc171_usb_receiver.py"),
    "/data/local/tmp/sc171_usb_cdc_receiver.py",
]

# How long to run each receiver invocation before restarting.
RECEIVER_RUN_SECONDS = int(os.environ.get("SENSOR_RUN_SECONDS", "86400"))  # 24 h

# USB CDC baud rate (LD6002 verified at 115200).
RECEIVER_BAUD = int(os.environ.get("SENSOR_BAUD", "115200"))

# Restart backoff (seconds). Caps at MAX_BACKOFF.
INITIAL_BACKOFF = float(os.environ.get("SENSOR_INITIAL_BACKOFF", "2.0"))
MAX_BACKOFF = float(os.environ.get("SENSOR_MAX_BACKOFF", "60.0"))
BACKOFF_MULTIPLIER = 2.0

# Required top-level keys in a valid sensor JSON.
_REQUIRED_KEYS = {"light", "air_ppb", "temperature_c", "humidity_rh", "radar"}


def _parse_receiver_line(line: str) -> Optional[dict]:
    """Parse one receiver line.

    The verified USB receiver prefixes raw serial lines with ``[RX] `` for
    interactive diagnostics.  Accept both that format and plain JSON Lines so
    the reader remains compatible with future machine-only receiver output.
    """
    candidate = line.strip()
    if candidate.startswith("[RX] "):
        candidate = candidate[5:].strip()
    if not candidate.startswith("{"):
        return None
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not _REQUIRED_KEYS.issubset(obj):
        return None
    if not isinstance(obj.get("radar"), dict):
        return None
    return obj


def _find_receiver() -> Optional[str]:
    """Return the first existing receiver script path, or None."""
    for candidate in _RECEIVER_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


class SensorReader:
    """Manages a long-running subprocess that reads ESP32-S3 sensor JSON.

    Usage:
        reader = SensorReader(state=get_sensor_state())
        reader.start()   # launches subprocess in a daemon thread
        ...
        reader.stop()    # graceful shutdown
    """

    def __init__(self, state: Optional[SensorState] = None):
        self._state = state or get_sensor_state()
        self._receiver_path: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return SENSOR_ENABLED

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> bool:
        """Launch the receiver subprocess in a background daemon thread.

        Returns True if the sensor subsystem is enabled and started,
        False if disabled or hardware not found.
        """
        if not SENSOR_ENABLED:
            print("[SENSOR] disabled by SENSOR_ENABLED env", flush=True)
            return False

        self._receiver_path = _find_receiver()
        if self._receiver_path is None:
            print("[SENSOR] receiver script not found in: %s" %
                  ", ".join(_RECEIVER_CANDIDATES), flush=True)
            return False

        with self._lock:
            if self._running:
                return True
            self._running = True
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="SensorWatchdog",
        )
        self._thread.start()
        print("[SENSOR] reader started receiver=%s baud=%d run_sec=%d" %
              (self._receiver_path, RECEIVER_BAUD, RECEIVER_RUN_SECONDS), flush=True)
        return True

    def stop(self) -> None:
        """Graceful shutdown: kill subprocess and join thread."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop_event.set()
        self._kill_proc()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._state.mark_disconnected()
        print("[SENSOR] reader stopped", flush=True)

    def health(self) -> dict:
        return self._state.health()

    # ── Internal ─────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Restart the receiver subprocess with exponential backoff."""
        backoff = INITIAL_BACKOFF
        while not self._stop_event.is_set():
            self._state.mark_disconnected()
            exit_code = self._run_one_session()
            if self._stop_event.is_set():
                break
            if exit_code == 0:
                backoff = INITIAL_BACKOFF
                print("[SENSOR] receiver exited cleanly, restarting", flush=True)
            else:
                print("[SENSOR] receiver exited code=%s, restart in %.1fs" %
                      (exit_code, backoff), flush=True)
                if self._stop_event.wait(timeout=backoff):
                    break
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)

    def _run_one_session(self) -> int:
        """Launch the receiver, read JSON Lines from stdout, return exit code."""
        cmd = [
            sys.executable, self._receiver_path,
            "--seconds", str(RECEIVER_RUN_SECONDS),
            "--baud", str(RECEIVER_BAUD),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            print("[SENSOR] failed to spawn receiver: %s" % exc, flush=True)
            self._state.mark_error()
            return -1

        print("[SENSOR] receiver pid=%d" % self._proc.pid, flush=True)

        proc = self._proc
        try:
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                obj = _parse_receiver_line(line)
                if obj is None:
                    if (
                        line.startswith("[SC171-USB]")
                        and "SENSOR_DATA_OK" not in line
                    ):
                        print("[SENSOR] %s" % line, flush=True)
                    continue
                self._state.update_from_json(obj)
                self._state.mark_connected(pid=proc.pid)
        except Exception as exc:
            print("[SENSOR] stdout read error: %s" % exc, flush=True)
            self._state.mark_error()
        if self._stop_event.is_set() and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5.0)
            return_code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass
            return_code = -9
        except Exception:
            return_code = -1
        finally:
            self._state.mark_disconnected()
            if self._proc is proc:
                self._proc = None
        return return_code

    def _kill_proc(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdout.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            except Exception:
                pass
        except Exception:
            pass
        self._proc = None
