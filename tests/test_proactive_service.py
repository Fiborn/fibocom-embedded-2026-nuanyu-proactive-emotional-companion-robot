#!/usr/bin/env python3
"""Tests for ProactiveServiceImpl — rule-based proactive decision engine.

All tests use a simulated clock (no dependency on time.time) so they
run deterministically and instantly.

Coverage:
  - Global enable/disable switch
  - Proactivity level scaling (1–10)
  - Greeting on user entry (USER_PRESENT, SESSION_STARTED)
  - Emotion-based care (sad / fear / angry streaks)
  - Silence / sit timeout care
  - Reminder delivery
  - Cooldown enforcement
  - Daily per-user limits
  - Dedup (same reason, short window)
  - Rejection recording and backoff
  - health() structure
  - close() cleanup
  - NoOpProactiveService backward-compat contract
"""

from __future__ import annotations
import copy
import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.domain.events import RuntimeEvent, EventType
from src.ports.proactive import ProactiveDecision, ProactiveService
from src.services.proactive_service import (
    NoOpProactiveService,
    ProactiveServiceImpl,
    CARE_EMOTIONS,
    EMOTION_STREAK_THRESHOLD,
)


# ═══════════════════════════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════════════════════════

class SimClock:
    """Simulated clock — deterministic, instant, manually advanced."""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t

    def set(self, t: float) -> float:
        self._t = t
        return self._t

    @property
    def now(self) -> float:
        return self._t


def make_event(event_type: str, user_id: str = "u1",
               timestamp: float = 0.0, source: str = "test",
               payload: dict | None = None,
               session_id: str = "s1") -> RuntimeEvent:
    return RuntimeEvent(
        event_id="ev-%s-%d" % (event_type, int(timestamp * 1000)),
        event_type=event_type,
        timestamp=timestamp,
        source=source,
        user_id=user_id,
        session_id=session_id,
        payload=payload or {},
    )


# ═══════════════════════════════════════════════════════════════════
#  NoOpProactiveService  — backward-compat contract
# ═══════════════════════════════════════════════════════════════════

class TestNoOpBackwardCompat(unittest.TestCase):
    """Ensure the NoOp stub still satisfies the Phase 2A contract
    (test_phase2_interfaces.py depends on it)."""

    def setUp(self):
        self.svc = NoOpProactiveService()

    def test_always_returns_none(self):
        evt = make_event(EventType.USER_PRESENT)
        self.assertIsNone(self.svc.handle_event(evt))

    def test_cooldown_reset(self):
        self.svc.reset_cooldown(5)
        self.assertGreater(self.svc.get_cooldown_remaining(), 0)

    def test_rejection_counting(self):
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        h = self.svc.health()
        self.assertEqual(h["rejection_count"], 2)

    def test_set_level_clamps(self):
        self.svc.set_proactivity_level(15)
        h = self.svc.health()
        self.assertEqual(h["level"], 10)
        self.svc.set_proactivity_level(-2)
        h = self.svc.health()
        self.assertEqual(h["level"], 1)

    def test_health_backend(self):
        self.assertEqual(self.svc.health()["backend"], "noop")

    def test_close_no_error(self):
        self.svc.close()  # must not raise


# ═══════════════════════════════════════════════════════════════════
#  ProactiveServiceImpl  —  global switch & level
# ═══════════════════════════════════════════════════════════════════

