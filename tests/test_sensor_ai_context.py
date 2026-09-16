import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.sensors.ai_context import SensorAnnouncementMonitor, build_sensor_context


class SensorAIContextTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "temperature_c": 26.45,
            "humidity_rh": 64.49,
            "air_ppb": 2437,
            "light": 0,
            "presence": False,
            "risk_level": 0,
        }
        self.health = {
            "connected": True,
            "last_update_sec": 0.5,
            "available": {
                "temperature_c": True,
                "humidity_rh": True,
                "air_ppb": True,
                "light": False,
                "presence": True,
                "risk_level": True,
            },
        }

    def test_context_contains_only_fresh_available_readings(self):
        text = build_sensor_context(self.snapshot, self.health)

        self.assertIn("温度26.4°C", text)
        self.assertIn("湿度64.5%RH", text)
        # Air quality is reported as a language band, not a raw ppb number:
        # the LLM is given something it can talk about without inventing units.
        self.assertIn("空气质量：较差", text)
        self.assertNotIn("2437", text)
        self.assertIn("毫米波未检测到人", text)
        self.assertNotIn("光照", text)

    def test_offline_context_forbids_invented_values(self):
        text = build_sensor_context(self.snapshot, {"connected": False})

        self.assertIn("传感器当前离线", text)
        self.assertIn("不得编造", text)
        self.assertNotIn("26.4", text)

    def test_monitor_announces_once_then_waits_for_meaningful_change(self):
        monitor = SensorAnnouncementMonitor(cooldown_seconds=300)

        # Initial readings establish a silent baseline; startup must not seize
        # the speaker before the user begins a conversation.
        self.assertIsNone(monitor.poll(self.snapshot, self.health, now=1000))
        self.assertIsNone(monitor.poll(self.snapshot, self.health, now=1001))

        changed = dict(self.snapshot, temperature_c=31.0)
        prompt = monitor.poll(changed, self.health, now=1301)
        self.assertIn("温度偏高", prompt)

        # A sustained abnormal value is not repeated after the cooldown.
        self.assertIsNone(monitor.poll(changed, self.health, now=1602))

    def test_monitor_never_announces_simulated_fallback(self):
        monitor = SensorAnnouncementMonitor(cooldown_seconds=1)
        health = dict(self.health, simulated=True)

        self.assertIsNone(monitor.poll(self.snapshot, health, now=1000))
        changed = dict(self.snapshot, risk_level=2, temperature_c=35.0)
        self.assertIsNone(monitor.poll(changed, health, now=2000))


if __name__ == "__main__":
    unittest.main()
