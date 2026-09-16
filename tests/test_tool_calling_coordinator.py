#!/usr/bin/env python3
"""Phase 2 Sprint: ToolCallingCoordinator comprehensive mock tests.

All LLM calls and tool executions are mocked — zero real API calls,
zero board hardware, zero real MemoryService.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.domain.models import ToolResult


# ═══════════════════════════════════════════════════════════════════════
#  Mock ToolService helpers
# ═══════════════════════════════════════════════════════════════════════

def _mk_tool_svc(extra_schemas=None):
    """Build a minimal ToolService mock with the three sprint tools."""
    svc = MagicMock()
    svc.list_tools.return_value = []

    base_schemas = [
        {
            "name": "set_reminder",
            "description": "设置提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "delay_seconds": {"type": "integer", "minimum": 1},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                },
                "required": ["message", "delay_seconds"],
            },
        },
        {
            "name": "remember_user_fact",
            "description": "记住用户事实",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["fact"],
            },
        },
        {
            "name": "get_device_status",
            "description": "获取设备状态",
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    schemas = base_schemas + (extra_schemas or [])
    svc.get_tool_schemas = MagicMock(return_value=schemas)

    def _execute(name, arguments, trace_id=""):
        if name == "set_reminder":
            return ToolResult(
                success=True, data={"reminder_id": "r1"},
                message=f"reminder set: {arguments.get('message', '')[:30]}",
                trace_id=trace_id)
        elif name == "remember_user_fact":
            return ToolResult(
                success=True, data={"saved": True},
                message=f"remembered: {arguments.get('fact', '')[:30]}",
                trace_id=trace_id)
        elif name == "get_device_status":
            return ToolResult(
                success=True,
                data={"cpu_temp_c": 52, "mem_free_mb": 1800, "uptime_s": 86400},
                message="device status ok",
                trace_id=trace_id)
        return ToolResult(success=False, error_code="UNKNOWN_TOOL",
                          message="unknown", trace_id=trace_id)
    svc.execute = MagicMock(side_effect=_execute)
    return svc


def _mk_deepseek_client(responses=None):
    """Build a mock DeepSeek client whose ``chat_completion`` returns
    responses from a list (one per call)."""
    client = MagicMock()
    client.warmup = MagicMock(return_value=True)
    client.stream_chat = MagicMock(return_value=iter([]))
    if responses is not None:
        client.chat_completion = MagicMock(side_effect=list(responses))
    else:
        client.chat_completion = MagicMock(return_value=_simple_reply("hello"))
    return client


# ── LLM response builders ──────────────────────────────────────────


def _simple_reply(content: str) -> Dict[str, Any]:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


def _tool_call_response(tool_name: str, arguments: Dict[str, Any],
                         tool_id: str = "call_1") -> Dict[str, Any]:
    """Build an LLM response with one tool_call."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }


def _multi_tool_call_response(calls: List[Tuple[str, Dict, str]]) -> Dict:
    """Build an LLM response with multiple tool_calls.
    Each call: (tool_name, arguments, tool_id)."""
    tcs = []
    for name, args, tid in calls:
        tcs.append({
            "id": tid,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })
    return {
        "choices": [{
            "message": {"role": "assistant", "content": None,
                         "tool_calls": tcs},
            "finish_reason": "tool_calls",
        }],
    }


# ═══════════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════════

