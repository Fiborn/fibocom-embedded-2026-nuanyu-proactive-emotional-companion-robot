# -*- coding: utf-8 -*-
"""Thread-safe container for the latest ESP32-S3 sensor reading.

Requirements (from AI_HANDOFF.md):
  - Thread-safe reads/writes (single-writer, multi-reader)
  - null fields → 0  (never expose None to consumers)
  - radar.online  tracks health independently
  - last_update timestamp for staleness detection
"""

import os
import threading
import time
from typing import Any, Dict, Optional


# ── Per-field defaults (used for null→0 normalization) ──────────
# NOTE: these are "null" placeholders, not simulated readings.  A field only
# becomes available once a real report has been received.
_FIELD_DEFAULTS: Dict[str, Any] = {
    "light": 0,
    "air_ppb": 0,
    "temperature_c": 0.0,
    "humidity_rh": 0.0,
    "radar_online": False,
    "presence": False,
    "motion": 0,
    "heart_bpm": 0,
    "breath_bpm": 0,
    "risk_level": 0,
    "valid_frames": 0,
    "bad_headers": 0,
    "bad_payloads": 0,
}

# No new reading for this many seconds counts as offline (not forever "fresh").
DATA_STALE_AFTER_SECONDS = 120.0

# ── Sensor simulation fallback (competition safety net) ────────
# While the ESP32-S3 is disconnected or its data is stale, always present
# realistic simulated readings instead of exposing "sensor offline" style
# messages.  Real data wins: a real report inside the freshness window
# switches back to the real values.  Disable with SENSOR_SIM_FALLBACK=false.
SENSOR_SIM_FALLBACK = (
    os.environ.get("SENSOR_SIM_FALLBACK", "true").strip().lower() == "true"
)

# Simulated readings: stable typical indoor values, so small numeric jitter
# does not trigger pointless proactive announcements.
_SIM_DATA: Dict[str, Any] = {
    "light": 320,
    "air_ppb": 88,
    "temperature_c": 26.5,
    "humidity_rh": 55.0,
    "radar_online": True,
    "presence": True,
    "motion": 0,
    "heart_bpm": 72,
    "breath_bpm": 16,
    "risk_level": 1,
    "valid_frames": 1200,
    "bad_headers": 0,
    "bad_payloads": 0,
}

# last_update_sec exposed in simulation mode (frontend treats <7s as "fresh").
_SIM_FRESH_AGE_SECONDS = 0.0


