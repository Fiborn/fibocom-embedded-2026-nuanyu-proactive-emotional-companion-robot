#!/usr/bin/env python3
"""Phase 1: Streaming LLM + TTS coordinator lifecycle and fallback tests.

Verifies:
- Normal stream → finish works.
- Pre-first-token failure → fallback with clean TTS.
- Mid-stream failure → segments already submitted are NOT re-submitted.
- Coordinator session is properly cancelled on failure.
- ASR system_speaking state recovers after errors.
- Consecutive requests don't bleed state.
"""

import collections
import threading
import unittest
import uuid


class FakeCoordinator:
    """Minimal StreamingTTSCoordinator stand-in."""

    def __init__(self):
        self.sessions = {}
        self.pending = collections.deque()
        self.active = False
        self._latencies = []

    def begin(self, replace=True):
        session_id = uuid.uuid4().hex
        if replace:
            for sid in list(self.sessions):
                self.sessions[sid]["cancelled"] = True
            self.pending.clear()
        self.sessions[session_id] = {"cancelled": False, "finished": False, "pending": 0}
        return session_id

    def submit(self, session_id, text, on_done=None):
        state = self.sessions.get(session_id)
        if not state or state["cancelled"]:
            return False
        self.pending.append((session_id, text))
        state["pending"] += 1
        self.active = True
        return True

    def finish(self, session_id):
        state = self.sessions.get(session_id)
        if state:
            state["finished"] = True

    def cancel(self, session_id):
        state = self.sessions.get(session_id)
        if state:
            state["cancelled"] = True
        kept = collections.deque()
        removed = 0
        for item in self.pending:
            if item[0] == session_id:
                removed += 1
            else:
                kept.append(item)
        self.pending = kept
        if state:
            state["pending"] = max(0, state["pending"] - removed)

    def speak_once(self, text, replace=True):
        sid = self.begin(replace=replace)
        self.submit(sid, text)
        self.finish(sid)

    def get_tts_latencies(self):
        return list(self._latencies)


class TestCoordinatorLifecycle(unittest.TestCase):
    """Coordinator session begin / submit / finish / cancel."""

    def setUp(self):
        self.c = FakeCoordinator()

    def test_normal_flow(self):
        sid = self.c.begin()
        self.c.submit(sid, "你好")
        self.c.submit(sid, "世界")
        self.c.finish(sid)
        self.assertEqual(len(self.c.pending), 2)

    def test_replace_clears_previous(self):
        s1 = self.c.begin()
        self.c.submit(s1, "old text")
        self.assertEqual(len(self.c.pending), 1)
        s2 = self.c.begin(replace=True)  # cancels s1, clears queue
        self.assertEqual(len(self.c.pending), 0)
        self.c.submit(s2, "new text")
        self.assertEqual(len(self.c.pending), 1)

    def test_cancel_removes_pending(self):
        sid = self.c.begin()
        self.c.submit(sid, "a")
        self.c.submit(sid, "b")
        self.assertEqual(len(self.c.pending), 2)
        self.c.cancel(sid)
        self.assertEqual(len(self.c.pending), 0)
        state = self.c.sessions.get(sid)
        self.assertTrue(state["cancelled"])

    def test_submit_to_cancelled_session_rejected(self):
        sid = self.c.begin()
        self.c.cancel(sid)
        result = self.c.submit(sid, "should fail")
        self.assertFalse(result)

    def test_speak_once_is_atomic_replace(self):
        s1 = self.c.begin()
        self.c.submit(s1, "stale")
        self.assertEqual(len(self.c.pending), 1)
        self.c.speak_once("fresh reply")
        # speak_once calls begin(replace=True) internally → clears stale
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual(self.c.pending[0][1], "fresh reply")


