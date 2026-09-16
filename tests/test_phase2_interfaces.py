#!/usr/bin/env python3
"""Phase 2A: interface contract tests.

Verify that every port has a matching NoOp implementation, that all
NoOp services are loadable without board deps, that domain models
round-trip correctly, and that RuntimeServices loads Phase 2 slots.
"""

import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)


# ═══════════════════════════════════════════════════════════════
#  Domain model round-trip tests
# ═══════════════════════════════════════════════════════════════

class TestDomainModels(unittest.TestCase):

    def test_runtime_event_is_immutable(self):
        from src.domain.events import RuntimeEvent, EventType
        evt = RuntimeEvent(
            event_id="ev-1", event_type=EventType.USER_SPEECH,
            timestamp=100.0, source="asr", user_id="u1",
            payload={"text": "hello"})
        self.assertEqual(evt.source, "asr")
        self.assertEqual(evt.payload["text"], "hello")

    def test_agent_context_defaults(self):
        from src.domain.models import AgentContext
        ctx = AgentContext(user_id="u1", user_text="hi")
        self.assertEqual(ctx.user_id, "u1")
        self.assertEqual(ctx.recent_messages, [])
        self.assertEqual(ctx.persona, {})

    def test_agent_action_fields(self):
        from src.domain.models import AgentAction
        act = AgentAction(
            action_type="tool_call", tool_name="weather",
            tool_arguments={"city": "上海"}, trace_id="tr1")
        self.assertEqual(act.tool_name, "weather")

    def test_tool_result_error(self):
        from src.domain.models import ToolResult
        r = ToolResult(success=False, error_code="TIMEOUT",
                       message="request timed out")
        self.assertFalse(r.success)
        self.assertEqual(r.error_code, "TIMEOUT")


# ═══════════════════════════════════════════════════════════════
#  NoOp service existence tests
# ═══════════════════════════════════════════════════════════════