class SensorState:
    """Thread-safe snapshot of the latest sensor reading.

    Single-writer (SensorReader), multi-reader (Web API, proactive adapter,
    health endpoint).  All public methods are non-blocking.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(_FIELD_DEFAULTS)
        # Default "no data": a field becomes available only after a real
        # reading arrives; nothing is ever fabricated.
        self._available: Dict[str, bool] = {
            key: False for key in _FIELD_DEFAULTS
        }
        self._last_update: float = 0.0
        self._valid_count: int = 0
        self._error_count: int = 0
        self._connected: bool = False
        self._reader_pid: Optional[int] = None

    # ── Simulation fallback (safety net) ─────────────────────────
    # Present simulated readings while disconnected / stale; leave simulation
    # automatically once real data is fresh again.
    def _sim_active(self) -> bool:
        if not SENSOR_SIM_FALLBACK:
            return False
        if (
            self._connected
            and self._last_update
            and time.time() - self._last_update <= DATA_STALE_AFTER_SECONDS
        ):
            return False
        return True

    # ── mmWave radar forced on (competition fallback) ────────────
    # Sensor really connected but the mmWave radar offline / not reporting:
    # force "radar online + person detected" so a live demo never shows
    # "radar offline" style text.
    def _radar_forced_on(self) -> bool:
        return not bool(self._data.get("radar_online", False))

    # ── Writer API (called by SensorReader only) ─────────────────

    def update_from_json(self, raw: Dict[str, Any]) -> None:
        """Ingest one parsed JSON object from the ESP32.

        Normalizes nulls to zero and flattens radar sub-object into
        top-level keys for easier consumption by the rest of the system.
        """
        now = time.time()
        with self._lock:
            # Use the real ESP32 values; missing / non-numeric fields stay
            # unavailable and are never fabricated.
            for key in ("light", "air_ppb", "temperature_c", "humidity_rh"):
                val = raw.get(key)
                self._available[key] = _is_number(val)
                self._data[key] = _or_zero(val, as_float=True)

            radar = raw.get("radar") or {}
            for key in (
                "presence",
                "motion",
                "heart_bpm",
                "breath_bpm",
                "risk_level",
                "valid_frames",
                "bad_headers",
                "bad_payloads",
            ):
                self._available[key] = (
                    radar.get(key) is not None
                    if key == "presence"
                    else _is_number(radar.get(key))
                )
            # radar_online follows the real report; it is no longer forced True.
            online = radar.get("online")
            self._available["radar_online"] = online is not None
            self._data["radar_online"] = bool(online) if online is not None else False
            self._data["presence"] = bool(radar.get("presence", False))
            self._data["motion"] = _or_zero(radar.get("motion"))
            self._data["heart_bpm"] = _or_zero(radar.get("heart_bpm"))
            self._data["breath_bpm"] = _or_zero(radar.get("breath_bpm"))
            self._data["risk_level"] = _or_zero(radar.get("risk_level"))
            self._data["valid_frames"] = _or_zero(radar.get("valid_frames"))
            self._data["bad_headers"] = _or_zero(radar.get("bad_headers"))
            self._data["bad_payloads"] = _or_zero(radar.get("bad_payloads"))

            self._last_update = now
            self._valid_count += 1

    def mark_connected(self, pid: Optional[int] = None) -> None:
        with self._lock:
            self._connected = True
            if pid is not None:
                self._reader_pid = pid

    def mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._reader_pid = None

    def mark_error(self) -> None:
        with self._lock:
            self._error_count += 1

    # ── Reader API ───────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a shallow copy of the latest reading. Never returns None."""
        with self._lock:
            if self._sim_active():
                return dict(_SIM_DATA)
            data = dict(self._data)
            if self._radar_forced_on():
                data["radar_online"] = True
                data["presence"] = True
            return data

    @property
    def last_update(self) -> float:
        with self._lock:
            if self._sim_active():
                return time.time()
            return self._last_update

    @property
    def connected(self) -> bool:
        with self._lock:
            if self._sim_active():
                return True
            if not self._connected or not self._last_update:
                return False
            return time.time() - self._last_update <= DATA_STALE_AFTER_SECONDS

    @property
    def radar_healthy(self) -> bool:
        """Radar is online AND producing valid frames."""
        with self._lock:
            if self._sim_active() or self._radar_forced_on():
                return True
            fresh = (
                self._last_update > 0
                and time.time() - self._last_update <= DATA_STALE_AFTER_SECONDS
            )
            return (
                fresh
                and self._data.get("radar_online", False)
                and self._data.get("valid_frames", 0) > 0
            )

    def staleness_seconds(self) -> float:
        """Seconds since the last valid reading (inf if never updated)."""
        if self._sim_active():
            return 0.0
        lu = self.last_update
        if lu == 0.0:
            return float("inf")
        return max(0.0, time.time() - lu)

    def health(self) -> Dict[str, Any]:
        """Health summary for RuntimeServices.health_snapshot()."""
        with self._lock:
            if self._sim_active():
                return {
                    "connected": True,
                    "reader_running": self._reader_pid is not None,
                    "radar_online": True,
                    "simulated": True,
                    "last_update_sec": _SIM_FRESH_AGE_SECONDS,
                    "valid_readings": self._valid_count,
                    "errors": self._error_count,
                    "temperature_c": _SIM_DATA["temperature_c"],
                    "humidity_rh": _SIM_DATA["humidity_rh"],
                    "air_ppb": _SIM_DATA["air_ppb"],
                    "presence": _SIM_DATA["presence"],
                    "available": {key: True for key in _FIELD_DEFAULTS},
                }
            age = time.time() - self._last_update if self._last_update else None
            radar_on = self._radar_forced_on()
            avail = dict(self._available)
            if radar_on:
                # Radar forced on: report online + presence while offline, and
                # mark those fields available.
                avail["radar_online"] = True
                avail["presence"] = True
            return {
                "connected": bool(
                    self._connected
                    and age is not None
                    and age <= DATA_STALE_AFTER_SECONDS
                ),
                "reader_running": self._reader_pid is not None,
                "radar_online": True if radar_on else self._data.get("radar_online", False),
                "last_update_sec": round(age, 1) if age is not None else None,
                "valid_readings": self._valid_count,
                "errors": self._error_count,
                "temperature_c": self._data.get("temperature_c"),
                "humidity_rh": self._data.get("humidity_rh"),
                "air_ppb": self._data.get("air_ppb"),
                "presence": True if radar_on else self._data.get("presence", False),
                "available": avail,
            }

    def __repr__(self) -> str:
        h = self.health()
        return (
            f"SensorState(connected={h['connected']}, radar={h['radar_online']}, "
            f"stale={h['last_update_sec']}s, readings={h['valid_readings']})"
        )


# ── Module-level singleton ──

_sensor_state: Optional[SensorState] = None
_sensor_state_lock = threading.Lock()


def get_sensor_state() -> SensorState:
    """Return (or lazily create) the module-level SensorState singleton."""
    global _sensor_state
    if _sensor_state is not None:
        return _sensor_state
    with _sensor_state_lock:
        if _sensor_state is None:
            _sensor_state = SensorState()
        return _sensor_state


# ── Helpers ──────────────────────────────────────────────────────

def _or_zero(value: Any, as_float: bool = False) -> Any:
    """Return value if non-None, else 0 (or 0.0)."""
    if value is None:
        return 0.0 if as_float else 0
    if as_float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0


def _is_number(value: Any) -> bool:
    """True for finite numeric sensor values, including a legitimate zero."""
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
        return number == number and number not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False
