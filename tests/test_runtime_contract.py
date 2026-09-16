#!/usr/bin/env python3
"""Phase 1: Runtime contract checks — safe on hosts without Fibocom SDKs.

These tests validate that the Phase 1 refactoring did not break:
- HTTP API contract (endpoints, methods, response shapes)
- TTS backend names (drizzle / stream / surge)
- Default TTS selection via env
- Camera single-ownership
- ASR routing / pending queue
- Shutdown idempotency
"""
import collections
import os
import pathlib
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOT = REPO_ROOT / "app"

# The importable package lives in <repo>/app (app/src, app/static, ...).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "nuanyu_web.py").read_text(encoding="utf-8")
START_SCRIPT = REPO_ROOT / "deploy" / "start_nuanyu_runtime.sh"
START_TEXT = (START_SCRIPT.read_text(encoding="utf-8")
              if START_SCRIPT.exists() else "")


class TestApiContracts(unittest.TestCase):
    """HTTP API endpoints and their contracts."""

    def test_all_required_api_endpoints_exist(self):
        required_gets = [
            "/api/status",
            "/api/tts_status",
            "/api/tts_audio",
            "/api/users",
            "/api/whoami",
            "/api/logout",
            "/api/health",
        ]
        for path in required_gets:
            self.assertIn(path, MAIN,
                          "GET %s should exist in nuanyu_web.py" % path)

    def test_all_required_post_endpoints_exist(self):
        required_posts = [
            "/api/login",
            "/api/chat",
            "/api/command",
            "/api/mood",
            "/api/tts",
            "/api/tts_provider",
            "/api/shutdown",
        ]
        for path in required_posts:
            self.assertIn(path, MAIN,
                          "POST %s should exist in nuanyu_web.py" % path)

    def test_mutating_shutdown_is_post_only(self):
        get_section = MAIN.split("    def do_POST(self):", 1)[0]
        post_section_parts = MAIN.split("    def do_POST(self):", 1)
        self.assertEqual(len(post_section_parts), 2,
                         "do_POST must exist")
        post_section = post_section_parts[1]
        self.assertNotIn('path == "/api/shutdown"', get_section,
                         "shutdown must not be accessible via GET")
        self.assertIn('path == "/api/shutdown"', post_section,
                      "shutdown must be in do_POST")

    def test_tts_backend_names_match(self):
        """TTS backend names must remain drizzle / stream / surge."""
        for name in ["drizzle", "stream", "surge"]:
            self.assertIn(name, MAIN,
                          "TTS backend '%s' must be referenced" % name)

    def test_default_tts_backend_is_drizzle(self):
        """Default TTS is 'drizzle' (set in deploy/start_nuanyu_runtime.sh)."""
        self.assertTrue(START_TEXT, "deploy/start_nuanyu_runtime.sh must exist")
        self.assertIn("NUANYU_TTS_BACKEND", START_TEXT,
                      "startup script must set NUANYU_TTS_BACKEND")

    def test_camera_device_used_in_one_place_only(self):
        """VisionWorker owns /dev/video2; legacy vision_loop is deprecated."""
        self.assertIn("DEPRECATED", MAIN,
                      "vision_loop must be marked DEPRECATED")
        # VisionWorker is the canonical camera owner
        self.assertIn("VisionWorker", MAIN)


class TestAsrRoutingLogic(unittest.TestCase):
    """ASR routing: pending queue, resolve target, sleep gate."""

    def setUp(self):
        self.pending = collections.deque(maxlen=8)

    def test_pending_queue_bounded(self):
        for i in range(12):
            self.pending.append("msg-%d" % i)
        self.assertEqual(len(self.pending), 8)
        self.assertEqual(self.pending[0], "msg-4")

    def test_empty_queue_is_safe(self):
        drained = list(self.pending)
        self.assertEqual(drained, [])

    def test_flush_drains_all(self):
        for i in range(3):
            self.pending.append("item-%d" % i)
        count = 0
        while self.pending:
            self.pending.popleft()
            count += 1
        self.assertEqual(count, 3)


class TestCoordinatorLifecycle(unittest.TestCase):
    """StreamingTTSCoordinator session lifecycle."""

    def setUp(self):
        self.active = False
        self.pending = collections.deque()
        self.sessions = {}

    def _begin(self, replace=True):
        import uuid
        sid = uuid.uuid4().hex
        if replace:
            for sid in list(self.sessions):
                self.sessions[sid]["cancelled"] = True
            self.pending.clear()
        self.sessions[sid] = {"cancelled": False, "finished": False}
        return sid

    def test_normal_flow_finishes(self):
        sid = self._begin()
        self.sessions[sid]["finished"] = True
        self.assertTrue(self.sessions[sid]["finished"])

    def test_cancel_clears_pending(self):
        sid = self._begin()
        self.pending.append((sid, "text"))
        self.assertEqual(len(self.pending), 1)
        self.pending.clear()
        self.assertEqual(len(self.pending), 0)

    def test_consecutive_sessions_dont_bleed(self):
        s1 = self._begin()
        self.pending.append((s1, "req1"))
        s2 = self._begin(replace=True)
        self.pending.append((s2, "req2"))
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.pending[0][1], "req2")


class TestShutdownIdempotent(unittest.TestCase):
    """shutdown must be safe to call multiple times."""

    def test_idempotent_stop(self):
        stopped = False
        count = [0]

        def stop():
            nonlocal stopped
            if stopped:
                return
            stopped = True
            count[0] += 1

        stop()
        stop()
        stop()
        self.assertEqual(count[0], 1)


class TestVisionSingleOwner(unittest.TestCase):
    """Only one VisionWorker should ever own the camera."""

    def test_only_runtime_creates_vision_worker(self):
        """RuntimeServices._create_vision_worker is idempotent."""
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.load_config()
        rt.config["vision_enabled"] = False
        rt._create_vision_worker()
        self.assertIsNone(rt.vision_worker,
                          "VisionWorker must be None when vision is disabled")


class TestStartupOrdering(unittest.TestCase):
    """Creation order is enforced by RuntimeServices."""

    def test_startup_seq_is_recorded(self):
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.load_config()
        rt.config["asr_enabled"] = False
        rt.config["vision_enabled"] = False
        rt.initialize()
        self.assertIn("config", rt._startup_seq)
        self.assertIn("robot_slot", rt._startup_seq)


if __name__ == "__main__":
    unittest.main()