class TestToolCallingCoordinator(unittest.TestCase):

    def setUp(self):
        from src.tools.tool_calling_coordinator import ToolCallingCoordinator
        self.TCC = ToolCallingCoordinator
        self.tool_svc = _mk_tool_svc()
        self.system_prompt = "你是小陪，温柔简短的桌面陪伴机器人。"
        self.api_key = "sk-test-key"

    # ═══════════════════════════════════════════════════════════════
    #  Basic: no tool calls needed
    # ═══════════════════════════════════════════════════════════════

    def test_plain_reply_no_tool_calls(self):
        """LLM returns content with no tool_calls → returns text directly."""
        client = _mk_deepseek_client([_simple_reply("你好呀，今天想做什么？")])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("你好", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertEqual(text, "你好呀，今天想做什么？")
        self.assertEqual(events, [])
        self.assertEqual(client.chat_completion.call_count, 1)

    def test_empty_enabled_tools_skips_tool_loop(self):
        """No enabled tools → plain reply without tool payload."""
        client = _mk_deepseek_client([_simple_reply("好的")])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("你好", self.system_prompt,
                                enabled_tools=[])
        self.assertEqual(text, "好的")
        self.assertEqual(events, [])

    # ═══════════════════════════════════════════════════════════════
    #  Single tool call
    # ═══════════════════════════════════════════════════════════════

    def test_set_reminder_single_call(self):
        """LLM calls set_reminder → tool executed → final reply."""
        client = _mk_deepseek_client([
            _tool_call_response("set_reminder",
                                {"message": "喝水", "delay_seconds": 300}),
            _simple_reply("好的，5分钟后提醒你喝水！"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("5分钟后提醒我喝水", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertEqual(text, "好的，5分钟后提醒你喝水！")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tool_name, "set_reminder")
        self.assertTrue(events[0].success)
        self.assertIn("喝水", events[0].result_text)

    def test_remember_user_fact(self):
        """LLM calls remember_user_fact → executed → final reply."""
        client = _mk_deepseek_client([
            _tool_call_response("remember_user_fact",
                                {"fact": "用户叫Alice", "category": "profile"}),
            _simple_reply("记住了，Alice！"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("记住我叫Alice", self.system_prompt,
                                enabled_tools=["remember_user_fact"])
        self.assertEqual(text, "记住了，Alice！")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tool_name, "remember_user_fact")
        self.assertTrue(events[0].success)

    def test_get_device_status(self):
        """LLM calls get_device_status → executed → final reply."""
        client = _mk_deepseek_client([
            _tool_call_response("get_device_status", {}),
            _simple_reply("设备运行正常，CPU温度52度。"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("设备状态怎么样", self.system_prompt,
                                enabled_tools=["get_device_status"])
        self.assertEqual(text, "设备运行正常，CPU温度52度。")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tool_name, "get_device_status")

    # ═══════════════════════════════════════════════════════════════
    #  Multi-tool calls
    # ═══════════════════════════════════════════════════════════════

    def test_multiple_tools_in_one_response(self):
        """LLM returns multiple tool_calls in one response."""
        client = _mk_deepseek_client([
            _multi_tool_call_response([
                ("set_reminder", {"message": "喝水", "delay_seconds": 300}, "c1"),
                ("remember_user_fact", {"fact": "用户喜欢喝水", "category": "pref"}, "c2"),
            ]),
            _simple_reply("好的，都记住了！"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("记住我喜欢喝水并5分钟后提醒我",
                                self.system_prompt,
                                enabled_tools=["set_reminder",
                                               "remember_user_fact"])
        self.assertEqual(text, "好的，都记住了！")
        self.assertEqual(len(events), 2)

    # ═══════════════════════════════════════════════════════════════
    #  Max 2 loops
    # ═══════════════════════════════════════════════════════════════

    def test_max_two_loops_then_plain_reply(self):
        """Two consecutive tool calls → third call is plain reply."""
        client = _mk_deepseek_client([
            _tool_call_response("set_reminder",
                                {"message": "t1", "delay_seconds": 60}, "c1"),
            _tool_call_response("remember_user_fact",
                                {"fact": "t2", "category": "x"}, "c2"),
            _simple_reply("都处理好了！"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc, max_loops=2)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder",
                                               "remember_user_fact"])
        self.assertEqual(text, "都处理好了！")
        self.assertEqual(len(events), 2)
        # 3 LLM calls: tool1 → tool2 → final
        self.assertGreaterEqual(client.chat_completion.call_count, 3)

    # ═══════════════════════════════════════════════════════════════
    #  Duplicate prevention
    # ═══════════════════════════════════════════════════════════════

    def test_same_tool_same_args_not_called_twice(self):
        """Duplicate tool call with identical args is skipped."""
        client = _mk_deepseek_client([
            _tool_call_response("set_reminder",
                                {"message": "喝水", "delay_seconds": 60}, "c1"),
            # LLM tries to call the same tool again
            _tool_call_response("set_reminder",
                                {"message": "喝水", "delay_seconds": 60}, "c2"),
            _simple_reply("已经设置过了。"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc, max_loops=2)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertEqual(len(events), 1)  # only first executed
        self.assertEqual(self.tool_svc.execute.call_count, 1)

    def test_same_tool_different_args_allowed(self):
        """Different arguments → different dedup key → both execute."""
        client = _mk_deepseek_client([
            _tool_call_response("set_reminder",
                                {"message": "喝水", "delay_seconds": 60}, "c1"),
            _tool_call_response("set_reminder",
                                {"message": "休息", "delay_seconds": 120}, "c2"),
            _simple_reply("都设好了。"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc, max_loops=2)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertEqual(len(events), 2)

    # ═══════════════════════════════════════════════════════════════
    #  Tool failure → graceful fallback
    # ═══════════════════════════════════════════════════════════════

    def test_tool_execution_error_falls_back_to_plain_reply(self):
        """Tool raises → error injected into conversation → LLM replies."""
        svc = _mk_tool_svc()
        svc.execute = MagicMock(side_effect=RuntimeError("DB连接失败"))

        client = _mk_deepseek_client([
            _tool_call_response("remember_user_fact",
                                {"fact": "x"}, "c1"),
            _simple_reply("抱歉，暂时无法保存，请稍后再试。"),
        ])
        tcc = self.TCC(client, self.api_key, svc)
        text, events = tcc.run("记住xxx", self.system_prompt,
                                enabled_tools=["remember_user_fact"])
        self.assertIn("抱歉", text)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].success)
        self.assertEqual(events[0].error_code, "EXECUTION_ERROR")

    def test_tool_timeout_falls_back(self):
        """ToolService returns TIMEOUT → error → plain reply."""
        svc = _mk_tool_svc()
        svc.execute = MagicMock(return_value=ToolResult(
            success=False, error_code="TIMEOUT",
            message="exceeded 5.0s timeout", trace_id="t1"))

        client = _mk_deepseek_client([
            _tool_call_response("get_device_status", {}, "c1"),
            _simple_reply("设备状态暂时获取不到，请稍后再试。"),
        ])
        tcc = self.TCC(client, self.api_key, svc)
        text, events = tcc.run("查设备", self.system_prompt,
                                enabled_tools=["get_device_status"])
        self.assertIn("暂时", text)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].success)
        self.assertEqual(events[0].error_code, "TIMEOUT")

    # ═══════════════════════════════════════════════════════════════
    #  LLM error → returns None
    # ═══════════════════════════════════════════════════════════════

    def test_deepseek_http_error_returns_none(self):
        """DeepSeek returns None → coordinator returns (None, [])."""
        client = _mk_deepseek_client([None])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("你好", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertIsNone(text)
        self.assertEqual(events, [])

    def test_deepseek_raises_exception_returns_none(self):
        """DeepSeek throws → coordinator catches → (None, [])."""
        client = _mk_deepseek_client()
        client.chat_completion = MagicMock(
            side_effect=RuntimeError("connection refused"))
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("你好", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertIsNone(text)
        self.assertEqual(events, [])

    # ═══════════════════════════════════════════════════════════════
    #  Weather is excluded
    # ═══════════════════════════════════════════════════════════════

    def test_weather_not_in_default_schemas(self):
        """Only the 3 sprint tools: set_reminder, remember_user_fact,
        get_device_status.  No weather."""
        svc = _mk_tool_svc()
        tcc = self.TCC(_mk_deepseek_client(), self.api_key, svc)
        schemas = svc.get_tool_schemas(None)
        names = {s["name"] for s in schemas}
        self.assertIn("set_reminder", names)
        self.assertIn("remember_user_fact", names)
        self.assertIn("get_device_status", names)
        self.assertNotIn("get_weather", names)

    def test_weather_excluded_even_when_registered(self):
        """Explicitly enabled tools filter excludes get_weather."""
        svc = _mk_tool_svc(extra_schemas=[{
            "name": "get_weather",
            "description": "查询天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]},
        }])
        client = _mk_deepseek_client([_simple_reply("好的")])
        tcc = self.TCC(client, self.api_key, svc)
        # Only enable the 3 sprint tools — weather is excluded
        text, events = tcc.run("查天气", self.system_prompt,
                                enabled_tools=["set_reminder",
                                               "remember_user_fact",
                                               "get_device_status"])
        self.assertEqual(text, "好的")
        # get_tool_schemas should only have been called with the 3
        call_args = svc.get_tool_schemas.call_args
        enabled = call_args[0][0] if call_args[0] else None
        self.assertIsNotNone(enabled)
        self.assertNotIn("get_weather", enabled)

    # ═══════════════════════════════════════════════════════════════
    #  Messages / conversation history
    # ═══════════════════════════════════════════════════════════════

    def test_conversation_history_passed_to_llm(self):
        """Prior messages are included in the LLM request."""
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀！"},
        ]
        client = _mk_deepseek_client([_simple_reply("今天想做什么？")])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("帮我个忙", self.system_prompt,
                                messages=history,
                                enabled_tools=["set_reminder"])
        call_args = client.chat_completion.call_args
        payload = call_args[0][0]
        msgs = payload.get("messages", [])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "你好")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "帮我个忙")

    # ═══════════════════════════════════════════════════════════════
    #  Edge cases
    # ═══════════════════════════════════════════════════════════════

    def test_malformed_tool_arguments_parsed_as_empty(self):
        """JSON-decode failure → arguments default to {}."""
        resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "get_device_status",
                            "arguments": "not valid json {{{",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        client = _mk_deepseek_client([resp, _simple_reply("好的")])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["get_device_status"])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].success)  # get_device_status has no req args

    def test_empty_tool_calls_list_is_final_reply(self):
        """Response with tool_calls=[] → treated as final content."""
        resp = {
            "choices": [{
                "message": {"role": "assistant", "content": "直接回复",
                             "tool_calls": []},
                "finish_reason": "stop",
            }],
        }
        client = _mk_deepseek_client([resp])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder"])
        self.assertEqual(text, "直接回复")
        self.assertEqual(events, [])

    def test_tool_response_includes_execution_result_in_conversation(self):
        """Tool result is injected back as 'tool' role message."""
        client = _mk_deepseek_client([
            _tool_call_response("set_reminder",
                                {"message": "x", "delay_seconds": 10}, "c1"),
            _simple_reply("done"),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder"])
        # Check that the second LLM call had the tool result
        # The chat_completion is called twice: tool call → final reply
        calls = client.chat_completion.call_args_list
        self.assertGreaterEqual(len(calls), 2)
        # Second call's messages should include a 'tool' role
        second_msgs = calls[1][0][0].get("messages", [])
        tool_roles = [m["role"] for m in second_msgs if m["role"] == "tool"]
        self.assertGreaterEqual(len(tool_roles), 1)

    def test_all_tools_exhausted_returns_plain_reply(self):
        """After all 3 tools are used, remaining tools list is empty."""
        # First call: all 3 tools called at once
        client = _mk_deepseek_client([
            _multi_tool_call_response([
                ("set_reminder", {"message": "a", "delay_seconds": 10}, "c1"),
                ("remember_user_fact", {"fact": "b"}, "c2"),
                ("get_device_status", {}, "c3"),
            ]),
        ])
        tcc = self.TCC(client, self.api_key, self.tool_svc, max_loops=2)
        text, events = tcc.run("test", self.system_prompt,
                                enabled_tools=["set_reminder",
                                               "remember_user_fact",
                                               "get_device_status"])
        self.assertEqual(len(events), 3)
        # After all tools used, the remaining check triggers plain reply
        # The second LLM call is the plain reply
        self.assertGreaterEqual(client.chat_completion.call_count, 2)

    # ═══════════════════════════════════════════════════════════════
    #  Constructor validation
    # ═══════════════════════════════════════════════════════════════

    def test_empty_api_key_raises(self):
        with self.assertRaises(ValueError):
            self.TCC(_mk_deepseek_client(), "", self.tool_svc)

    def test_zero_max_loops_raises(self):
        with self.assertRaises(ValueError):
            self.TCC(_mk_deepseek_client(), self.api_key, self.tool_svc,
                     max_loops=0)

    # ═══════════════════════════════════════════════════════════════
    #  DedupKey stability
    # ═══════════════════════════════════════════════════════════════

    def test_dedup_key_stable_ordering(self):
        from src.tools.tool_calling_coordinator import _dedup_key
        k1 = _dedup_key("set_reminder", {"a": 1, "b": 2})
        k2 = _dedup_key("set_reminder", {"b": 2, "a": 1})
        self.assertEqual(k1, k2)

    def test_dedup_key_different_args(self):
        from src.tools.tool_calling_coordinator import _dedup_key
        k1 = _dedup_key("set_reminder", {"msg": "a"})
        k2 = _dedup_key("set_reminder", {"msg": "b"})
        self.assertNotEqual(k1, k2)

    def test_dedup_key_different_tools(self):
        from src.tools.tool_calling_coordinator import _dedup_key
        k1 = _dedup_key("set_reminder", {"msg": "x"})
        k2 = _dedup_key("remember_user_fact", {"msg": "x"})
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
