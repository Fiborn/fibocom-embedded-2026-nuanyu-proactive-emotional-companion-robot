#!/usr/bin/env python3
"""Phase 2B: ToolService comprehensive tests.

Covers: builtin-tool registration, JSON Schema output, parameter validation
(type / enum / range), normal execution, unknown-tool, execution-exception,
and timeout paths.

All external dependencies use fake (in-memory) providers — no real network,
no real MemoryService, no board hardware.
"""

import os
import sys
import time
import unittest
from typing import Any, Dict, List

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)


# ═══════════════════════════════════════════════════════════════════
#  Fake providers  (injectable, no real I/O)
# ═══════════════════════════════════════════════════════════════════

def fake_weather_provider(city: str) -> Dict[str, Any]:
    """Return canned weather data — never touches the network."""
    return {
        "city": city,
        "temperature_c": 26.0,
        "condition": "晴",
        "humidity": 55,
        "wind_kmh": 12,
        "source": "fake",
    }


def fake_device_status_provider() -> Dict[str, Any]:
    """Return canned device status — never reads /proc or /sys."""
    return {
        "cpu_temp_c": 52.3,
        "memory_mb_total": 4096,
        "memory_mb_free": 1840,
        "disk_mb_free": 3200,
        "uptime_seconds": 86400,
        "source": "fake",
    }


class FakeReminderHandler:
    """In-memory reminder handler — records calls for assertions."""

    def __init__(self):
        self.reminders: List[Dict[str, Any]] = []

    def __call__(self, message: str, delay_seconds: int,
                 priority: str = "normal") -> Dict[str, Any]:
        record = {
            "message": message,
            "delay_seconds": delay_seconds,
            "priority": priority,
        }
        self.reminders.append(record)
        return {"reminder_id": f"rem-{len(self.reminders)}", **record}


class FakeFactHandler:
    """In-memory user-fact handler — records calls for assertions."""

    def __init__(self):
        self.facts: List[Dict[str, Any]] = []

    def __call__(self, fact: str, category: str = "general") -> Dict[str, Any]:
        record = {"fact": fact, "category": category}
        self.facts.append(record)
        return {"fact_id": f"fact-{len(self.facts)}", **record}


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_service_with_builtins():
    """Return a fresh ToolService with all 4 built-in tools registered."""
    from src.services.tool_service import (
        NoOpToolService,
        register_builtin_tools,
    )
    svc = NoOpToolService()
    register_builtin_tools(
        svc,
        weather_provider=fake_weather_provider,
        device_status_provider=fake_device_status_provider,
        reminder_handler=FakeReminderHandler(),
        fact_handler=FakeFactHandler(),
    )
    return svc


# ═══════════════════════════════════════════════════════════════════
#  1. Tool registration & schema export
# ═══════════════════════════════════════════════════════════════════

class TestToolRegistration(unittest.TestCase):
    """Basic CRUD: register, unregister, get, list, schemas."""

    def setUp(self):
        self.svc = _make_service_with_builtins()

    # ── Register / list ──────────────────────────────────────────

    def test_four_builtin_tools_registered(self):
        tools = self.svc.list_tools()
        names = {t.name for t in tools}
        self.assertEqual(len(tools), 4)
        self.assertSetEqual(
            names,
            {"set_reminder", "get_weather",
             "remember_user_fact", "get_device_status"},
        )

    def test_duplicate_registration_rejected(self):
        from src.ports.tools import ToolDefinition
        td = ToolDefinition(name="set_reminder", description="dup")
        self.assertFalse(self.svc.register_tool(td))

    def test_unregister_and_lookup(self):
        self.assertTrue(self.svc.unregister_tool("get_weather"))
        self.assertIsNone(self.svc.get_tool("get_weather"))
        self.assertEqual(len(self.svc.list_tools()), 3)
        # unregister twice is safe
        self.assertFalse(self.svc.unregister_tool("get_weather"))

    # ── Schema export ────────────────────────────────────────────

    def test_get_all_schemas(self):
        schemas = self.svc.get_tool_schemas()
        self.assertEqual(len(schemas), 4)
        for s in schemas:
            self.assertIn("name", s)
            self.assertIn("description", s)
            self.assertIn("parameters", s)
            self.assertIn("type", s["parameters"])
            self.assertEqual(s["parameters"]["type"], "object")

    def test_get_schemas_filtered(self):
        schemas = self.svc.get_tool_schemas(["get_weather"])
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["name"], "get_weather")
        self.assertIn("city", schemas[0]["parameters"]["properties"])

    def test_get_schemas_empty_filter(self):
        schemas = self.svc.get_tool_schemas([])
        self.assertEqual(schemas, [])

    def test_get_schemas_unknown_tool_omitted(self):
        schemas = self.svc.get_tool_schemas(["nonexistent"])
        self.assertEqual(schemas, [])

    # ── Health ───────────────────────────────────────────────────

    def test_health(self):
        h = self.svc.health()
        self.assertEqual(h["tool_count"], 4)
        self.assertFalse(h["closed"])

    def test_close_preserves_registry(self):
        """close() shuts down executor but keeps tools for stateless ops."""
        self.svc.close()
        self.assertTrue(self.svc.health()["closed"])
        # Registry survives close — schemas/list/validate still work
        self.assertEqual(len(self.svc.list_tools()), 4)
        # double close is safe
        self.svc.close()


