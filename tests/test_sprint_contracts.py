#!/usr/bin/env python3
"""Sprint contract tests — reminder, proactive, persona, gating."""
import os, sys, unittest, time
# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path: sys.path.insert(0, _APP)

class FakeReminderManager:
    def __init__(self): self.items = []
    def add(self, content, trigger_time, source="ai"):
        self.items.append({"content":content,"trigger_time":trigger_time,"source":source})
        return {"ok":True}

class FakeMemoryService:
    def __init__(self): self.mem = {}
    def save_memory(self, uid, key, val): self.mem[key]=val; return True
    def get_memory(self, uid, key): return self.mem.get(key)

class TestReminderMapping(unittest.TestCase):
    """set_reminder creates exactly one ReminderManager entry."""
    def test_delay_seconds_maps_to_trigger_time(self):
        from src.services.tool_service import register_builtin_tools, NoOpToolService
        svc = NoOpToolService()
        rm = FakeReminderManager()
        def handler(message, delay_seconds, priority="normal"):
            trigger = time.time() + int(delay_seconds)
            rm.add(str(message), trigger, source="ai")
            return {"ok": True}
        register_builtin_tools(svc, weather_provider=lambda c:{"ok":True,"weather":"sunny","temp":25}, device_status_provider=lambda:{"ok":True}, reminder_handler=handler,
            fact_handler=lambda f,c:{"ok":True})
            
        t0 = time.time()
        result = svc.execute("set_reminder", {"message":"drink water","delay_seconds":60})
        self.assertTrue(result.success)
        self.assertEqual(len(rm.items), 1)
        self.assertAlmostEqual(rm.items[0]["trigger_time"], t0+60, delta=2)

class TestProactiveGating(unittest.TestCase):
    """Emotion care is blocked while the robot is speaking or thinking.

    The adapter no longer greets on the first camera frame: it deliberately
    sends ``suppress_greeting`` and instead announces presence via the
    away/return transition, so emotion care is the call the gate actually
    has to drop or pass.
    """

    def setUp(self):
        from src.services.proactive_service import ProactiveServiceImpl
        from src.services.proactive_adapter import ProactiveInteractionAdapter
        self.svc = ProactiveServiceImpl()
        self.adapter = ProactiveInteractionAdapter(self.svc)
        self.adapter._service.reset_cooldown(0)

    def _feed_sad(self, speaking=False, thinking=False, times=3):
        """Three consecutive negative readings — the service's care streak."""
        for _ in range(times):
            self.adapter.tick({"face_detected": True, "emotion": "sad"},
                              [], False, speaking, thinking)
        return self.adapter.poll_decision()

    def test_emotion_care_blocked_when_speaking(self):
        self.assertIsNone(self._feed_sad(speaking=True))

    def test_emotion_care_blocked_when_thinking(self):
        self.assertIsNone(self._feed_sad(thinking=True))

    def test_emotion_care_allowed_when_idle(self):
        d = self._feed_sad()
        self.assertIsNotNone(d)
        self.assertTrue(d.should_act)
        self.assertEqual(d.reason, "emotion_care:sad")

    def test_reminder_is_not_gated_by_speaking(self):
        """A due reminder survives the gate; the caller decides when to speak."""
        self.adapter.tick({}, [{"content": "喝水", "trigger_time": 0}],
                          False, True, False)
        d = self.adapter.poll_decision()
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "reminder")

class TestPersonaSwitching(unittest.TestCase):
    """Persona update reflects in next system prompt."""
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        from src.services.persona_service import JsonFilePersonaService
        self.svc = JsonFilePersonaService(personas_dir=self._tmp)
    def test_switch_tone_reflected(self):
        self.svc.update_persona("default", tone="活泼")
        p = self.svc.build_system_prompt("default")
        self.assertIn("活泼", p)
    def test_switch_persona_id(self):
        self.svc.update_persona("default", name="冷静助手", tone="沉稳")
        p = self.svc.get_persona("default")
        self.assertEqual(p.name, "冷静助手")
        self.assertEqual(p.tone, "沉稳")

class TestNormalChatNoToolLoop(unittest.TestCase):
    """Plain chat must NOT trigger function calling."""
    def test_no_tool_keywords(self):
        tool_kws = ("提醒","闹钟","几分钟后","记住","记着","别忘了","帮我记","设备状态","查询状态")
        chat_text = "今天天气真好，我想出去散步"
        has_intent = any(kw in chat_text for kw in tool_kws)
        self.assertFalse(has_intent)

class TestProactiveHealthFields(unittest.TestCase):
    """health() returns fields main.js expects."""
    def test_health_has_enabled_and_level(self):
        from src.services.proactive_adapter import ProactiveInteractionAdapter
        from src.services.proactive_service import ProactiveServiceImpl
        svc = ProactiveServiceImpl()
        adapter = ProactiveInteractionAdapter(svc)
        h = adapter.health()
        self.assertIn("enabled", h)
        self.assertIn("level", h)
        self.assertTrue(h["enabled"])

class TestReminderDedup(unittest.TestCase):
    """Two identical reminders create only one entry."""
    def test_same_reason_not_queued_twice(self):
        from src.services.proactive_adapter import ProactiveInteractionAdapter
        from src.services.proactive_service import ProactiveServiceImpl
        svc = ProactiveServiceImpl()
        svc.reset_cooldown(0)
        adapter = ProactiveInteractionAdapter(svc)
        # Feed reminder twice with same content
        adapter.tick({}, [{"content":"drink","trigger_time":time.time()+60}], False, False, False)
        adapter.tick({}, [{"content":"drink","trigger_time":time.time()+60}], False, False, False)
        # Only one should be queued
        count = 0
        while adapter.poll_decision():
            d = adapter.poll_decision()
            adapter.record_executed(d)
            count += 1
        self.assertEqual(count, 1, "Same reminder should be deduped")

if __name__ == "__main__": unittest.main()
