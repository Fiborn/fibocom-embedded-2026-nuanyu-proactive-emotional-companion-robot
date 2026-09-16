#!/usr/bin/env python3
"""Phase 1: RuntimeServices singleton, lifecycle, and health tests.

Runs without board hardware, real models, or network.
"""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch


# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)


class FakeWorker:
    """Stand-in for ASRWorker / VisionWorker in tests."""

    def __init__(self, loaded=True, running=True):
        self.loaded = loaded
        self._running = running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def health(self):
        return {"loaded": self.loaded, "running": self._running}


class TestRuntimeServicesSingleton(unittest.TestCase):
    """get_runtime_services() returns same instance."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def test_singleton_returns_same_instance(self):
        from src.core.runtime_services import (get_runtime_services,
                                               set_runtime_services)
        set_runtime_services(None)
        rt1 = get_runtime_services()
        rt2 = get_runtime_services()
        self.assertIs(rt1, rt2)

    def test_set_injects_test_instance(self):
        from src.core.runtime_services import (get_runtime_services,
                                               set_runtime_services,
                                               RuntimeServices)
        fake = RuntimeServices()
        set_runtime_services(fake)
        rt = get_runtime_services()
        self.assertIs(rt, fake)


class TestRuntimeServicesInit(unittest.TestCase):
    """initialize() is idempotent and respects creation order."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def setUp(self):
        from src.core.runtime_services import set_runtime_services, RuntimeServices
        set_runtime_services(None)
        self.rt = RuntimeServices()
        self.rt.config = {
            "asr_enabled": False,
            "vision_enabled": False,
        }
        self.rt._initialized = False

    def test_initialize_is_idempotent(self):
        self.rt.initialize()
        seq1 = list(self.rt._startup_seq)
        self.rt.initialize()  # second call — no-op
        self.assertEqual(self.rt._startup_seq, seq1)

    def test_config_loaded_first(self):
        self.rt.initialize()
        self.assertEqual(self.rt._startup_seq[0], "config")

    def test_robot_slot_registered(self):
        self.rt.initialize()
        self.assertIn("robot_slot", self.rt._startup_seq)

    def test_asr_skipped_when_disabled(self):
        self.rt.config["asr_enabled"] = False
        self.rt.initialize()
        self.assertIsNone(self.rt.asr_worker)

    def test_vision_skipped_when_disabled(self):
        self.rt.config["vision_enabled"] = False
        self.rt.initialize()
        self.assertIsNone(self.rt.vision_worker)


class TestRuntimeServicesStop(unittest.TestCase):
    """stop() is idempotent, safe to call multiple times."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def test_stop_idempotent(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.stop()
        rt.stop()
        rt.stop()
        self.assertTrue(rt._stopped)

    def test_stop_event_set(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.stop()
        self.assertTrue(rt.stop_event.is_set())
        self.assertTrue(rt.is_stopping())

    def test_stop_does_not_crash_with_none_services(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.asr_worker = None
        rt.vision_worker = None
        rt.tts_coordinator = None
        rt.deepseek_client = None
        rt.stop()  # must not raise

    def test_stop_handles_failing_worker(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()

        class BrokenWorker:
            def stop(self):
                raise RuntimeError("simulated failure")

            def health(self):
                return {"loaded": False}

        rt.vision_worker = BrokenWorker()
        rt.asr_worker = FakeWorker()
        rt.stop()  # vision failure should not prevent ASR stop
        self.assertTrue(rt._stopped)


class TestRuntimeServicesHealth(unittest.TestCase):
    """health_snapshot() returns structured data."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def test_health_with_services(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt._initialized = True  # simulate post-initialize state
        rt.asr_worker = FakeWorker(loaded=True)
        rt.vision_worker = FakeWorker(loaded=True, running=True)
        rt.default_robot = MagicMock(running=True)
        snap = rt.health_snapshot()
        self.assertTrue(snap["initialized"])
        self.assertFalse(snap["stopping"])
        # 5 Phase 1 (asr/vision/tts/deepseek/default_robot) + sensor
        # + 5 Phase 2 (memory/persona/tools/proactive/cloud_sync).
        names = [s["name"] for s in snap["services"]]
        self.assertEqual(len(names), 11)
        self.assertIn("sensor", names)

    def test_health_without_services(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        snap = rt.health_snapshot()
        for s in snap["services"]:
            self.assertFalse(s["running"])


class TestCompatibilityAccessors(unittest.TestCase):
    """Backwards-compat functions work."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def setUp(self):
        from src.core.runtime_services import set_runtime_services, RuntimeServices
        set_runtime_services(None)
        self.rt = RuntimeServices()
        set_runtime_services(self.rt)

    def test_get_default_robot_returns_none_initially(self):
        from src.core.runtime_services import get_default_robot
        self.assertIsNone(get_default_robot())

    def test_get_default_robot_after_register(self):
        from src.core.runtime_services import get_default_robot
        robot = MagicMock(running=True)
        self.rt.register_default_robot(robot)
        self.assertIs(get_default_robot(), robot)

    def test_get_asr_worker(self):
        from src.core.runtime_services import get_asr_worker
        worker = FakeWorker()
        self.rt.asr_worker = worker
        self.assertIs(get_asr_worker(), worker)

    def test_get_vision_worker(self):
        from src.core.runtime_services import get_vision_worker
        worker = FakeWorker()
        self.rt.vision_worker = worker
        self.assertIs(get_vision_worker(), worker)


class TestSingleVisionOwner(unittest.TestCase):
    """Only RuntimeServices may create a VisionWorker."""

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def test_create_vision_worker_twice_is_idempotent(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.config["vision_enabled"] = False
        rt._create_vision_worker()
        self.assertIsNone(rt.vision_worker)
        rt._create_vision_worker()  # second call
        self.assertIsNone(rt.vision_worker)


if __name__ == "__main__":
    unittest.main()