class TestNoOpMemoryService(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src.services.memory_service import NoOpMemoryService
        cls.svc = NoOpMemoryService()

    def test_save_and_retrieve_message(self):
        self.svc.save_message("u1", "s1", "user", "hello")
        msgs = self.svc.get_recent_messages("u1")
        self.assertGreaterEqual(len(msgs), 1)
        self.assertEqual(msgs[-1]["content"], "hello")

    def test_save_and_get_memory(self):
        self.svc.save_memory("u1", "favorite_color", "blue")
        self.assertEqual(self.svc.get_memory("u1", "favorite_color"), "blue")

    def test_list_memories_filtered(self):
        self.svc.save_memory("u1", "pref:food", "pizza")
        self.svc.save_memory("u1", "pref:drink", "coffee")
        result = self.svc.list_memories("u1", prefix="pref:")
        self.assertIn("pref:food", result)

    def test_delete_memory(self):
        self.svc.save_memory("u1", "temp", "x")
        self.assertTrue(self.svc.delete_memory("u1", "temp"))
        self.assertIsNone(self.svc.get_memory("u1", "temp"))

    def test_health_returns_dict(self):
        h = self.svc.health()
        self.assertEqual(h["backend"], "noop")


class TestNoOpPersonaService(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src.services.persona_service import NoOpPersonaService
        cls.svc = NoOpPersonaService()

    def test_default_persona_exists(self):
        from src.ports.persona import Persona
        p = self.svc.get_persona()
        self.assertIsInstance(p, Persona)
        # The shipped default persona is the neutral "陪伴助手"; users rename it
        # through /api/persona/update.
        self.assertEqual(p.name, "陪伴助手")

    def test_update_persona(self):
        p = self.svc.update_persona("default", tone="活泼")
        self.assertEqual(p.tone, "活泼")

    def test_build_system_prompt(self):
        prompt = self.svc.build_system_prompt("default",
                                              {"mood": "开心", "goal": "学习"})
        self.assertIn("陪伴助手", prompt)
        self.assertIn("开心", prompt)

    def test_voice_binding(self):
        from src.ports.persona import VoiceProfile
        vp = VoiceProfile(voice_id="v1", display_name="Test")
        self.svc.bind_voice("default", vp)
        bound = self.svc.get_voice_for_persona("default")
        self.assertEqual(bound.voice_id, "v1")


class TestNoOpToolService(unittest.TestCase):

    def setUp(self):
        from src.services.tool_service import NoOpToolService
        self.svc = NoOpToolService()

    def test_register_and_list(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(
            name="echo", description="echoes input",
            parameters={"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]})
        ok = self.svc.register_tool(td)
        self.assertTrue(ok)
        self.assertEqual(len(self.svc.list_tools()), 1)

    def test_duplicate_rejected(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(name="echo2", description="x")
        self.svc.register_tool(td)
        self.assertFalse(self.svc.register_tool(td))

    def test_validate_missing_required(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(
            name="need_arg", description="requires arg x",
            parameters={"type": "object",
                        "properties": {"x": {"type": "number"}},
                        "required": ["x"]})
        self.svc.register_tool(td)
        ok, err = self.svc.validate_arguments("need_arg", {})
        self.assertFalse(ok)

    def test_execute_no_handler(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(name="nohandler", description="no handler")
        self.svc.register_tool(td)
        result = self.svc.execute("nohandler", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NO_HANDLER")

    def test_get_schemas_for_llm(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(
            name="weather", description="get weather",
            parameters={"type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"]})
        self.svc.register_tool(td)
        schemas = self.svc.get_tool_schemas(["weather"])
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["name"], "weather")


class TestNoOpProactiveService(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src.services.proactive_service import NoOpProactiveService
        cls.svc = NoOpProactiveService()

    def test_always_returns_none(self):
        from src.domain.events import RuntimeEvent
        evt = RuntimeEvent(event_id="e1", event_type="user_present",
                           timestamp=0.0, source="vision")
        decision = self.svc.handle_event(evt)
        self.assertIsNone(decision)

    def test_cooldown_reset(self):
        self.svc.reset_cooldown(5)
        self.assertGreater(self.svc.get_cooldown_remaining(), 0)


class TestNoOpCloudSyncService(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src.services.cloud_sync_service import NoOpCloudSyncService
        cls.svc = NoOpCloudSyncService()

    def test_never_connected(self):
        self.assertFalse(self.svc.is_connected())
        self.assertFalse(self.svc.connect())

    def test_enqueue_and_count(self):
        rid = self.svc.enqueue_upload("test", {"val": 1})
        self.assertIsNotNone(rid)
        self.assertEqual(self.svc.pending_count(), 1)

    def test_flush_noop(self):
        uploaded = self.svc.flush()
        self.assertEqual(uploaded, [])

    def test_poll_commands_empty(self):
        self.assertEqual(self.svc.poll_commands(), [])


# ═══════════════════════════════════════════════════════════════
#  RuntimeServices Phase 2 slots test
# ═══════════════════════════════════════════════════════════════

class TestRuntimeServicesPhase2Slots(unittest.TestCase):

    def tearDown(self):
        from src.core.runtime_services import set_runtime_services
        set_runtime_services(None)

    def test_initialize_loads_all_five_noop_services(self):
        from src.core.runtime_services import (RuntimeServices,
                                               set_runtime_services)
        rt = RuntimeServices()
        rt.config = {
            "asr_enabled": False,
            "vision_enabled": False,
        }
        set_runtime_services(rt)
        rt.initialize()
        self.assertIsNotNone(rt.memory_service)
        self.assertIsNotNone(rt.persona_service)
        self.assertIsNotNone(rt.tool_service)
        self.assertIsNotNone(rt.proactive_service)
        self.assertIsNotNone(rt.cloud_sync_service)

    def test_health_includes_phase2_services(self):
        from src.core.runtime_services import (RuntimeServices,
                                               set_runtime_services)
        rt = RuntimeServices()
        rt._initialized = True
        rt.config = {"asr_enabled": False, "vision_enabled": False}
        set_runtime_services(rt)
        rt.initialize()
        snap = rt.health_snapshot()
        names = [s["name"] for s in snap["services"]]
        for expected in ["memory", "persona", "tools",
                         "proactive", "cloud_sync"]:
            self.assertIn(expected, names)

    def test_stop_closes_phase2_services(self):
        from src.core.runtime_services import (RuntimeServices,
                                               set_runtime_services)
        rt = RuntimeServices()
        rt.config = {"asr_enabled": False, "vision_enabled": False}
        set_runtime_services(rt)
        rt.initialize()
        rt.stop()
        # stop should complete without exception even with NoOp services
        self.assertTrue(rt._stopped)

    def test_noop_services_dont_break_existing_pipeline(self):
        """Loading NoOp services does not import board-specific deps."""
        from src.core.runtime_services import RuntimeServices
        rt = RuntimeServices()
        rt.config = {"asr_enabled": False, "vision_enabled": False}
        # initialize must not raise
        rt.initialize()
        # health must return valid structure
        snap = rt.health_snapshot()
        self.assertTrue(snap["initialized"])


if __name__ == "__main__":
    unittest.main()
