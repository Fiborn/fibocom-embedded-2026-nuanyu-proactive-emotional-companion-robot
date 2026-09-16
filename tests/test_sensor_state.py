"""SensorState reports only what the hardware actually sent.

Regression cover for the removal of the two fabrication mechanisms that used to
live in this class:

  * ``SENSOR_SIM_FALLBACK`` — invented a full set of plausible readings
    (including ``radar_online: True`` and ``presence: True``) whenever the
    ESP32-S3 was disconnected or its data had gone stale.
  * ``_radar_forced_on()`` — reported "radar online, person present" whenever
    the real radar was offline.

Both existed to keep a demo screen from ever showing "offline".  That is
fabricated health, which this project explicitly forbids, so both are gone.
These tests fail if either comes back.
"""

import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.sensors.sensor_state import SensorState, DATA_STALE_AFTER_SECONDS


def _report(radar_online=False, presence=False, valid_frames=0, **overrides):
    """A realistic ESP32-S3 report with the radar in a chosen state."""
    payload = {
        "light": 320,
        "air_ppb": 88,
        "temperature_c": 26.5,
        "humidity_rh": 55.0,
        "radar": {
            "online": radar_online,
            "presence": presence,
            "motion": 0,
            "heart_bpm": 72,
            "breath_bpm": 16,
            "risk_level": 1,
            "valid_frames": valid_frames,
            "bad_headers": 0,
            "bad_payloads": 0,
        },
    }
    payload.update(overrides)
    return payload


class NoDataTest(unittest.TestCase):
    """A freshly constructed state must claim nothing."""

    def setUp(self):
        self.state = SensorState()

    def test_reports_disconnected(self):
        self.assertFalse(self.state.connected)

    def test_snapshot_is_all_defaults(self):
        snap = self.state.snapshot()
        self.assertFalse(snap["radar_online"])
        self.assertFalse(snap["presence"])
        self.assertEqual(snap["temperature_c"], 0.0)

    def test_no_field_is_marked_available(self):
        health = self.state.health()
        self.assertFalse(health["connected"])
        self.assertFalse(any(health["available"].values()))

    def test_health_carries_no_simulated_flag(self):
        # The simulator is gone; a "simulated" key would mean it came back.
        self.assertNotIn("simulated", self.state.health())

    def test_staleness_is_infinite_before_any_reading(self):
        self.assertEqual(self.state.staleness_seconds(), float("inf"))


class RadarOfflineTest(unittest.TestCase):
    """A connected node whose radar is offline must say so."""

    def setUp(self):
        self.state = SensorState()
        self.state.mark_connected(pid=1234)
        self.state.update_from_json(_report(radar_online=False, presence=False))

    def test_radar_offline_is_not_forced_online(self):
        self.assertFalse(self.state.snapshot()["radar_online"])

    def test_absence_is_not_reported_as_presence(self):
        self.assertFalse(self.state.snapshot()["presence"])

    def test_radar_is_not_healthy(self):
        self.assertFalse(self.state.radar_healthy)

    def test_health_reports_radar_offline(self):
        health = self.state.health()
        self.assertTrue(health["connected"])       # the node itself is fine
        self.assertFalse(health["radar_online"])   # the radar is not
        self.assertFalse(health["presence"])

    def test_climate_fields_are_still_real(self):
        # Only the radar is down; the temperature/humidity sensors reported.
        snap = self.state.snapshot()
        self.assertEqual(snap["temperature_c"], 26.5)
        self.assertEqual(snap["humidity_rh"], 55.0)


class RadarOnlineTest(unittest.TestCase):
    """The honest positive case still works."""

    def setUp(self):
        self.state = SensorState()
        self.state.mark_connected(pid=1234)
        self.state.update_from_json(
            _report(radar_online=True, presence=True, valid_frames=900))

    def test_radar_is_healthy(self):
        self.assertTrue(self.state.radar_healthy)

    def test_presence_is_reported(self):
        self.assertTrue(self.state.snapshot()["presence"])

    def test_fields_are_marked_available(self):
        available = self.state.health()["available"]
        self.assertTrue(available["radar_online"])
        self.assertTrue(available["temperature_c"])
        self.assertTrue(available["presence"])


class RadarOnlineWithoutFramesTest(unittest.TestCase):
    """Online but producing no valid frames is not healthy."""

    def test_no_valid_frames_is_unhealthy(self):
        state = SensorState()
        state.mark_connected(pid=1234)
        state.update_from_json(_report(radar_online=True, valid_frames=0))
        self.assertFalse(state.radar_healthy)


class StalenessTest(unittest.TestCase):
    def test_stale_data_reports_disconnected(self):
        state = SensorState()
        state.mark_connected(pid=1234)
        state.update_from_json(_report(radar_online=True, valid_frames=10))

        real_time = __import__("time").time
        try:
            __import__("time").time = lambda: real_time() + DATA_STALE_AFTER_SECONDS + 1
            self.assertFalse(state.connected)
            self.assertFalse(state.radar_healthy)
        finally:
            __import__("time").time = real_time

    def test_disconnect_is_reported(self):
        state = SensorState()
        state.mark_connected(pid=1234)
        state.update_from_json(_report())
        self.assertTrue(state.connected)

        state.mark_disconnected()
        self.assertFalse(state.connected)


if __name__ == "__main__":
    unittest.main()