class TestGlobalSwitch(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_disabled_returns_none_for_greeting(self):
        self.svc.set_enabled(False)
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        self.assertIsNone(self.svc.handle_event(evt))

    def test_disabled_returns_none_for_emotion(self):
        self.svc.set_enabled(False)
        for _ in range(EMOTION_STREAK_THRESHOLD):
            evt = make_event(EventType.EMOTION_CHANGED,
                           payload={"emotion": "sad"},
                           timestamp=self.clock.now)
            self.assertIsNone(self.svc.handle_event(evt))
            self.clock.advance(0.1)

    def test_disabled_returns_none_for_reminder(self):
        self.svc.set_enabled(False)
        evt = make_event(EventType.REMINDER_DUE,
                       payload={"text": "吃药"},
                       timestamp=self.clock.now)
        self.assertIsNone(self.svc.handle_event(evt))

    def test_enabled_by_default(self):
        self.assertTrue(self.svc.enabled)

    def test_enabled_returns_decision_for_greeting(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertTrue(d.should_act)
        self.assertEqual(d.reason, "greeting")

    def test_set_enabled_toggle(self):
        self.svc.set_enabled(False)
        self.assertFalse(self.svc.enabled)
        self.svc.set_enabled(True)
        self.assertTrue(self.svc.enabled)


class TestProactivityLevel(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)

    def test_default_level_is_5(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        self.assertEqual(svc.level, 5)

    def test_set_level_clamps_low(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        svc.set_proactivity_level(-5)
        self.assertEqual(svc.level, 1)

    def test_set_level_clamps_high(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        svc.set_proactivity_level(99)
        self.assertEqual(svc.level, 10)

    def test_level_1_daily_max_is_low(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        svc.set_proactivity_level(1)
        # Greet once
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        svc.handle_event(evt)
        self.clock.advance(200)  # past cooldown
        # Second greet should be blocked by daily limit at level 1 (max=2)
        svc2 = ProactiveServiceImpl(time_fn=self.clock)
        svc2.set_proactivity_level(1)
        # Actually test daily limit directly
        self.assertEqual(svc._daily_max(), 2)

    def test_level_10_daily_max_is_high(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        svc.set_proactivity_level(10)
        self.assertEqual(svc._daily_max(), 30)

    def test_level_5_daily_max_is_mid(self):
        svc = ProactiveServiceImpl(time_fn=self.clock)
        svc.set_proactivity_level(5)
        self.assertEqual(svc._daily_max(), 12)

    def test_level_affects_cooldown(self):
        svc_low = ProactiveServiceImpl(time_fn=self.clock)
        svc_low.set_proactivity_level(1)
        svc_high = ProactiveServiceImpl(time_fn=self.clock)
        svc_high.set_proactivity_level(10)

        # Get greeting decisions to compare cooldowns
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d_low = svc_low.handle_event(evt)
        self.clock.advance(0.1)
        evt2 = make_event(EventType.USER_PRESENT, user_id="u2",
                        timestamp=self.clock.now)
        d_high = svc_high.handle_event(evt2)

        # Level 10 should have shorter cooldown than level 1
        self.assertLess(d_high.cooldown_seconds, d_low.cooldown_seconds)


# ═══════════════════════════════════════════════════════════════════
#  Greeting rules
# ═══════════════════════════════════════════════════════════════════

class TestGreetingRules(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_user_present_triggers_greeting(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "greeting")
        self.assertEqual(d.priority, 8)
        self.assertEqual(d.suggested_scene, "greeting")

    def test_session_started_triggers_greeting(self):
        evt = make_event(EventType.SESSION_STARTED, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "greeting")

    def test_second_detection_no_greeting(self):
        evt1 = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d1 = self.svc.handle_event(evt1)
        self.assertIsNotNone(d1)

        self.clock.advance(200)  # past cooldown
        evt2 = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNone(d2)

    def test_different_users_each_get_greeting(self):
        evt1 = make_event(EventType.USER_PRESENT, user_id="u1",
                        timestamp=self.clock.now)
        d1 = self.svc.handle_event(evt1)
        self.assertIsNotNone(d1)

        self.clock.advance(200)
        evt2 = make_event(EventType.USER_PRESENT, user_id="u2",
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNotNone(d2)
        self.assertEqual(d2.reason, "greeting")

    def test_default_user_when_user_id_empty(self):
        evt = make_event(EventType.USER_PRESENT, user_id="",
                        timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "greeting")

    def test_greeting_respects_cooldown(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)

        # Immediately after — should be blocked by cooldown
        evt2 = make_event(EventType.SESSION_STARTED, user_id="u2",
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNone(d2)


# ═══════════════════════════════════════════════════════════════════
#  Emotion care rules
# ═══════════════════════════════════════════════════════════════════

class TestEmotionCareRules(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def _feed_emotions(self, emotions: list, user_id: str = "u1"):
        """Feed a list of emotions; return the first non-None decision or None."""
        for emo in emotions:
            self.clock.advance(1.0)
            evt = make_event(EventType.EMOTION_CHANGED, user_id=user_id,
                           payload={"emotion": emo},
                           timestamp=self.clock.now)
            d = self.svc.handle_event(evt)
            if d is not None:
                return d
        return None

    def test_single_sad_no_care(self):
        d = self._feed_emotions(["sad"])
        self.assertIsNone(d)

    def test_two_sad_no_care(self):
        d = self._feed_emotions(["sad", "sad"])
        self.assertIsNone(d)

    def test_three_consecutive_sad_triggers_care(self):
        d = self._feed_emotions(["sad", "sad", "sad"])
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "emotion_care:sad")
        self.assertEqual(d.priority, 7)
        self.assertEqual(d.suggested_scene, "comfort_sad")

    def test_three_consecutive_fear_triggers_care(self):
        d = self._feed_emotions(["fear", "fear", "fear"])
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "emotion_care:fear")
        self.assertEqual(d.suggested_scene, "comfort_fear")

    def test_three_consecutive_angry_triggers_care(self):
        d = self._feed_emotions(["angry", "angry", "angry"])
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "emotion_care:angry")
        self.assertEqual(d.suggested_scene, "comfort_angry")

    def test_chinese_emotions_trigger_care(self):
        d = self._feed_emotions(["悲伤", "悲伤", "悲伤"])
        self.assertIsNotNone(d)
        self.assertIn("悲伤", d.reason)

    def test_mixed_negative_emotions_trigger_care(self):
        """Three consecutive negative emotions (different types) should trigger."""
        d = self._feed_emotions(["sad", "fear", "sad"])
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "emotion_care:sad")  # sad is dominant

    def test_happy_interrupts_streak(self):
        """Happy emotion resets the negative streak."""
        d = self._feed_emotions(["sad", "sad", "happy", "sad"])
        self.assertIsNone(d)  # streak broken

    def test_emotion_after_cooldown_fires_again(self):
        d = self._feed_emotions(["sad", "sad", "sad"])
        self.assertIsNotNone(d)

        self.clock.advance(400)  # well past emotion care cooldown (~180s at level 5)
        d2 = self._feed_emotions(["sad", "sad", "sad"])
        self.assertIsNotNone(d2)


# ═══════════════════════════════════════════════════════════════════
#  Silence / sit timeout rules
# ═══════════════════════════════════════════════════════════════════

class TestSilenceCareRules(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_silence_timeout_no_user_present(self):
        """Can't fire silence care if user was never detected."""
        evt = make_event(EventType.SILENCE_TIMEOUT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNone(d)

    def test_silence_timeout_with_interaction_ok(self):
        """With recent interaction, silence care should not fire."""
        # Mark user present
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))
        self.clock.advance(200)  # past cooldown
        # User spoke recently
        self.svc.handle_event(
            make_event(EventType.USER_SPEECH, timestamp=self.clock.now))
        # Silence timeout event — but interaction was recent
        evt = make_event(EventType.SILENCE_TIMEOUT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNone(d)

    def test_silence_timeout_triggers_care(self):
        """User present for a long time with no interaction."""
        # Mark user present first
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))
        self.clock.advance(200)  # past greeting cooldown

        # Advance time past silence threshold
        self.clock.advance(400)  # total > 300s base at level 5

        evt = make_event(EventType.SILENCE_TIMEOUT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "silence_care")
        self.assertEqual(d.priority, 4)
        self.assertEqual(d.suggested_scene, "light_chat")

    def test_sit_timeout_triggers_care(self):
        """Long sitting without interaction."""
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))
        self.clock.advance(200)

        self.clock.advance(700)  # > 600s base

        evt = make_event(EventType.SIT_TIMEOUT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "silence_care")


# ═══════════════════════════════════════════════════════════════════
#  Reminder rules
# ═══════════════════════════════════════════════════════════════════

class TestReminderRules(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_reminder_due_fires(self):
        evt = make_event(EventType.REMINDER_DUE,
                       payload={"text": "该吃药了", "title": "吃药提醒"},
                       timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "reminder")
        self.assertEqual(d.priority, 9)
        self.assertEqual(d.suggested_scene, "reminder")
        self.assertIn("该吃药了", d.suggested_prompt)

    def test_reminder_with_title_fallback(self):
        evt = make_event(EventType.REMINDER_DUE,
                       payload={"title": "喝水"},
                       timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertIn("喝水", d.suggested_prompt)

    def test_reminder_empty_payload(self):
        evt = make_event(EventType.REMINDER_DUE, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "reminder")


# ═══════════════════════════════════════════════════════════════════
#  Cooldown enforcement
# ═══════════════════════════════════════════════════════════════════

class TestCooldownEnforcement(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_cooldown_blocks_during_period(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)

        # Immediately — cooldown should be active
        self.assertGreater(self.svc.get_cooldown_remaining(), 0)

        evt2 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "喝水"},
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNone(d2)

    def test_cooldown_expires(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        self.svc.handle_event(evt)

        cooldown = self.svc.get_cooldown_remaining()
        self.clock.advance(cooldown + 1.0)

        self.assertEqual(self.svc.get_cooldown_remaining(), 0.0)

    def test_reset_cooldown_with_value(self):
        self.svc.reset_cooldown(10)
        remaining = self.svc.get_cooldown_remaining()
        self.assertAlmostEqual(remaining, 10.0, delta=0.1)

    def test_reset_cooldown_to_zero(self):
        self.svc.reset_cooldown(60)
        self.assertGreater(self.svc.get_cooldown_remaining(), 0)
        self.svc.reset_cooldown()  # clear to 0
        self.assertEqual(self.svc.get_cooldown_remaining(), 0.0)

    def test_after_cooldown_expires_new_decision_allowed(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d1 = self.svc.handle_event(evt)
        self.assertIsNotNone(d1)

        self.clock.advance(d1.cooldown_seconds + 1.0)

        evt2 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "x"},
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNotNone(d2)


# ═══════════════════════════════════════════════════════════════════
#  Daily limit enforcement
# ═══════════════════════════════════════════════════════════════════

class TestDailyLimit(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)
        self.svc.set_proactivity_level(1)  # daily max = 2

    def test_daily_limit_blocks_after_threshold(self):
        # Fire 1: greeting — at level 1, cooldown ~600s
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.clock.advance(700)

        # Fire 2: reminder
        evt2 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "喝水"},
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNotNone(d2)
        self.clock.advance(700)

        # Fire 3: should be blocked (daily max = 2 at level 1)
        evt3 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "再喝水"},
                        timestamp=self.clock.now)
        d3 = self.svc.handle_event(evt3)
        self.assertIsNone(d3)

    def test_daily_limit_per_user(self):
        # u1 fires twice
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, user_id="u1",
                     timestamp=self.clock.now))
        self.clock.advance(700)
        self.svc.handle_event(
            make_event(EventType.REMINDER_DUE, user_id="u1",
                     payload={"text": "a"}, timestamp=self.clock.now))
        self.clock.advance(700)

        # u1 third attempt — blocked
        self.assertIsNone(
            self.svc.handle_event(
                make_event(EventType.REMINDER_DUE, user_id="u1",
                         payload={"text": "b"}, timestamp=self.clock.now)))

        # u2 first attempt — allowed (separate counter)
        self.clock.advance(700)
        d = self.svc.handle_event(
            make_event(EventType.REMINDER_DUE, user_id="u2",
                     payload={"text": "c"}, timestamp=self.clock.now))
        self.assertIsNotNone(d)


# ═══════════════════════════════════════════════════════════════════
#  Dedup enforcement
# ═══════════════════════════════════════════════════════════════════

class TestDedupEnforcement(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_same_reason_blocked_within_window(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)

        self.clock.advance(200)  # past cooldown, but within dedup window

        # Same event type, same user — should be deduped
        evt2 = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNone(d2)

    def test_different_user_not_deduped(self):
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, user_id="u1",
                     timestamp=self.clock.now))
        self.clock.advance(200)

        evt2 = make_event(EventType.USER_PRESENT, user_id="u2",
                        timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNotNone(d2)

    def test_dedup_window_expires(self):
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))
        self.clock.advance(200)  # wait out window

        # Greeting won't fire again for same user, so use reminder
        self.clock.advance(200)
        evt = make_event(EventType.REMINDER_DUE,
                       payload={"text": "x"},
                       timestamp=self.clock.now)
        self.svc.handle_event(evt)
        self.clock.advance(200)

        evt2 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "y"},
                        timestamp=self.clock.now)
        # This should be allowed after dedup window passes
        # （But reminder also triggers again — dedup is per-user per-reason）
        # Wait long enough for dedup window to pass
        self.clock.advance(100)
        d = self.svc.handle_event(evt2)
        # Dedup window 60s — this passed after 100s
        self.assertIsNotNone(d)

    def test_emotion_care_different_subtype_same_family_blocked(self):
        """emotion_care:sad and emotion_care:fear share the same family."""
        for _ in range(EMOTION_STREAK_THRESHOLD):
            self.clock.advance(1.0)
            self.svc.handle_event(
                make_event(EventType.EMOTION_CHANGED,
                         payload={"emotion": "sad"},
                         timestamp=self.clock.now))

        self.clock.advance(200)

        # Now try fear — same emotion_care family, should be deduped
        for _ in range(EMOTION_STREAK_THRESHOLD):
            self.clock.advance(1.0)
            self.svc.handle_event(
                make_event(EventType.EMOTION_CHANGED,
                         payload={"emotion": "fear"},
                         timestamp=self.clock.now))

        self.assertIsNone(
            self.svc.handle_event(
                make_event(EventType.EMOTION_CHANGED,
                         payload={"emotion": "fear"},
                         timestamp=self.clock.now)))


