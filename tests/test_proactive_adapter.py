#!/usr/bin/env python3
"""ProactiveInteractionAdapter — queueing contract, and the reminder schedule.

Two things are covered here:

* ``src/services/proactive_adapter.py`` — the adapter that turns perception
  state into ``ProactiveDecision`` objects.  It never speaks and never calls the
  LLM itself; it only decides, and the runtime reads the decision back with
  ``poll_decision()``.  The *rules* (greeting / emotion care / silence /
  reminder, cooldowns, daily limits, dedup) live in ``ProactiveServiceImpl`` and
  are covered by ``test_proactive_service.py``.

* ``ReminderManager`` — the schedule store the adapter is fed from.  It now
  lives in ``app/nuanyu_web.py``, which imports ``termios`` and therefore cannot
  be imported on a non-POSIX development machine.  The class itself only needs
  ``os``/``json``/``threading``/``time``/``re``, so it is read out of the source
  with ``ast`` and executed standalone — the same technique the other
  ``*_contract.py`` files in this directory use.

No board hardware, models or network are required.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from src.ports.proactive import ProactiveDecision
from src.services.proactive_adapter import (
    NoOpProactiveAdapter,
    ProactiveInteractionAdapter,
)
from src.services.proactive_service import ProactiveServiceImpl

MAIN_SOURCE = (_APP / "nuanyu_web.py").read_text(encoding="utf-8")
ADAPTER_SOURCE_PATH = _APP / "src" / "services" / "proactive_adapter.py"


def load_reminder_manager():
    """Return the real ReminderManager class without importing nuanyu_web."""
    tree = ast.parse(MAIN_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ReminderManager":
            module = ast.fix_missing_locations(
                ast.Module(body=[node], type_ignores=[]))
            namespace = {
                "os": os, "json": json, "threading": threading, "time": time,
            }
            exec(compile(module, "nuanyu_web.py", "exec"), namespace)
            return namespace["ReminderManager"]
    raise AssertionError("ReminderManager not found in nuanyu_web.py")


ReminderManager = load_reminder_manager()


# ═══════════════════════════════════════════════════════════════════
#  ReminderManager — schedule store
# ═══════════════════════════════════════════════════════════════════

class ReminderManagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rem_test_")
        self.path = pathlib.Path(self._tmp.name) / "schedule.json"
        self.mgr = ReminderManager(path=str(self.path))

    def tearDown(self):
        self._tmp.cleanup()

    def _write_schedule(self, items):
        self.path.write_text(json.dumps(items, ensure_ascii=False),
                             encoding="utf-8")
        self.mgr = ReminderManager(path=str(self.path))

    def test_missing_schedule_file_is_an_empty_schedule(self):
        self.assertEqual(self.mgr.get_active(), [])
        self.assertEqual(self.mgr.check_due(), [])

    def test_add_persists_a_pending_entry(self):
        self.mgr.add("新提醒", time.time() + 600, source="test")

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["content"], "新提醒")
        self.assertEqual(stored[0]["source"], "test")
        self.assertFalse(stored[0]["done"])
        self.assertEqual(len(self.mgr.get_active()), 1)

    def test_future_reminder_is_not_due(self):
        self._write_schedule([{
            "content": "test", "trigger_time": time.time() + 3600,
            "source": "ai", "done": False,
        }])
        self.assertEqual(self.mgr.check_due(), [])
        self.assertEqual(len(self.mgr.get_active()), 1)

    def test_past_reminder_is_returned_once_and_marked_done(self):
        self._write_schedule([{
            "content": "到期提醒", "trigger_time": time.time() - 600,
            "source": "ai", "done": False,
        }])

        due = self.mgr.check_due()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["content"], "到期提醒")
        # Reading the due list fires the reminder: it must not fire twice.
        self.assertEqual(self.mgr.check_due(), [])

    def test_fired_reminder_is_persisted_as_done(self):
        self._write_schedule([{
            "content": "fire me", "trigger_time": time.time() - 600,
            "source": "ai", "done": False,
        }])
        self.mgr.check_due()

        reopened = ReminderManager(path=str(self.path))
        self.assertEqual(reopened.get_active(), [])
        self.assertEqual(reopened.check_due(), [])

    def test_done_entries_are_never_active(self):
        self._write_schedule([
            {"content": "past", "trigger_time": time.time() - 600,
             "source": "ai", "done": False},
            {"content": "future", "trigger_time": time.time() + 600,
             "source": "ai", "done": False},
            {"content": "done", "trigger_time": time.time() - 600,
             "source": "ai", "done": True},
        ])
        self.assertEqual([r["content"] for r in self.mgr.get_active()],
                         ["past", "future"])

    def test_corrupt_schedule_file_degrades_to_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(ReminderManager(path=str(self.path)).get_active(), [])


# ═══════════════════════════════════════════════════════════════════
#  NoOpProactiveAdapter — the fallback when no service is wired
# ═══════════════════════════════════════════════════════════════════

class NoOpAdapterTest(unittest.TestCase):
    def test_never_reports_a_decision(self):
        adapter = NoOpProactiveAdapter()
        for _ in range(5):
            adapter.tick({"face_detected": True}, [{"content": "x"}],
                         False, False, False)
            self.assertIsNone(adapter.poll_decision())

    def test_health_identifies_the_noop_backend(self):
        self.assertEqual(NoOpProactiveAdapter().health()["backend"], "noop")


# ═══════════════════════════════════════════════════════════════════
#  ProactiveInteractionAdapter — decision queue
# ═══════════════════════════════════════════════════════════════════

class AdapterQueueTest(unittest.TestCase):
    def setUp(self):
        self.svc = ProactiveServiceImpl()
        self.svc.reset_cooldown(0)
        self.adapter = ProactiveInteractionAdapter(self.svc)

    def _due_reminder(self, content="喝水"):
        return [{"content": content, "trigger_time": 0}]

    def test_due_reminder_is_queued_for_the_runtime(self):
        self.adapter.tick({}, self._due_reminder(), False, False, False)

        decision = self.adapter.poll_decision()
        self.assertIsNotNone(decision)
        self.assertTrue(decision.should_act)
        self.assertEqual(decision.reason, "reminder")
        self.assertEqual(decision.priority, 9)

    def test_reminder_body_reaches_the_decision(self):
        # Regression: the adapter used to put the reminder body under the
        # payload key "content" while ProactiveServiceImpl._decide_reminder
        # reads payload["text"]. The mismatch silently blanked the prompt, so
        # every reminder spoke the generic fallback line instead of the text
        # the user actually scheduled.
        self.adapter.tick({}, self._due_reminder("下午三点开会"), False, False, False)

        decision = self.adapter.poll_decision()
        self.assertEqual(decision.suggested_scene, "reminder")
        self.assertEqual(decision.suggested_prompt, "下午三点开会")

    def test_repeat_ticks_do_not_queue_a_second_decision_behind_the_first(self):
        # Dedup lives in the service (same reason within DEDUP_WINDOW); the
        # adapter's job is to hand the runtime exactly one item to act on.
        self.adapter.tick({}, self._due_reminder("第一条"), False, False, False)
        self.adapter.tick({}, self._due_reminder("第二条"), False, False, False)

        self.assertIsNotNone(self.adapter.poll_decision())
        self.adapter.record_executed(self.adapter.poll_decision())
        self.assertIsNone(self.adapter.poll_decision())

    def test_record_executed_releases_the_queue_slot(self):
        self.adapter.tick({}, self._due_reminder(), False, False, False)
        decision = self.adapter.poll_decision()

        self.adapter.record_executed(decision)
        self.assertIsNone(self.adapter.poll_decision())

    def test_close_discards_queued_decisions(self):
        self.adapter.tick({}, self._due_reminder(), False, False, False)
        self.assertIsNotNone(self.adapter.poll_decision())

        self.adapter.close()
        self.assertIsNone(self.adapter.poll_decision())

    def test_only_decisions_are_queued(self):
        self.adapter.tick({}, self._due_reminder(), False, False, False)
        self.assertIsInstance(self.adapter.poll_decision(), ProactiveDecision)

    def test_sleeping_suppresses_every_decision(self):
        """A sleeping companion stays silent no matter what it perceives."""
        self.adapter.tick({"face_detected": True, "emotion": "sad"},
                          self._due_reminder(), True, False, False)
        self.assertIsNone(self.adapter.poll_decision())

    def test_emotion_care_needs_three_consecutive_readings(self):
        def tick(speaking=False):
            self.adapter.tick({"face_detected": True, "emotion": "sad"},
                              [], False, speaking, False)

        tick()
        self.assertIsNone(self.adapter.poll_decision())
        tick()
        self.assertIsNone(self.adapter.poll_decision())
        tick()
        decision = self.adapter.poll_decision()
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "emotion_care:sad")

    def test_speaking_suppresses_emotion_care(self):
        for _ in range(3):
            self.adapter.tick({"face_detected": True, "emotion": "sad"},
                              [], False, True, False)
        self.assertIsNone(self.adapter.poll_decision())


# ═══════════════════════════════════════════════════════════════════
#  Contract — the adapter decides, it does not act
# ═══════════════════════════════════════════════════════════════════

class AdapterContractTest(unittest.TestCase):
    def test_adapter_never_calls_llm_or_tts(self):
        """The adapter must stay a pure decision maker."""
        text = ADAPTER_SOURCE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("deepseek", text.lower())
        self.assertNotIn("openai", text.lower())
        self.assertNotIn("tts_speak", text)
        self.assertNotIn("fibo_tts", text)
        self.assertNotIn("doubao", text.lower())
        self.assertNotIn("drizzle", text.lower())

    def test_health_reports_level_and_backend(self):
        svc = ProactiveServiceImpl()
        adapter = ProactiveInteractionAdapter(svc)

        health = adapter.health()
        self.assertEqual(health["backend"], "real")
        self.assertEqual(health["level"], 5)
        self.assertEqual(health["queued"], 0)


if __name__ == "__main__":
    unittest.main()