# ═══════════════════════════════════════════════════════════════════
#  2. JSON Schema parameter validation
# ═══════════════════════════════════════════════════════════════════

class TestParameterValidation(unittest.TestCase):
    """validate_arguments edge-cases beyond the Phase 2A basic check."""

    def setUp(self):
        self.svc = _make_service_with_builtins()

    # ── Required params ──────────────────────────────────────────

    def test_missing_required_param(self):
        ok, err = self.svc.validate_arguments("set_reminder", {})
        self.assertFalse(ok)
        self.assertIn("missing", err.lower())

    def test_missing_one_of_two_required(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder", {"message": "喝水"})
        self.assertFalse(ok)
        self.assertIn("missing", err.lower())

    def test_all_required_present(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder", {"message": "喝水", "delay_seconds": 60})
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_no_required_params_accepts_empty(self):
        ok, err = self.svc.validate_arguments("get_device_status", {})
        self.assertTrue(ok)

    # ── Type checking ────────────────────────────────────────────

    def test_integer_param_accepts_int(self):
        ok, _ = self.svc.validate_arguments(
            "set_reminder", {"message": "x", "delay_seconds": 60})
        self.assertTrue(ok)

    def test_integer_param_rejects_string(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": "sixty"})
        self.assertFalse(ok)
        self.assertIn("integer", err.lower())

    def test_integer_param_rejects_float(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": 30.5})
        self.assertFalse(ok)
        self.assertIn("integer", err.lower())

    def test_integer_param_rejects_boolean(self):
        """bool is a subclass of int in Python — must be rejected."""
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": True})
        self.assertFalse(ok)
        self.assertIn("integer", err.lower())

    def test_string_param_accepts_string(self):
        ok, _ = self.svc.validate_arguments(
            "get_weather", {"city": "beijing"})
        self.assertTrue(ok)

    def test_string_param_rejects_int(self):
        ok, err = self.svc.validate_arguments(
            "get_weather", {"city": 123})
        self.assertFalse(ok)
        self.assertIn("string", err.lower())

    def test_extra_unknown_params_ignored(self):
        """Unknown keys should be silently accepted (forward-compat)."""
        ok, err = self.svc.validate_arguments(
            "get_weather", {"city": "beijing", "foo": "bar"})
        self.assertTrue(ok)

    # ── Enum constraints ─────────────────────────────────────────

    def test_enum_valid_value(self):
        ok, _ = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": 10, "priority": "high"})
        self.assertTrue(ok)

    def test_enum_invalid_value(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": 10, "priority": "urgent"})
        self.assertFalse(ok)
        self.assertIn("one of", err.lower())

    # ── Numeric bounds ───────────────────────────────────────────

    def test_minimum_bound_passes(self):
        ok, _ = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": 1})
        self.assertTrue(ok)

    def test_minimum_bound_fails(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": 0})
        self.assertFalse(ok)
        self.assertIn("minimum", err.lower())

    def test_negative_delay_seconds_rejected(self):
        ok, err = self.svc.validate_arguments(
            "set_reminder",
            {"message": "x", "delay_seconds": -5})
        self.assertFalse(ok)

    # ── Unknown tool ─────────────────────────────────────────────

    def test_validate_unknown_tool(self):
        ok, err = self.svc.validate_arguments("no_such_tool", {})
        self.assertFalse(ok)
        self.assertIn("unknown", err.lower())


# ═══════════════════════════════════════════════════════════════════
#  3. Normal execution  (happy path)
# ═══════════════════════════════════════════════════════════════════

class TestNormalExecution(unittest.TestCase):
    """Each built-in tool executes successfully with fake providers."""

    def setUp(self):
        self.reminders = FakeReminderHandler()
        self.facts = FakeFactHandler()
        from src.services.tool_service import (
            NoOpToolService, register_builtin_tools,
        )
        self.svc = NoOpToolService()
        register_builtin_tools(
            self.svc,
            weather_provider=fake_weather_provider,
            device_status_provider=fake_device_status_provider,
            reminder_handler=self.reminders,
            fact_handler=self.facts,
        )

    # ── set_reminder ─────────────────────────────────────────────

    def test_set_reminder_success(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "该喝水了", "delay_seconds": 300},
            trace_id="t1",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.trace_id, "t1")
        self.assertIn("reminder set", result.message.lower())
        self.assertEqual(len(self.reminders.reminders), 1)
        self.assertEqual(self.reminders.reminders[0]["message"], "该喝水了")
        self.assertEqual(self.reminders.reminders[0]["delay_seconds"], 300)
        self.assertIn("execution_ms", result.data)

    def test_set_reminder_with_priority(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "开会", "delay_seconds": 600, "priority": "high"},
        )
        self.assertTrue(result.success)
        self.assertEqual(self.reminders.reminders[0]["priority"], "high")

    def test_set_reminder_default_priority(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "休息", "delay_seconds": 120},
        )
        self.assertTrue(result.success)
        self.assertEqual(self.reminders.reminders[0]["priority"], "normal")

    # ── get_weather ──────────────────────────────────────────────

    def test_get_weather_success(self):
        result = self.svc.execute(
            "get_weather", {"city": "北京"}, trace_id="t2")
        self.assertTrue(result.success)
        self.assertEqual(result.trace_id, "t2")
        self.assertEqual(result.data["city"], "北京")
        self.assertEqual(result.data["condition"], "晴")
        self.assertAlmostEqual(result.data["temperature_c"], 26.0)
        self.assertIn("execution_ms", result.data)

    def test_get_weather_different_city(self):
        result = self.svc.execute(
            "get_weather", {"city": "上海"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["city"], "上海")

    # ── remember_user_fact ───────────────────────────────────────

    def test_remember_user_fact_success(self):
        result = self.svc.execute(
            "remember_user_fact",
            {"fact": "用户喜欢蓝色", "category": "preference"},
            trace_id="t3",
        )
        self.assertTrue(result.success)
        self.assertIn("remembered", result.message.lower())
        self.assertEqual(len(self.facts.facts), 1)
        self.assertEqual(self.facts.facts[0]["fact"], "用户喜欢蓝色")
        self.assertEqual(self.facts.facts[0]["category"], "preference")
        self.assertIn("execution_ms", result.data)

    def test_remember_user_fact_default_category(self):
        result = self.svc.execute(
            "remember_user_fact", {"fact": "用户住在北京"})
        self.assertTrue(result.success)
        self.assertEqual(self.facts.facts[0]["category"], "general")

    # ── get_device_status ────────────────────────────────────────

    def test_get_device_status_success(self):
        result = self.svc.execute(
            "get_device_status", {}, trace_id="t4")
        self.assertTrue(result.success)
        self.assertEqual(result.trace_id, "t4")
        self.assertIn("cpu_temp_c", result.data)
        self.assertIn("memory_mb_total", result.data)
        self.assertIn("execution_ms", result.data)

    def test_get_device_status_ignores_extra_args(self):
        result = self.svc.execute(
            "get_device_status", {"foo": "bar"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["source"], "fake")


# ═══════════════════════════════════════════════════════════════════
#  4. Parameter error paths  (INVALID_ARGS)
# ═══════════════════════════════════════════════════════════════════

class TestParameterErrors(unittest.TestCase):
    """Every tool returns INVALID_ARGS on bad input."""

    def setUp(self):
        self.svc = _make_service_with_builtins()

    def test_set_reminder_missing_message(self):
        result = self.svc.execute(
            "set_reminder", {"delay_seconds": 60})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_set_reminder_missing_delay(self):
        result = self.svc.execute(
            "set_reminder", {"message": "hello"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_set_reminder_wrong_type_delay(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "hello", "delay_seconds": "later"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_set_reminder_invalid_enum(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "x", "delay_seconds": 10, "priority": "critical"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_set_reminder_negative_delay(self):
        result = self.svc.execute(
            "set_reminder",
            {"message": "x", "delay_seconds": -1})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_get_weather_missing_city(self):
        result = self.svc.execute("get_weather", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_get_weather_wrong_type_city(self):
        result = self.svc.execute("get_weather", {"city": 42})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_remember_user_fact_missing_fact(self):
        result = self.svc.execute("remember_user_fact", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_remember_user_fact_wrong_type_fact(self):
        result = self.svc.execute(
            "remember_user_fact", {"fact": 12345})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")


# ═══════════════════════════════════════════════════════════════════
#  5. Unknown tool
# ═══════════════════════════════════════════════════════════════════

class TestUnknownTool(unittest.TestCase):
    """Executing a non-registered tool returns UNKNOWN_TOOL."""

    def setUp(self):
        self.svc = _make_service_with_builtins()

    def test_unknown_tool_name(self):
        result = self.svc.execute("nonexistent_tool", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")
        self.assertIn("nonexistent_tool", result.message)

    def test_unregistered_after_removal(self):
        self.svc.unregister_tool("get_weather")
        result = self.svc.execute("get_weather", {"city": "北京"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")

    def test_unknown_tool_validation(self):
        result = self.svc.execute("__invalid__", {"x": 1})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")

    def test_empty_tool_name(self):
        result = self.svc.execute("", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")


# ═══════════════════════════════════════════════════════════════════
#  6. Execution exception
# ═══════════════════════════════════════════════════════════════════

class TestExecutionException(unittest.TestCase):
    """When a tool handler raises an exception, EXECUTION_ERROR is returned."""

    def setUp(self):
        from src.services.tool_service import NoOpToolService
        self.svc = NoOpToolService()

    def test_handler_raises_runtime_error(self):
        from src.ports.tools import ToolDefinition

        def _failing_handler():
            raise RuntimeError("boom!")

        self.svc.register_tool(ToolDefinition(
            name="failing_tool", description="always fails",
            handler=_failing_handler,
        ))
        result = self.svc.execute("failing_tool", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "EXECUTION_ERROR")
        self.assertIn("RuntimeError", result.message)
        self.assertIn("boom", result.message)

    def test_handler_raises_value_error(self):
        from src.ports.tools import ToolDefinition

        def _bad_handler(x: int):
            raise ValueError(f"bad value: {x}")

        self.svc.register_tool(ToolDefinition(
            name="bad_tool", description="raises ValueError",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
            handler=_bad_handler,
        ))
        result = self.svc.execute("bad_tool", {"x": -1})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "EXECUTION_ERROR")
        self.assertIn("ValueError", result.message)

    def test_handler_raises_division_by_zero(self):
        from src.ports.tools import ToolDefinition

        def _div_zero():
            return 1 / 0

        self.svc.register_tool(ToolDefinition(
            name="div_zero", description="crashes",
            handler=_div_zero,
        ))
        result = self.svc.execute("div_zero", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "EXECUTION_ERROR")
        self.assertIn("ZeroDivisionError", result.message)

    def test_exc_result_includes_trace_id(self):
        from src.ports.tools import ToolDefinition

        def _fail():
            raise Exception("fail")

        self.svc.register_tool(ToolDefinition(
            name="fail", description="fails", handler=_fail,
        ))
        result = self.svc.execute("fail", {}, trace_id="exc-tr-1")
        self.assertEqual(result.trace_id, "exc-tr-1")

    def test_exc_result_includes_execution_ms(self):
        from src.ports.tools import ToolDefinition

        def _fail():
            raise Exception("fail")

        self.svc.register_tool(ToolDefinition(
            name="fail2", description="fails", handler=_fail,
        ))
        result = self.svc.execute("fail2", {})
        self.assertIn("execution_ms", result.data)


# ═══════════════════════════════════════════════════════════════════
#  7. Timeout
# ═══════════════════════════════════════════════════════════════════

class TestTimeout(unittest.TestCase):
    """If a handler exceeds its timeout_seconds, TIMEOUT is returned."""

    def setUp(self):
        from src.services.tool_service import NoOpToolService
        self.svc = NoOpToolService()

    def test_timeout_returns_timed_out(self):
        from src.ports.tools import ToolDefinition

        def _slow_handler():
            time.sleep(5.0)
            return {"done": True}

        self.svc.register_tool(ToolDefinition(
            name="slow_tool", description="too slow",
            handler=_slow_handler,
            timeout_seconds=0.5,  # handler sleeps 5s, timeout 0.5s
        ))
        t0 = time.perf_counter()
        result = self.svc.execute("slow_tool", {})
        elapsed = time.perf_counter() - t0

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertIn("slow_tool", result.message)
        self.assertIn("0.5s", result.message.lower() or result.message)
        # Must return well before the handler finishes (5s)
        self.assertLess(elapsed, 2.0,
                        f"timeout should fire quickly, took {elapsed:.1f}s")
        self.assertIn("execution_ms", result.data)

    def test_timeout_preserves_trace_id(self):
        from src.ports.tools import ToolDefinition

        def _slow():
            time.sleep(5)

        self.svc.register_tool(ToolDefinition(
            name="slow2", description="slow", handler=_slow,
            timeout_seconds=0.3,
        ))
        result = self.svc.execute("slow2", {}, trace_id="timeout-tr")
        self.assertEqual(result.trace_id, "timeout-tr")

    def test_fast_handler_not_timed_out(self):
        """A handler that completes quickly should not be timed out."""
        from src.ports.tools import ToolDefinition

        def _fast_handler():
            return {"ok": True}

        self.svc.register_tool(ToolDefinition(
            name="fast_tool", description="fast",
            handler=_fast_handler,
            timeout_seconds=5.0,
        ))
        result = self.svc.execute("fast_tool", {})
        self.assertTrue(result.success)
        self.assertEqual(result.data["result"]["ok"], True)


# ═══════════════════════════════════════════════════════════════════
#  8. Lifecycle edge cases
# ═══════════════════════════════════════════════════════════════════

class TestLifecycle(unittest.TestCase):

    def test_execute_after_close_returns_service_closed(self):
        from src.services.tool_service import (
            NoOpToolService, register_builtin_tools,
        )
        svc = NoOpToolService()
        register_builtin_tools(
            svc,
            weather_provider=fake_weather_provider,
            device_status_provider=fake_device_status_provider,
            reminder_handler=FakeReminderHandler(),
            fact_handler=FakeFactHandler(),
        )
        svc.close()
        result = svc.execute("get_weather", {"city": "北京"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SERVICE_CLOSED")

    def test_close_idempotent(self):
        from src.services.tool_service import NoOpToolService
        svc = NoOpToolService()
        svc.close()
        svc.close()  # must not raise
        self.assertTrue(svc.health()["closed"])

    def test_validate_after_close_still_works(self):
        """Closing shouldn't break stateless validation."""
        from src.services.tool_service import (
            NoOpToolService, register_builtin_tools,
        )
        svc = NoOpToolService()
        register_builtin_tools(
            svc,
            weather_provider=fake_weather_provider,
            device_status_provider=fake_device_status_provider,
            reminder_handler=FakeReminderHandler(),
            fact_handler=FakeFactHandler(),
        )
        svc.close()
        ok, err = svc.validate_arguments(
            "get_weather", {"city": "北京"})
        self.assertTrue(ok)


# ═══════════════════════════════════════════════════════════════════
#  9. Standalone validation function
# ═══════════════════════════════════════════════════════════════════

class TestStandaloneValidation(unittest.TestCase):
    """validate_tool_arguments() works without an instantiated service."""

    def test_standalone_valid(self):
        from src.ports.tools import ToolDefinition
        from src.services.tool_service import validate_tool_arguments

        td = ToolDefinition(
            name="test", description="x",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        )
        ok, err = validate_tool_arguments(td, {"x": 42})
        self.assertTrue(ok)

    def test_standalone_invalid(self):
        from src.ports.tools import ToolDefinition
        from src.services.tool_service import validate_tool_arguments

        td = ToolDefinition(
            name="test", description="x",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        )
        ok, err = validate_tool_arguments(td, {"x": "not-int"})
        self.assertFalse(ok)


# ═══════════════════════════════════════════════════════════════════
#  10. Phase 2A backward-compatibility
# ═══════════════════════════════════════════════════════════════════

class TestPhase2ABackwardCompat(unittest.TestCase):
    """The enhanced NoOpToolService must still pass all Phase 2A tests."""

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


if __name__ == "__main__":
    unittest.main()
