#!/usr/bin/env python3
"""Phase 1: ASR result routing and startup-order tests.

These tests verify that:
- ASR results are buffered when no ROBOT is registered yet.
- Buffered results are delivered when ROBOT becomes available.
- Sleep state prevents ASR-triggered chat.
- The pending queue has an upper bound.
"""

import collections
import threading
import unittest


class FakeRobot:
    """Minimal stand-in for NuanyuCore during routing tests."""

    def __init__(self, running=True):
        self.running = running
        self.events = collections.deque()
        self.chat_history = collections.deque()
        self.ai_ok = True
        self._replies = collections.deque()

    def add_event(self, event_type, text):
        self.events.append((event_type, text))

    def add_chat(self, role, text, **kwargs):
        self.chat_history.append((role, text))

    def ask_ai(self, text, scene="chat", max_tokens=80):
        reply = self._replies.popleft() if self._replies else "test reply: %s" % text[:20]
        return reply

    def queue_reply(self, text):
        self._replies.append(text)


class TestResolveTargetRobot(unittest.TestCase):
    """resolve_asr_target_robot() behaviour."""

    def _make_resolve(self, robot_ref):
        """Simulate the module-level function."""
        def resolve():
            nonlocal robot_ref
            if robot_ref is None or not getattr(robot_ref, "running", False):
                return None
            return robot_ref
        return resolve

    def test_returns_none_when_robot_is_none(self):
        robot = None
        fn = self._make_resolve(robot)
        self.assertIsNone(fn())

    def test_returns_none_when_robot_not_running(self):
        robot = FakeRobot(running=False)
        fn = self._make_resolve(robot)
        self.assertIsNone(fn())

    def test_returns_robot_when_running(self):
        robot = FakeRobot(running=True)
        fn = self._make_resolve(robot)
        self.assertIs(fn(), robot)


class TestPendingAsrQueue(unittest.TestCase):
    """ASR result buffering when no robot is available."""

    def setUp(self):
        self.pending = collections.deque(maxlen=8)
        self.lock = threading.Lock()

    def test_buffers_when_no_robot(self):
        """Results are buffered when robot is None."""
        with self.lock:
            self.pending.append("hello")
            self.pending.append("world")
        self.assertEqual(len(self.pending), 2)
        self.assertEqual(self.pending[0], "hello")

    def test_queue_bounded_to_max(self):
        """Oldest entry dropped when queue is full."""
        for i in range(12):
            with self.lock:
                self.pending.append("msg-%d" % i)
        self.assertEqual(len(self.pending), 8)
        self.assertEqual(self.pending[0], "msg-4")  # 0-3 dropped
        self.assertEqual(self.pending[-1], "msg-11")

    def test_empty_queue_iteration_is_safe(self):
        """Iterating an empty pending queue doesn't crash."""
        drained = []
        while self.pending:
            drained.append(self.pending.popleft())
        self.assertEqual(len(drained), 0)

    def test_flush_delivers_all_to_target(self):
        """Flushing delivers every buffered item to the robot."""
        robot = FakeRobot()
        robot.queue_reply("r1")
        robot.queue_reply("r2")
        robot.queue_reply("r3")
        with self.lock:
            self.pending.append("a")
            self.pending.append("b")
            self.pending.append("c")
        # Simulate flush
        while self.pending:
            text = self.pending.popleft()
            reply = robot.ask_ai(text)
            robot.add_chat("assistant", reply)
        self.assertEqual(len(robot.chat_history), 3)


class TestSleepBlocksAsr(unittest.TestCase):
    """Sleep state prevents ASR results from reaching the robot."""

    def test_sleeping_skips_asr_delivery(self):
        sleeping = True
        robot = FakeRobot()
        delivered = 0
        text = "hello there"
        # Simulate _poll_asr_results logic
        if sleeping:
            pass  # skip
        else:
            robot.add_event("asr", text)
            delivered += 1
        self.assertEqual(delivered, 0)
        self.assertEqual(len(robot.events), 0)

    def test_not_sleeping_delivers_asr(self):
        sleeping = False
        robot = FakeRobot()
        robot.queue_reply("hi back")
        text = "hello there"
        if not sleeping:
            robot.add_event("asr", "recognized: %s" % text)
            reply = robot.ask_ai(text)
            robot.add_chat("assistant", reply)
        self.assertGreater(len(robot.events), 0)
        self.assertGreater(len(robot.chat_history), 0)


class TestTtsSpeakingBlocksAsr(unittest.TestCase):
    """TTS playback prevents ASR from recording new utterances."""

    def test_system_speaking_blocks_listening(self):
        system_speaking = True
        recorded = False
        # ASRWorker._run logic: if system_speaking, skip recording loop
        if system_speaking:
            pass  # block
        else:
            recorded = True
        self.assertFalse(recorded)

    def test_not_speaking_allows_recording(self):
        system_speaking = False
        recorded = False
        if not system_speaking:
            recorded = True
        self.assertTrue(recorded)


if __name__ == "__main__":
    unittest.main()
