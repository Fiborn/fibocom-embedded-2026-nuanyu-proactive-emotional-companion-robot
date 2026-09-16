#!/usr/bin/env python3
"""Phase 2C: end-to-end integration tests."""
import os, sys, unittest
# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path: sys.path.insert(0, _APP)

class TestMemoryPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile; cls._tmp = tempfile.mktemp(suffix=".db")
        from src.services.memory_service import SqliteMemoryService
        cls.svc = SqliteMemoryService(db_path=cls._tmp)
    @classmethod
    def tearDownClass(cls):
        try: cls.svc.close()
        except: pass
        try: os.remove(cls._tmp)
        except: pass
    def test_save_12_turns(self):
        for i in range(12):
            self.svc.save_message("u1","s1","user",f"msg {i}")
            self.svc.save_message("u1","s1","assistant",f"reply{i}")
        self.assertEqual(len(self.svc.get_recent_messages("u1",24)),24)
    def test_memory_kv(self):
        self.svc.save_memory("u1","color","blue")
        self.assertEqual(self.svc.get_memory("u1","color"),"blue")

class TestPersonaInPrompt(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        from src.services.persona_service import JsonFilePersonaService
        self.svc = JsonFilePersonaService(personas_dir=self._tmpdir)
    def test_default_persona_name_reaches_the_prompt(self):
        # The shipped default persona is "陪伴助手" and its name must appear in
        # the prompt the LLM receives (the user can rename it at runtime).
        self.assertIn("陪伴助手", self.svc.build_system_prompt("default"))
    def test_extra_context_in_prompt(self):
        p = self.svc.build_system_prompt("default", {"mood":"开心","goal":"写作业"})
        self.assertIn("开心", p)

class TestToolServiceBuiltins(unittest.TestCase):
    def setUp(self):
        from src.services.tool_service import NoOpToolService
        from src.ports.tools import ToolDefinition
        self.svc = NoOpToolService()
        self.svc.register_tool(ToolDefinition(name="dev_status", description="get board status",
            parameters={"type":"object","properties":{"mem":{"type":"boolean"}},"required":[]}))
    def test_schema_count(self):
        self.assertEqual(len(self.svc.get_tool_schemas()), 1)

class TestProactiveDecisions(unittest.TestCase):
    def setUp(self):
        from src.services.proactive_service import ProactiveServiceImpl
        self.svc = ProactiveServiceImpl()
        self.svc.reset_cooldown(0)
    def test_greeting_on_present(self):
        from src.domain.events import RuntimeEvent
        d = self.svc.handle_event(RuntimeEvent(event_id="e1",event_type="user_present",timestamp=100.0,source="vision"))
        self.assertIsNotNone(d)
        self.assertTrue(d.should_act)
    def test_cooldown_blocks(self):
        from src.domain.events import RuntimeEvent
        self.svc.reset_cooldown(300)
        d = self.svc.handle_event(RuntimeEvent(event_id="e2",event_type="user_present",timestamp=200.0,source="vision"))
        self.assertIsNone(d)

class TestCloudService(unittest.TestCase):
    def test_noop_cloud_basics(self):
        from src.services.cloud_sync_service import NoOpCloudSyncService
        svc = NoOpCloudSyncService()
        self.assertFalse(svc.is_connected())
        r = svc.enqueue_upload("test", {"v":1})
        self.assertIsNotNone(r)
        self.assertEqual(svc.pending_count(), 1)

class TestVoicePipelineUnaffected(unittest.TestCase):
    def test_noop_memory_empty(self):
        from src.services.memory_service import NoOpMemoryService
        self.assertEqual(NoOpMemoryService().get_recent_messages("x"),[])
    def test_noop_tool_safe(self):
        from src.services.tool_service import NoOpToolService
        self.assertFalse(NoOpToolService().execute("x",{}).success)
    def test_noop_proactive_none(self):
        from src.domain.events import RuntimeEvent
        from src.services.proactive_service import NoOpProactiveService
        self.assertIsNone(NoOpProactiveService().handle_event(
            RuntimeEvent(event_id="x",event_type="t",timestamp=0.0)))

class TestPhase2HttpApisExist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        cls._main = (root / "nuanyu_web.py").read_text(encoding="utf-8")
    def test_get_apis_registered(self):
        for api in ["/api/persona","/api/memory/stats","/api/tools","/api/cloud/status"]:
            self.assertIn(api, self._main)
    def test_post_apis_registered(self):
        for api in ["/api/persona/update","/api/cloud/command/ack"]:
            self.assertIn(api, self._main)

if __name__ == "__main__": unittest.main()