# ═══════════════════════════════════════════════════════════════════
#  Rejection recording & backoff
# ═══════════════════════════════════════════════════════════════════

class TestRejectionBackoff(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_record_rejection_increases_count(self):
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        h = self.svc.health()
        self.assertEqual(h["rejection_count"].get("u1", 0), 2)

    def test_record_rejection_increases_streak(self):
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        self.assertEqual(self.svc.rejection_streak.get("u1", 0), 3)

    def test_rejection_increases_cooldown(self):
        before = self.svc.get_cooldown_remaining()
        self.svc.record_rejection("u1")
        after = self.svc.get_cooldown_remaining()
        self.assertGreater(after, before)

    def test_three_rejections_block_low_priority(self):
        self.svc.record_rejection("u1")  # streak=1
        self.svc.record_rejection("u1")  # streak=2
        self.svc.record_rejection("u1")  # streak=3
        self.clock.advance(3000)  # well past any cooldown

        # Now user enters — greeting (priority 8) should pass
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, user_id="u1",
                     timestamp=self.clock.now))
        self.clock.advance(1000)  # past greeting cooldown

        # Silence care (priority 4 < 8) should be blocked by streak ≥ 3
        evt = make_event(EventType.SILENCE_TIMEOUT, user_id="u1",
                       timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNone(d)  # blocked by rejection streak ≥ 3

    def test_high_priority_passes_despite_rejections(self):
        for _ in range(4):
            self.svc.record_rejection("u1")
        # Rejection backoff: 120s * 1.0 * 1.5^4 = 120 * 5.06 ≈ 608s at level 5
        self.clock.advance(800)

        # Reminder (priority 9) should still pass
        evt = make_event(EventType.REMINDER_DUE, user_id="u1",
                       payload={"text": "重要提醒"},
                       timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)

    def test_interaction_reduces_rejection_streak(self):
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        self.assertEqual(self.svc.rejection_streak.get("u1", 0), 3)

        # User speaks — reduces streak by 1
        self.svc.handle_event(
            make_event(EventType.USER_SPEECH, user_id="u1",
                     timestamp=self.clock.now))
        self.assertEqual(self.svc.rejection_streak.get("u1", 0), 2)

    def test_wake_word_reduces_rejection_streak(self):
        self.svc.record_rejection("u1")
        self.svc.record_rejection("u1")
        self.assertEqual(self.svc.rejection_streak["u1"], 2)

        self.svc.handle_event(
            make_event(EventType.WAKE_WORD, user_id="u1",
                     timestamp=self.clock.now))
        self.assertEqual(self.svc.rejection_streak["u1"], 1)


# ═══════════════════════════════════════════════════════════════════
#  health() contract
# ═══════════════════════════════════════════════════════════════════

class TestHealth(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_health_required_keys(self):
        h = self.svc.health()
        for key in ("backend", "enabled", "level", "cooldown_remaining_s",
                     "rejection_count", "rejection_streak"):
            self.assertIn(key, h)

    def test_health_backend_is_rule_based(self):
        self.assertEqual(self.svc.health()["backend"], "rule_based")

    def test_health_reflects_state_after_events(self):
        self.svc.record_rejection("u1")
        # Advance past the rejection cooldown so greeting can fire
        self.clock.advance(300)
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))

        h = self.svc.health()
        self.assertGreater(h["cooldown_remaining_s"], 0)
        self.assertIn("u1", h["rejection_count"])
        self.assertIn("u1", h["greeted_users"])


