#!/usr/bin/env python3
"""ToolCallingCoordinator — standalone function-calling loop for DeepSeek.

Design principles
-----------------
* Zero imports from nuanyu_web, RuntimeServices, ASR/TTS/Memory/Persona.
* Takes DeepSeek client + ToolService as constructor dependencies.
* Returns final text ready for the existing streaming-TTS pipeline.
* Degrades gracefully: any tool failure → plain reply; any loop error → None.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# ── Lightweight event record ───────────────────────────────────────


@dataclass
class ToolCallEvent:
    """One tool-execution event for observability / logging."""
    tool_name: str
    arguments: Dict[str, Any]
    success: bool
    result_text: str = ""
    error_code: str = ""
    elapsed_ms: float = 0.0
    trace_id: str = ""


# ── Coordinator ────────────────────────────────────────────────────


class ToolCallingCoordinator:
    """Standalone DeepSeek function-calling loop.

    Usage sketch (outside nuanyu_web)::

        from src.tools import ToolCallingCoordinator

        tcc = ToolCallingCoordinator(
            deepseek_client=client,
            api_key=os.environ["DEEPSEEK_API_KEY"],
            tool_service=rt.tool_service,
            model="deepseek-v4-flash",
        )
        final_text, events = tcc.run(
            user_text="帮我定一个 5 分钟后的提醒",   # "set me a reminder in 5 minutes"
            system_prompt="你是小陪，温柔简短的桌面陪伴机器人。",  # Persona prompt (Chinese product)
            messages=[...],                     # conversation history
            enabled_tools=["set_reminder", "remember_user_fact",
                           "get_device_status"],
        )
        # *final_text* ← assistant reply (plain str).
        # *events*     ← List[ToolCallEvent] for logging.

    Features
    --------
    * Max 2 tool-calling loops (configurable).
    * Duplicate prevention: same (tool_name, arguments_hash) won't fire twice.
    * Tool timeout / failure → error injected into conversation; LLM can
      retry or fall back to a plain reply.
    * Fatal coordinator error → returns (None, []) so caller can fall back
      to the existing non-tool ask_ai() path.
    """

    # ── API budgets ──────────────────────────────────────────────

    _DEFAULT_MAX_LOOPS = 2
    _DEFAULT_MODEL = "deepseek-v4-flash"
    _DEFAULT_TEMPERATURE = 0.5
    _TOOL_MAX_TOKENS = 200      # tool-mode replies
    _FINAL_MAX_TOKENS = 120     # final answer (kept short for TTS)
    _DEFAULT_TIMEOUT = 30       # seconds for LLM HTTP call

    def __init__(
        self,
        deepseek_client: Any,
        api_key: str,
        tool_service: Any,          # ToolService-compatible
        model: str = _DEFAULT_MODEL,
        max_loops: int = _DEFAULT_MAX_LOOPS,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if max_loops < 1:
            raise ValueError("max_loops must be >= 1")
        self._client = deepseek_client
        self._api_key = api_key
        self._tool_service = tool_service
        self._model = model
        self._max_loops = max_loops

    # ── Public API ────────────────────────────────────────────────

    def run(
        self,
        user_text: str,
        system_prompt: str,
        messages: Optional[List[Dict[str, str]]] = None,
        enabled_tools: Optional[List[str]] = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        timeout_s: Optional[float] = None,
    ) -> Tuple[Optional[str], List[ToolCallEvent]]:
        """Execute the tool-calling loop and return final reply text.

        Parameters
        ----------
        user_text:
            The latest user utterance.
        system_prompt:
            The full system prompt (persona, scene, context).
        messages:
            Prior conversation history  ``[{role, content}, …]``.
        enabled_tools:
            Subset of registered tool names to expose.  ``None`` → all.
        temperature:
            LLM sampling temperature.

        Returns
        -------
        (final_text, events):
            *final_text* — the assistant's final reply (may be empty).
            *events*     — ordered list of tool-call events for logging.
            On fatal error returns ``(None, [])``.
        """
        try:
            return self._run_inner(
                user_text, system_prompt, messages or [],
                enabled_tools, temperature, timeout_s,
            )
        except Exception as exc:
            print("[ToolCallingCoordinator] fatal error: %s" % exc, flush=True)
            return None, []

    def _run_inner(
        self,
        user_text: str,
        system_prompt: str,
        messages: List[Dict[str, str]],
        enabled_tools: Optional[List[str]],
        temperature: float,
        timeout_s: Optional[float] = None,
    ) -> Tuple[Optional[str], List[ToolCallEvent]]:
        events: List[ToolCallEvent] = []
        executed: Set[str] = set()   # "tool_name:args_hash"
        # Overall hard deadline: keeps tool calls from stalling the request/ASR
        # thread (default max_loops * per-round budget); timeout → (None, []) fallback.
        deadline = time.monotonic() + (timeout_s if timeout_s is not None
                                       else self._max_loops * 20.0)

        # ── Build the initial conversation ────────────────────────
        conversation: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        if messages:
            conversation.extend(messages)
        conversation.append({"role": "user", "content": user_text})

        # ── Get tool schemas ──────────────────────────────────────
        tool_schemas = self._tool_service.get_tool_schemas(enabled_tools)
        if not tool_schemas:
            # No tools available — skip straight to plain reply
            return self._plain_reply(conversation, temperature), events

        # Convert to OpenAI function-calling format
        tools_payload = [
            {"type": "function", "function": s} for s in tool_schemas
        ]

        # ── Main loop ─────────────────────────────────────────────
        for loop_idx in range(self._max_loops):
            if time.monotonic() > deadline:
                print("[ToolCallingCoordinator] overall timeout, abandoning",
                      flush=True)
                return None, events
            # Build request payload
            payload = {
                "model": self._model,
                "messages": list(conversation),
                "temperature": temperature,
                "max_tokens": self._TOOL_MAX_TOKENS,
            }

            # Include tools on the first pass; on subsequent passes
            # only if there are still unfired tools.
            remaining = self._remaining_tools(tool_schemas, executed)
            if remaining:
                payload["tools"] = [
                    {"type": "function", "function": s} for s in remaining
                ]
                payload["tool_choice"] = "auto"
            else:
                # All tools exhausted — plain reply
                return self._plain_reply(conversation, temperature), events

            # Call LLM
            resp = self._client.chat_completion(payload, self._api_key)
            if resp is None:
                return None, events

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message", {})

            # No tool_calls → final answer
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                content = msg.get("content", "") or ""
                if content:
                    conversation.append(
                        {"role": "assistant", "content": content})
                return content, events

            # ── Execute tool calls ─────────────────────────────────
            # Add the assistant message (with tool_calls) to history
            conversation.append(msg)

            for tc in tool_calls:
                tc_id = tc.get("id", uuid.uuid4().hex[:8])
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                args_str = func.get("arguments", "{}")

                # Parse arguments
                try:
                    arguments = json.loads(args_str) if isinstance(
                        args_str, str) else (args_str or {})
                except json.JSONDecodeError:
                    arguments = {}

                # ── Weather-specific logging ────────────────────────
                if tool_name == "get_weather":
                    city = arguments.get("city", "?")
                    print(f"[WEATHER] intent_detected tool=get_weather city={city}",
                          flush=True)

                # Duplicate check
                dedup_key = _dedup_key(tool_name, arguments)
                if dedup_key in executed:
                    continue
                executed.add(dedup_key)

                # Execute
                t0 = time.perf_counter()
                evt = ToolCallEvent(
                    tool_name=tool_name,
                    arguments=arguments,
                    success=False,
                    trace_id=tc_id,
                )
                try:
                    result = self._tool_service.execute(
                        tool_name, arguments, trace_id=tc_id)
                    evt.elapsed_ms = (time.perf_counter() - t0) * 1000
                    evt.success = result.success
                    evt.result_text = result.message or ""
                    evt.error_code = result.error_code or ""
                except Exception as exc:
                    evt.elapsed_ms = (time.perf_counter() - t0) * 1000
                    evt.error_code = "EXECUTION_ERROR"
                    evt.result_text = str(exc)[:200]
                    # Build a synthetic error ToolResult for the LLM
                    from src.domain.models import ToolResult
                    result = ToolResult(
                        success=False,
                        error_code="EXECUTION_ERROR",
                        message=str(exc)[:200],
                        trace_id=tc_id,
                    )
                events.append(evt)

                # ── Weather result logging ────────────────────────
                if tool_name == "get_weather":
                    city = arguments.get("city", "?")
                    if evt.success:
                        data = result.data or {}
                        temp = data.get("temperature_c", "?")
                        cond = data.get("weather_cn", "?")
                        print(f"[WEATHER] tool_called city={city} "
                              f"success=True temp={temp}°C cond={cond} "
                              f"elapsed={evt.elapsed_ms:.0f}ms", flush=True)
                    else:
                        print(f"[WEATHER] provider_failed city={city} "
                              f"error={evt.error_code} elapsed={evt.elapsed_ms:.0f}ms",
                              flush=True)

                # Add tool result to conversation
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "success": result.success,
                        "data": result.data,
                        "message": result.message,
                        "error": result.error_code or "",
                    }, ensure_ascii=False, default=str),
                })

        # ── Loop exhausted → final plain reply ────────────────────
        if time.monotonic() > deadline:
            print("[ToolCallingCoordinator] overall timeout at loop end, abandoning",
                  flush=True)
            return None, events
        return self._plain_reply(conversation, temperature), events

    # ── Helpers ───────────────────────────────────────────────────

    def _plain_reply(
        self, conversation: List[Dict[str, Any]], temperature: float,
    ) -> str:
        """Final non-tool reply from the LLM."""
        payload = {
            "model": self._model,
            "messages": list(conversation),
            "temperature": temperature,
            "max_tokens": self._FINAL_MAX_TOKENS,
        }
        try:
            resp = self._client.chat_completion(payload, self._api_key)
            if resp is None:
                return ""
            choice = (resp.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content", "") or ""
            return content
        except Exception:
            return ""

    @staticmethod
    def _remaining_tools(
        schemas: List[Dict[str, Any]],
        executed: Set[str],
    ) -> List[Dict[str, Any]]:
        """Filter schemas to only those not yet executed (by name prefix)."""
        executed_names: Set[str] = set()
        for key in executed:
            # key format: "tool_name:args_hash"
            name = key.split(":", 1)[0]
            executed_names.add(name)
        return [s for s in schemas if s.get("name") not in executed_names]


# ── Dedup helper ────────────────────────────────────────────────────


def _dedup_key(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Stable dedup key: tool_name + hash of canonicalised arguments."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                           default=str)
    h = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return "%s:%s" % (tool_name, h)
