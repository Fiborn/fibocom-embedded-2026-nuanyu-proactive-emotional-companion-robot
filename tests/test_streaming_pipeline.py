#!/usr/bin/env python3
import os
import sys
import threading
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.core.reminder_gate import looks_like_reminder
from src.tts.streaming_pipeline import (
    IncrementalSentenceSegmenter,
    StreamingTTSCoordinator,
)


class SentenceSegmenterTest(unittest.TestCase):
    def test_handles_punctuation_split_across_token_chunks(self):
        segmenter = IncrementalSentenceSegmenter(min_chars=4, soft_chars=8, max_chars=20)
        self.assertEqual([], segmenter.feed("先说结"))
        self.assertEqual(["先说结论。"], segmenter.feed("论。然后解释原因"))
        self.assertEqual(["然后解释原因"], segmenter.flush())

    def test_weak_punctuation_waits_for_enough_context(self):
        segmenter = IncrementalSentenceSegmenter(min_chars=4, soft_chars=8, max_chars=20)
        # The opening sentence is cut at the first punctuation mark so the
        # greeting starts synthesising immediately (fast first-sentence onset).
        self.assertEqual(["好的，"], segmenter.feed("好的，"))
        # After that, a weak comma only ends a segment once the fragment has
        # reached min_chars — a two-character fragment waits for more text.
        self.assertEqual([], segmenter.feed("好，"))
        self.assertEqual(["好，我们现在开始，"], segmenter.feed("我们现在开始，后面继续"))
        self.assertEqual(["后面继续"], segmenter.flush())

    def test_long_text_is_forced_into_bounded_segments(self):
        segmenter = IncrementalSentenceSegmenter(min_chars=4, soft_chars=8, max_chars=10)
        parts = segmenter.feed("这是一个没有任何标点而且比较长的连续句子")
        parts.extend(segmenter.flush())
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(len(part) <= 10 for part in parts))


class CoordinatorTest(unittest.TestCase):
    def test_preserves_order_and_activity_across_all_segments(self):
        spoken = []
        activity = []
        complete = threading.Event()

        def speak_async(text, callback):
            spoken.append(text)
            threading.Timer(0.01, lambda: callback(True, "")).start()
            return True

        def on_activity(active):
            activity.append(active)
            if not active:
                complete.set()

        coordinator = StreamingTTSCoordinator(
            speak_async, on_activity=on_activity, item_timeout=1)
        session = coordinator.begin()
        self.assertTrue(coordinator.submit(session, "第一句。"))
        self.assertTrue(coordinator.submit(session, "第二句。"))
        coordinator.finish(session)
        self.assertTrue(complete.wait(1))
        self.assertEqual(["第一句。", "第二句。"], spoken)
        self.assertEqual([True, False], activity)
        coordinator.close()

    def test_replacing_session_drops_queued_old_segments(self):
        spoken = []
        dispatched = threading.Event()
        release_first = threading.Event()
        all_done = threading.Event()

        def speak_async(text, callback):
            spoken.append(text)
            if text == "旧一":
                # The dispatch loop hands each segment to the provider without
                # waiting for playback, so hold the loop here: that is what
                # keeps "旧二" in the queue while the old session is replaced.
                dispatched.set()
                release_first.wait(2)
            callback(True, "")
            return True

        coordinator = StreamingTTSCoordinator(
            speak_async, on_activity=lambda active: all_done.set() if not active else None,
            item_timeout=1)
        old = coordinator.begin()
        coordinator.submit(old, "旧一")
        self.assertTrue(dispatched.wait(1), "first segment was never dispatched")
        coordinator.submit(old, "旧二")
        coordinator.finish(old)
        new = coordinator.begin(replace=True)
        coordinator.submit(new, "新回答")
        coordinator.finish(new)
        release_first.set()
        self.assertTrue(all_done.wait(1))
        self.assertEqual(["旧一", "新回答"], spoken)
        coordinator.close()

    def test_queue_pressure_never_creates_an_unbounded_tts_item(self):
        release = threading.Event()

        def speak_async(text, callback):
            threading.Thread(
                target=lambda: (release.wait(1), callback(True, "")),
                daemon=True).start()
            return True

        coordinator = StreamingTTSCoordinator(
            speak_async, max_pending=1, max_merged_chars=12, item_timeout=1)
        session = coordinator.begin()
        with coordinator._condition:
            self.assertTrue(coordinator.submit(session, "第一段"))
            self.assertFalse(
                coordinator.submit(session, "这是一段不能继续合并的长文本"))
            self.assertTrue(
                all(len(item[1]) <= 12 for item in coordinator._pending))
        coordinator.finish(session)
        release.set()
        coordinator.close()


class ReminderGateTest(unittest.TestCase):
    def test_normal_chat_does_not_trigger_second_ai_call(self):
        self.assertFalse(looks_like_reminder("今天感觉有点累"))
        self.assertFalse(looks_like_reminder("你觉得这个方案怎么样"))

    def test_explicit_or_timed_action_triggers(self):
        self.assertTrue(looks_like_reminder("提醒我半小时后喝水"))
        self.assertTrue(looks_like_reminder("明天下午三点开会"))


if __name__ == "__main__":
    unittest.main()