# ═══════════════════════════════════════════════════════════════════
#  close() cleanup
# ═══════════════════════════════════════════════════════════════════

class TestClose(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_close_clears_state(self):
        # Populate some state
        self.svc.handle_event(
            make_event(EventType.USER_PRESENT, timestamp=self.clock.now))
        self.svc.record_rejection("u1")
        for _ in range(EMOTION_STREAK_THRESHOLD):
            self.clock.advance(1.0)
            self.svc.handle_event(
                make_event(EventType.EMOTION_CHANGED,
                         payload={"emotion": "sad"},
                         timestamp=self.clock.now))

        h_before = self.svc.health()
        self.assertIn("u1", h_before["greeted_users"])

        self.svc.close()

        h_after = self.svc.health()
        self.assertEqual(h_after["greeted_users"], [])
        self.assertEqual(h_after["rejection_count"], {})

    def test_close_idempotent(self):
        self.svc.close()
        self.svc.close()  # must not raise

    def test_close_does_not_break_new_events(self):
        self.svc.close()
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)


# ═══════════════════════════════════════════════════════════════════
#  Abstract interface conformance
# ═══════════════════════════════════════════════════════════════════

class TestAbstractConformance(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1000.0)
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_is_instance_of_proactive_service(self):
        self.assertIsInstance(self.svc, ProactiveService)

    def test_handle_event_returns_decision_or_none(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        result = self.svc.handle_event(evt)
        self.assertTrue(result is None or isinstance(result, ProactiveDecision))

    def test_proactive_decision_fields(self):
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertIsInstance(d, ProactiveDecision)
        self.assertTrue(d.should_act)
        self.assertIsInstance(d.reason, str)
        self.assertGreater(d.reason, "")
        self.assertGreaterEqual(d.priority, 1)
        self.assertLessEqual(d.priority, 10)
        self.assertGreater(d.cooldown_seconds, 0)

    def test_no_live_imports_of_tts_or_llm(self):
        """Proactive service must not import TTS, LLM, vision, or frontend modules."""
        import inspect
        import src.services.proactive_service as mod
        source = inspect.getsource(mod)
        # Check that we don't import anything from the forbidden modules
        forbidden = [
            "tts", "asr", "vision", "fibo_tts", "deepseek",
            "flask", "jinja", "http.server",
            "doubao", "piper", "onnx", "cv2", "numpy",
        ]
        lines_lower = source.lower()
        for keyword in forbidden:
            # Allow mentions in comments/docstrings about what NOT to do
            # Simple check: if keyword appears in an import line
            self.assertNotIn(
                "import %s" % keyword, lines_lower,
                "ProactiveServiceImpl must not import %s" % keyword
            )
            self.assertNotIn(
                "from %s" % keyword, lines_lower,
                "ProactiveServiceImpl must not import from %s" % keyword
            )


# ═══════════════════════════════════════════════════════════════════
#  Integration-like scenarios
# ═══════════════════════════════════════════════════════════════════

class TestIntegrationScenarios(unittest.TestCase):

    def setUp(self):
        self.clock = SimClock(1700000000.0)  # ~ Nov 2023
        self.svc = ProactiveServiceImpl(time_fn=self.clock)

    def test_full_session_flow(self):
        """Simulate a realistic session: enter → emotion → silence → reminder."""
        # 1. User enters → greeting
        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "greeting")

        self.clock.advance(d.cooldown_seconds + 1)

        # 2. User shows sad for a while → emotion care
        for _ in range(EMOTION_STREAK_THRESHOLD):
            self.clock.advance(2.0)
            self.svc.handle_event(
                make_event(EventType.EMOTION_CHANGED,
                         payload={"emotion": "sad"},
                         timestamp=self.clock.now))
        # (emotion care already fired on the 3rd one)

        self.clock.advance(300)

        # 3. Reminder fires
        evt3 = make_event(EventType.REMINDER_DUE,
                        payload={"text": "午饭时间"},
                        timestamp=self.clock.now)
        d3 = self.svc.handle_event(evt3)
        self.assertIsNotNone(d3)
        self.assertEqual(d3.reason, "reminder")

    def test_level_10_aggressive_mode(self):
        """At level 10: short cooldowns, high daily max, low thresholds."""
        self.svc.set_proactivity_level(10)

        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        # Level 10 cooldown should be short
        self.assertLess(d.cooldown_seconds, 80)

        self.clock.advance(d.cooldown_seconds + 1)

        # Silence should fire quickly
        self.clock.advance(200)
        evt2 = make_event(EventType.SILENCE_TIMEOUT, timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNotNone(d2)
        self.assertEqual(d2.reason, "silence_care")

    def test_level_1_conservative_mode(self):
        """At level 1: long cooldowns, low daily max, high thresholds."""
        self.svc.set_proactivity_level(1)

        evt = make_event(EventType.USER_PRESENT, timestamp=self.clock.now)
        d = self.svc.handle_event(evt)
        self.assertIsNotNone(d)
        # Level 1 cooldown should be long (≥ 2 min base × 2.0 factor)
        self.assertGreater(d.cooldown_seconds, 200)

        self.clock.advance(d.cooldown_seconds + 1)

        # At level 1, silence threshold = 300s × 2.0 = 600s
        # Advance only 200s → below threshold → should NOT fire
        self.clock.advance(200)
        evt2 = make_event(EventType.SILENCE_TIMEOUT, timestamp=self.clock.now)
        d2 = self.svc.handle_event(evt2)
        self.assertIsNone(d2)

    def test_rapid_alternating_events_not_spam(self):
        """Rapidly alternating event types should not flood decisions."""
        count = 0
        for _ in range(20):
            self.clock.advance(5)
            evt = make_event(EventType.EMOTION_CHANGED,
                           payload={"emotion": "happy"},
                           timestamp=self.clock.now)
            d = self.svc.handle_event(evt)
            if d is not None:
                count += 1
        # happy should never trigger care, so 0 decisions
        self.assertEqual(count, 0)


# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
