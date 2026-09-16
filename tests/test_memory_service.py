#!/usr/bin/env python3
"""Phase 2B: comprehensive tests for SqliteMemoryService.

Uses temporary :memory: databases — never touches real user data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import TYPE_CHECKING

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

if TYPE_CHECKING:  # the helper below imports it lazily at call time
    from src.services.memory_service import SqliteMemoryService


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def _make_svc(db_path: str = ":memory:") -> "SqliteMemoryService":
    from src.services.memory_service import SqliteMemoryService
    return SqliteMemoryService(db_path)


def _write_temp_json(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="mem_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


# ═══════════════════════════════════════════════════════════════════════
#  Message CRUD
# ═══════════════════════════════════════════════════════════════════════

class TestMessageCRUD(unittest.TestCase):
    """save_message / get_recent_messages / get_session_messages."""

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 1 ──
    def test_save_message_returns_id(self):
        mid = self.svc.save_message("u1", "s1", "user", "hello")
        self.assertIsInstance(mid, str)
        self.assertEqual(len(mid), 12)

    # ── 2 ──
    def test_save_and_retrieve_one_message(self):
        self.svc.save_message("u1", "s1", "user", "hello world")
        msgs = self.svc.get_recent_messages("u1")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "hello world")
        self.assertEqual(msgs[0]["role"], "user")

    # ── 3 ──
    def test_default_limit_is_24(self):
        for i in range(50):
            role = "user" if i % 2 == 0 else "assistant"
            self.svc.save_message("u1", "s1", role, f"msg {i}")
        msgs = self.svc.get_recent_messages("u1")
        self.assertEqual(len(msgs), 24)  # default 12 turns

    # ── 4 ──
    def test_custom_limit(self):
        for i in range(30):
            self.svc.save_message("u1", "s1", "user", f"msg {i}")
        msgs = self.svc.get_recent_messages("u1", limit=5)
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[0]["content"], "msg 25")
        self.assertEqual(msgs[-1]["content"], "msg 29")

    # ── 5 ──
    def test_messages_oldest_to_newest(self):
        self.svc.save_message("u1", "s1", "user", "first")
        self.svc.save_message("u1", "s1", "assistant", "second")
        self.svc.save_message("u1", "s1", "user", "third")
        msgs = self.svc.get_recent_messages("u1")
        contents = [m["content"] for m in msgs]
        self.assertEqual(contents, ["first", "second", "third"])

    # ── 6 ──
    def test_session_isolation(self):
        self.svc.save_message("u1", "sA", "user", "session A msg 1")
        self.svc.save_message("u1", "sA", "assistant", "session A msg 2")
        self.svc.save_message("u1", "sB", "user", "session B msg 1")
        msgs_a = self.svc.get_session_messages("sA")
        msgs_b = self.svc.get_session_messages("sB")
        self.assertEqual(len(msgs_a), 2)
        self.assertEqual(len(msgs_b), 1)
        self.assertEqual(msgs_b[0]["content"], "session B msg 1")

    # ── 7 ──
    def test_message_metadata(self):
        self.svc.save_message(
            "u1", "s1", "user", "hi",
            metadata={"latency_ms": 42, "source": "asr"},
        )
        msgs = self.svc.get_recent_messages("u1")
        meta = msgs[0].get("metadata", {})
        self.assertEqual(meta.get("latency_ms"), 42)
        self.assertEqual(meta.get("source"), "asr")


# ═══════════════════════════════════════════════════════════════════════
#  Cross-restart persistence
# ═══════════════════════════════════════════════════════════════════════

class TestPersistence(unittest.TestCase):
    """Data survives close + re-open."""

    def test_messages_survive_restart(self):
        db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_mem_")
        os.close(db_fd)

        try:
            svc1 = _make_svc(db_path)
            svc1.save_message("u1", "s1", "user", "persistent message")
            svc1.save_message("u1", "s1", "assistant", "persistent reply")
            svc1.close()

            svc2 = _make_svc(db_path)
            msgs = svc2.get_recent_messages("u1")
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["content"], "persistent message")
            self.assertEqual(msgs[1]["content"], "persistent reply")
            svc2.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    # ── 8 ──
    def test_memories_survive_restart(self):
        db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_mem_")
        os.close(db_fd)

        try:
            svc1 = _make_svc(db_path)
            svc1.save_memory("u1", "favorite", "pizza", importance=0.9)
            svc1.close()

            svc2 = _make_svc(db_path)
            val = svc2.get_memory("u1", "favorite")
            self.assertEqual(val, "pizza")
            svc2.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════════
#  User isolation
# ═══════════════════════════════════════════════════════════════════════

class TestUserIsolation(unittest.TestCase):
    """Strict user isolation — no cross-user data leak."""

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 9 ──
    def test_messages_isolated_by_user(self):
        self.svc.save_message("alice", "s1", "user", "alice msg")
        self.svc.save_message("bob", "s1", "user", "bob msg")
        alice_msgs = self.svc.get_recent_messages("alice")
        bob_msgs = self.svc.get_recent_messages("bob")
        self.assertEqual(len(alice_msgs), 1)
        self.assertEqual(alice_msgs[0]["content"], "alice msg")
        self.assertEqual(len(bob_msgs), 1)
        self.assertEqual(bob_msgs[0]["content"], "bob msg")

    # ── 10 ──
    def test_memories_isolated_by_user(self):
        self.svc.save_memory("alice", "color", "blue")
        self.svc.save_memory("bob", "color", "red")
        self.assertEqual(self.svc.get_memory("alice", "color"), "blue")
        self.assertEqual(self.svc.get_memory("bob", "color"), "red")

    # ── 11 ──
    def test_list_memories_user_scoped(self):
        self.svc.save_memory("alice", "pref:food", "pizza")
        self.svc.save_memory("alice", "pref:drink", "tea")
        self.svc.save_memory("bob", "pref:food", "burger")
        alice_all = self.svc.list_memories("alice")
        bob_all = self.svc.list_memories("bob")
        self.assertEqual(len(alice_all), 2)
        self.assertEqual(len(bob_all), 1)

    # ── 12 ──
    def test_delete_only_affects_target_user(self):
        self.svc.save_memory("alice", "key", "a")
        self.svc.save_memory("bob", "key", "b")
        self.svc.delete_memory("alice", "key")
        self.assertIsNone(self.svc.get_memory("alice", "key"))
        self.assertEqual(self.svc.get_memory("bob", "key"), "b")


# ═══════════════════════════════════════════════════════════════════════
#  Long-term memory  CRUD
# ═══════════════════════════════════════════════════════════════════════

class TestLongTermMemory(unittest.TestCase):

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 13 ──
    def test_save_and_get_memory(self):
        ok = self.svc.save_memory("u1", "city", "上海")
        self.assertTrue(ok)
        self.assertEqual(self.svc.get_memory("u1", "city"), "上海")

    # ── 14 ──
    def test_overwrite_memory(self):
        self.svc.save_memory("u1", "city", "上海")
        self.svc.save_memory("u1", "city", "北京")
        self.assertEqual(self.svc.get_memory("u1", "city"), "北京")

    # ── 15 ──
    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.svc.get_memory("u1", "no_such_key"))

    # ── 16 ──
    def test_list_memories_with_prefix(self):
        self.svc.save_memory("u1", "pref:color", "blue")
        self.svc.save_memory("u1", "pref:food", "pizza")
        self.svc.save_memory("u1", "meta:age", "25")
        result = self.svc.list_memories("u1", prefix="pref:")
        self.assertEqual(len(result), 2)
        self.assertIn("pref:color", result)
        self.assertIn("pref:food", result)
        self.assertNotIn("meta:age", result)

    # ── 17 ──
    def test_delete_memory(self):
        self.svc.save_memory("u1", "temp", "x")
        self.assertTrue(self.svc.delete_memory("u1", "temp"))
        self.assertIsNone(self.svc.get_memory("u1", "temp"))
        self.assertFalse(self.svc.delete_memory("u1", "temp"))

    # ── 18 ──
    def test_memory_stores_complex_value(self):
        data = {"tags": ["重要", "紧急"], "count": 5}
        self.svc.save_memory("u1", "complex", data)
        self.assertEqual(self.svc.get_memory("u1", "complex"), data)


# ═══════════════════════════════════════════════════════════════════════
#  Explicit  "remember"
# ═══════════════════════════════════════════════════════════════════════

class TestExplicitRemember(unittest.TestCase):

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 19 ──
    def test_remember_creates_keyed_memory(self):
        ok = self.svc.remember("u1", "Alice有颈椎病，不能久坐")
        self.assertTrue(ok)
        all_mem = self.svc.list_memories("u1")
        remembered = {
            k: v for k, v in all_mem.items()
            if k.startswith("remembered:")
        }
        self.assertEqual(len(remembered), 1)
        value = list(remembered.values())[0]
        self.assertIn("颈椎", value)

    # ── 20 ──
    def test_remember_is_searchable(self):
        self.svc.remember("u1", "Alice有颈椎病")
        self.svc.remember("u1", "Bob喜欢热可可")
        results = self.svc.search_memories("u1", "颈椎")
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("颈椎", results[0]["value"])


# ═══════════════════════════════════════════════════════════════════════
#  Lightweight  search
# ═══════════════════════════════════════════════════════════════════════

class TestSearch(unittest.TestCase):

    def setUp(self):
        self.svc = _make_svc()
        # Seed with varied data
        self.svc.save_memory("u1", "pref:drink", "热可可",
                              importance=0.9, keywords="饮品 热可可 可可")
        self.svc.save_memory("u1", "pref:food", "意大利面",
                              importance=0.6, keywords="食物 意大利面 意面")
        self.svc.save_memory("u1", "health:note", "颈椎病不能久坐",
                              importance=0.8, keywords="健康 颈椎 久坐")
        self.svc.save_memory("u1", "meta:city", "上海",
                              importance=0.3, keywords="城市 上海")

    def tearDown(self):
        self.svc.close()

    # ── 21 ──
    def test_search_finds_by_keyword(self):
        results = self.svc.search_memories("u1", "可可")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "pref:drink")

    # ── 22 ──
    def test_search_ranks_by_importance(self):
        results = self.svc.search_memories("u1", "food 饮品")
        self.assertGreaterEqual(len(results), 2)
        # "pref:drink" has importance 0.9 > "pref:food" 0.6
        top_keys = [r["key"] for r in results[:2]]
        self.assertIn("pref:drink", top_keys)

    # ── 23 ──
    def test_search_empty_query_returns_empty(self):
        results = self.svc.search_memories("u1", "")
        self.assertEqual(results, [])

    # ── 24 ──
    def test_search_no_match_returns_empty(self):
        results = self.svc.search_memories("u1", "zzz_nonexistent_xyz")
        self.assertEqual(results, [])

    # ── 25 ──
    def test_search_respects_limit(self):
        results = self.svc.search_memories("u1", "pref meta note", limit=2)
        self.assertLessEqual(len(results), 2)

    # ── 26 ──
    def test_search_user_isolated(self):
        self.svc.save_memory("bob", "pref:drink", "咖啡",
                              importance=0.9, keywords="饮品 咖啡")
        results_u1 = self.svc.search_memories("u1", "咖啡")
        self.assertEqual(results_u1, [])
        results_bob = self.svc.search_memories("bob", "咖啡")
        self.assertEqual(len(results_bob), 1)


# ═══════════════════════════════════════════════════════════════════════
#  Idempotent  JSON import
# ═══════════════════════════════════════════════════════════════════════

class TestJsonImport(unittest.TestCase):

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 27 ──
    def test_import_user_json_flattens_profile(self):
        data = {
            "nickname": "Bob",
            "profile": {
                "name": "Bob",
                "identity": "大学生",
                "major": "英语",
                "health": "爱熬夜",
                "weather_city": "上海",
            },
            "notes": ["非常聪明", "喜欢热可可"],
            "encourage_style": "温柔鼓励",
        }
        jpath = _write_temp_json(data)
        try:
            result = self.svc.import_user_memories("Bob", jpath)
            self.assertGreaterEqual(result["imported"], 5)
            self.assertEqual(result["errors"], [])

            self.assertEqual(
                self.svc.get_memory("Bob", "profile:name"), "Bob")
            self.assertEqual(
                self.svc.get_memory("Bob", "profile:major"), "英语")
            self.assertEqual(
                self.svc.get_memory("Bob", "meta:nickname"), "Bob")
        finally:
            os.unlink(jpath)

    # ── 28 ──
    def test_import_is_idempotent(self):
        data = {
            "profile": {"name": "Bob"},
            "notes": ["note one"],
        }
        jpath = _write_temp_json(data)
        try:
            r1 = self.svc.import_user_memories("Bob", jpath)
            self.assertGreaterEqual(r1["imported"], 1)

            r2 = self.svc.import_user_memories("Bob", jpath)
            self.assertEqual(r2["imported"], 0)
            self.assertGreater(r2["skipped"], 0)
        finally:
            os.unlink(jpath)

    # ── 29 ──
    def test_import_overwrite_mode(self):
        data = {"profile": {"name": "v1"}}
        jpath = _write_temp_json(data)
        try:
            self.svc.import_user_memories("u1", jpath)
            # Modify file
            with open(jpath, "w", encoding="utf-8") as fh:
                json.dump({"profile": {"name": "v2"}}, fh)
            r = self.svc.import_user_memories("u1", jpath, overwrite=True)
            self.assertGreaterEqual(r["imported"], 1)
            self.assertEqual(
                self.svc.get_memory("u1", "profile:name"), "v2")
        finally:
            os.unlink(jpath)

    # ── 30 ──
    def test_import_missing_file_reports_error(self):
        result = self.svc.import_user_memories(
            "u1", "/nonexistent/path/file.json")
        self.assertGreater(len(result["errors"]), 0)

    # ── 31 ──
    def test_import_preserves_notes(self):
        data = {"notes": ["颈椎病", "喜欢热可可", "下午有会"]}
        jpath = _write_temp_json(data)
        try:
            result = self.svc.import_user_memories("u1", jpath)
            self.assertGreaterEqual(result["imported"], 3)
            note0 = self.svc.get_memory("u1", "note:0")
            self.assertEqual(note0, "颈椎病")
        finally:
            os.unlink(jpath)


# ═══════════════════════════════════════════════════════════════════════
#  Graceful  degradation
# ═══════════════════════════════════════════════════════════════════════

class TestGracefulDegradation(unittest.TestCase):
    """A broken/closed DB must never raise — the voice pipeline stays alive."""

    # ── 32 ──
    def test_read_on_closed_db_returns_defaults(self):
        svc = _make_svc()
        svc.save_message("u1", "s1", "user", "hello")
        svc.close()  # close the underlying store
        # After close, all reads degrade gracefully
        msgs = svc.get_recent_messages("u1")
        self.assertEqual(msgs, [])
        mem = svc.get_memory("u1", "key")
        self.assertIsNone(mem)
        lst = svc.list_memories("u1")
        self.assertEqual(lst, {})

    # ── 33 ──
    def test_write_on_closed_db_does_not_raise(self):
        svc = _make_svc()
        svc.close()
        mid = svc.save_message("u1", "s1", "user", "hello")
        self.assertEqual(mid, "")  # degraded → empty string
        ok = svc.save_memory("u1", "key", "value")
        self.assertFalse(ok)
        ok2 = svc.delete_memory("u1", "key")
        self.assertFalse(ok2)

    # ── 34 ──
    def test_search_on_closed_db_returns_empty(self):
        svc = _make_svc()
        svc.close()
        results = svc.search_memories("u1", "query")
        self.assertEqual(results, [])

    # ── 34b ──
    def test_health_after_close(self):
        svc = _make_svc()
        svc.close()
        h = svc.health()
        self.assertFalse(h["healthy"])
        self.assertIn("backend", h)


# ═══════════════════════════════════════════════════════════════════════
#  Health  /  lifecycle
# ═══════════════════════════════════════════════════════════════════════

class TestHealthAndLifecycle(unittest.TestCase):

    # ── 35 ──
    def test_health_reports_backend_sqlite(self):
        svc = _make_svc()
        try:
            h = svc.health()
            self.assertEqual(h["backend"], "sqlite")
            self.assertTrue(h["healthy"])
            self.assertEqual(h["message_count"], 0)
            self.assertEqual(h["memory_count"], 0)
        finally:
            svc.close()

    # ── 36 ──
    def test_health_reflects_counts(self):
        svc = _make_svc()
        try:
            svc.save_message("u1", "s1", "user", "a")
            svc.save_message("u1", "s1", "assistant", "b")
            svc.save_memory("u1", "k1", "v1")
            svc.save_memory("u1", "k2", "v2")
            h = svc.health()
            self.assertEqual(h["message_count"], 2)
            self.assertEqual(h["memory_count"], 2)
        finally:
            svc.close()

    # ── 37 ──
    def test_close_is_idempotent(self):
        svc = _make_svc()
        svc.save_message("u1", "s1", "user", "test")
        svc.close()
        svc.close()  # second close must not raise
        # After close, reads degrade gracefully
        msgs = svc.get_recent_messages("u1")
        self.assertEqual(msgs, [])

    # ── 38 ──
    def test_large_conversation_window(self):
        svc = _make_svc()
        try:
            # 100 turns = 200 messages across 2 users
            for i in range(100):
                svc.save_message("u1", "s1", "user", f"turn {i}")
                svc.save_message("u1", "s1", "assistant", f"reply {i}")
            msgs = svc.get_recent_messages("u1", limit=200)
            self.assertEqual(len(msgs), 200)
            self.assertEqual(msgs[0]["content"], "turn 0")
            self.assertEqual(msgs[-1]["content"], "reply 99")
        finally:
            svc.close()


# ═══════════════════════════════════════════════════════════════════════
#  Interface  conformance  (same tests as test_phase2_interfaces)
# ═══════════════════════════════════════════════════════════════════════

class TestInterfaceConformance(unittest.TestCase):
    """SqliteMemoryService passes the Phase 2A contract tests."""

    def setUp(self):
        self.svc = _make_svc()

    def tearDown(self):
        self.svc.close()

    # ── 39 ──
    def test_save_and_retrieve_message(self):
        self.svc.save_message("u1", "s1", "user", "hello")
        msgs = self.svc.get_recent_messages("u1")
        self.assertGreaterEqual(len(msgs), 1)
        self.assertEqual(msgs[-1]["content"], "hello")

    # ── 40 ──
    def test_save_and_get_memory(self):
        self.svc.save_memory("u1", "favorite_color", "blue")
        self.assertEqual(
            self.svc.get_memory("u1", "favorite_color"), "blue")

    # ── 41 ──
    def test_list_memories_filtered(self):
        self.svc.save_memory("u1", "pref:food", "pizza")
        self.svc.save_memory("u1", "pref:drink", "coffee")
        result = self.svc.list_memories("u1", prefix="pref:")
        self.assertIn("pref:food", result)

    # ── 42 ──
    def test_delete_memory(self):
        self.svc.save_memory("u1", "temp", "x")
        self.assertTrue(self.svc.delete_memory("u1", "temp"))
        self.assertIsNone(self.svc.get_memory("u1", "temp"))

    # ── 43 ──
    def test_health_returns_dict(self):
        h = self.svc.health()
        self.assertIsInstance(h, dict)
        self.assertIn("backend", h)


# ═══════════════════════════════════════════════════════════════════════
#  NoOp  still works  (regression guard)
# ═══════════════════════════════════════════════════════════════════════

class TestNoOpStillWorks(unittest.TestCase):
    """Existing NoOp tests must still pass."""

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


if __name__ == "__main__":
    unittest.main()