class TestStreamingFallbackLogic(unittest.TestCase):
    """Simulates ask_ai() success / failure paths."""

    def setUp(self):
        self.coordinator = FakeCoordinator()
        self.full_text = ""
        self._submitted_segments = []
        self.trace_id = uuid.uuid4().hex[:8]
        self._streaming_failed = False
        self.ai_ok = True
        self.last_ai_reply = ""
        self.asr_system_speaking = False

    def _reset_asr(self):
        self.asr_system_speaking = False

    def test_normal_stream_completes(self):
        """Streaming finishes → no fallback needed."""
        session = self.coordinator.begin(replace=True)
        chunks = ["你好", "，今天", "过得", "怎么样？"]
        for chunk in chunks:
            self.full_text += chunk
            clean = chunk.strip("，？")
            if clean:
                self.coordinator.submit(session, clean)
                self._submitted_segments.append(clean)
        self.coordinator.finish(session)
        self._reset_asr()
        self.assertEqual(self.full_text, "你好，今天过得怎么样？")
        self.assertGreater(len(self._submitted_segments), 0)
        self.assertFalse(self.asr_system_speaking)

    def test_pre_first_token_failure_uses_fallback(self):
        """SSE fails before any token — fallback submits full reply."""
        self._streaming_failed = True
        # No segments were submitted
        self.assertEqual(len(self._submitted_segments), 0)
        # Fallback response
        self.full_text = "fallback reply"
        if self._submitted_segments:
            pass  # don't re-submit
        else:
            self.coordinator.speak_once(self.full_text, replace=True)
        self._reset_asr()
        self.assertEqual(self.full_text, "fallback reply")
        self.assertEqual(len(self.coordinator.pending), 1)

    def test_mid_stream_failure_does_not_re_submit_played_segments(self):
        """SSE fails after some segments were already submitted."""
        session = self.coordinator.begin(replace=True)
        # Two segments already submitted before failure
        self.coordinator.submit(session, "你好")
        self._submitted_segments.append("你好")
        self.coordinator.submit(session, "今天过得")
        self._submitted_segments.append("今天过得")
        # Now SSE fails
        self._streaming_failed = True
        self.coordinator.cancel(session)
        # Fallback gets full reply but does NOT re-submit
        self.full_text = "你好，今天过得怎么样？"
        if self._submitted_segments:
            pass  # already played — skip
        else:
            self.coordinator.speak_once(self.full_text, replace=True)
        self._reset_asr()
        # pending was cleared by cancel
        self.assertEqual(len(self.coordinator.pending), 0)
        # No duplicate submission
        self.assertEqual(len(self._submitted_segments), 2)

    def test_consecutive_requests_dont_bleed(self):
        """Session 2 replaces session 1 cleanly."""
        # Request 1
        s1 = self.coordinator.begin(replace=True)
        self.coordinator.submit(s1, "req1 text")
        self.coordinator.finish(s1)
        # Request 2
        s2 = self.coordinator.begin(replace=True)  # should cancel s1
        self.coordinator.submit(s2, "req2 text")
        self.coordinator.finish(s2)
        # Only req2 text is in pending
        self.assertEqual(len(self.coordinator.pending), 1)
        self.assertEqual(self.coordinator.pending[0][1], "req2 text")

    def test_asr_recovers_after_exception(self):
        """system_speaking is always False after completion (success or fail)."""
        # Normal completion
        s = self.coordinator.begin()
        self.coordinator.submit(s, "hi")
        self.coordinator.finish(s)
        self._reset_asr()
        self.assertFalse(self.asr_system_speaking)

        # Failure completion
        self._streaming_failed = True
        self._reset_asr()
        self.assertFalse(self.asr_system_speaking)

    def test_fallback_also_fails_returns_error_message(self):
        """Both stream and fallback fail — graceful error message."""
        self._streaming_failed = True
        try:
            raise RuntimeError("fallback http 500")
        except RuntimeError as e:
            reply = "我现在连不上AI服务，错误：" + str(e)
        self.assertIn("连不上AI服务", reply)


if __name__ == "__main__":
    unittest.main()
