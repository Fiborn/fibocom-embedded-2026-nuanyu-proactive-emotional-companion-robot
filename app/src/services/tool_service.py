#!/usr/bin/env python3
"""ToolService — tool registration, schema, validation, execution with timeout.

Phase 2B: real implementation replacing the Phase 2A NoOp placeholder.
Agent C owns this file (per Phase 2 interface contracts).

All external dependencies (weather, device status, reminder persistence,
user-fact storage) are injected as callables through
:func:`register_builtin_tools`.  ToolService itself has zero imports from
MemoryService, nuanyu_web, or real network libraries.
"""

from __future__ import annotations
import concurrent.futures
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.domain.models import ToolResult
from src.ports.tools import ToolDefinition, ToolService


# ═══════════════════════════════════════════════════════════════════
#  Provider type aliases  (injected, no real I/O here)
# ═══════════════════════════════════════════════════════════════════

WeatherProvider = Callable[[str], Dict[str, Any]]
"""Signature:  def get_weather(city: str) -> dict"""

DeviceStatusProvider = Callable[[], Dict[str, Any]]
"""Signature:  def get_device_status() -> dict"""

ReminderHandler = Callable[[str, int, str], Dict[str, Any]]
"""Signature:  def set_reminder(message: str, delay_seconds: int, priority: str) -> dict"""

UserFactHandler = Callable[[str, str], Dict[str, Any]]
"""Signature:  def remember_user_fact(fact: str, category: str) -> dict"""


# ═══════════════════════════════════════════════════════════════════
#  Built-in tool JSON Schemas  (OpenAI / DeepSeek function-calling subset)
# ═══════════════════════════════════════════════════════════════════

BUILTIN_TOOLS: Dict[str, Dict[str, Any]] = {
    "set_reminder": {
        "name": "set_reminder",
        "description": "设置一个提醒，在指定秒数后通知用户",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "提醒内容",
                },
                "delay_seconds": {
                    "type": "integer",
                    "description": "延迟秒数，最少 1 秒",
                    "minimum": 1,
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "优先级",
                    "default": "normal",
                },
            },
            "required": ["message", "delay_seconds"],
        },
    },
    "get_weather": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，例如 '北京' 或 '上海'",
                },
            },
            "required": ["city"],
        },
    },
    "remember_user_fact": {
        "name": "remember_user_fact",
        "description": "记住用户的一个事实或偏好",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "要记住的事实内容",
                },
                "category": {
                    "type": "string",
                    "description": "事实分类标签",
                    "default": "general",
                },
            },
            "required": ["fact"],
        },
    },
    "get_device_status": {
        "name": "get_device_status",
        "description": "获取设备硬件和连接的当前状态",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ═══════════════════════════════════════════════════════════════════
#  JSON Schema parameter validation  (stdlib only, no jsonschema pkg)
# ═══════════════════════════════════════════════════════════════════

_VALID_TYPES: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _validate_param(name: str, value: Any, prop_schema: Dict[str, Any]) -> Optional[str]:
    """Validate one parameter against its JSON Schema property definition.

    Returns an error string on failure, or ``None`` if valid.
    """
    expected_type = prop_schema.get("type")

    # ── Type check ──────────────────────────────────────────────
    if expected_type:
        py_type = _VALID_TYPES.get(expected_type)
        if py_type is not None:
            if not isinstance(value, py_type):
                return (
                    f"'{name}' must be {expected_type}, "
                    f"got {type(value).__name__}"
                )
            # Reject bool when integer is expected (bool is a subclass of int)
            if expected_type == "integer" and isinstance(value, bool):
                return f"'{name}' must be integer, got boolean"

    # ── Enum constraint ─────────────────────────────────────────
    allowed = prop_schema.get("enum")
    if allowed is not None and value not in allowed:
        return f"'{name}' must be one of {allowed}, got {value!r}"

    # ── Numeric constraints ─────────────────────────────────────
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = prop_schema.get("minimum")
        if minimum is not None and value < minimum:
            return f"'{name}' minimum is {minimum}, got {value}"
        maximum = prop_schema.get("maximum")
        if maximum is not None and value > maximum:
            return f"'{name}' maximum is {maximum}, got {value}"

    return None


def validate_tool_arguments(
    tool: ToolDefinition, arguments: Dict[str, Any]
) -> Tuple[bool, str]:
    """Pure-function validation of arguments against a tool's parameter schema.

    Returns ``(ok, error_message)``.  Callers may use this standalone
    without instantiating a ToolService.
    """
    params_schema = tool.parameters or {}
    properties = params_schema.get("properties", {})
    required: List[str] = params_schema.get("required", [])

    # 1. Required-parameter presence
    missing = [k for k in required if k not in arguments]
    if missing:
        return False, f"missing required arguments: {missing}"

    # 2. Per-argument validation against property schemas
    for key, value in arguments.items():
        prop = properties.get(key)
        if prop is None:
            # Unknown keys are silently ignored (forward-compatible)
            continue
        err = _validate_param(key, value, prop)
        if err:
            return False, err

    return True, ""


# ═══════════════════════════════════════════════════════════════════
#  NoOpToolService  (Phase 2B production implementation)
# ═══════════════════════════════════════════════════════════════════

class NoOpToolService(ToolService):
    """Production ToolService with timeout enforcement and validation.

    The class name is kept as ``NoOpToolService`` for backwards compatibility
    with ``RuntimeServices._load_phase2_services()`` which dynamically
    instantiates this class by name.

    External dependencies (weather lookup, device probing, reminder storage,
    user-fact persistence) are injected via :func:`register_builtin_tools`.
    The service itself performs no real I/O.
    """

    # Default thread-pool size — tools are CPU-trivial (they call injected
    # providers), so a small pool avoids thread-creation churn.
    _DEFAULT_MAX_WORKERS = 4

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._closed = False

    # ── Lazy executor ────────────────────────────────────────────

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._DEFAULT_MAX_WORKERS,
                thread_name_prefix="tool-",
            )
        return self._executor

    # ── Registration ─────────────────────────────────────────────

    def register_tool(self, tool: ToolDefinition) -> bool:
        """Register one tool.  Returns ``False`` if *tool.name* already exists."""
        if tool.name in self._tools:
            return False
        self._tools[tool.name] = tool
        return True

    def unregister_tool(self, name: str) -> bool:
        """Remove a tool by name.  Returns ``False`` if not registered."""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Look up a tool definition (including its handler)."""
        return self._tools.get(name)

    def list_tools(self) -> List[ToolDefinition]:
        """Return every registered tool."""
        return list(self._tools.values())

    # ── JSON Schema export ───────────────────────────────────────

    def get_tool_schemas(
        self, names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return JSON-Schema-compatible function descriptions for the LLM.

        Each entry is ``{name, description, parameters}`` — the subset
        that DeepSeek / OpenAI function-calling endpoints expect.
        """
        targets: set = set(names) if names is not None else set(self._tools.keys())
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
            if t.name in targets
        ]

    # ── Validation ───────────────────────────────────────────────

    def validate_arguments(
        self, name: str, arguments: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Check that *arguments* match the tool's parameter JSON Schema.

        Returns ``(ok, error_message)``.

        Checks performed:
        * Required parameters are present
        * Parameter types match the schema (string / integer / number / boolean)
        * Enum values are respected
        * Numeric ``minimum`` / ``maximum`` bounds
        """
        tool = self._tools.get(name)
        if tool is None:
            return False, f"unknown tool: {name}"
        return validate_tool_arguments(tool, arguments)

    # ── Execution ────────────────────────────────────────────────

    def execute(
        self, name: str, arguments: Dict[str, Any], trace_id: str = "",
    ) -> ToolResult:
        """Run a tool synchronously, enforcing its ``timeout_seconds``.

        The handler runs on a thread-pool thread so the calling thread is
        never blocked longer than the configured timeout.  On timeout
        the result is ``error_code=TIMEOUT`` — the handler thread continues
        to run but its result is discarded.

        Error codes returned:
        * ``SERVICE_CLOSED`` — :meth:`close` already called
        * ``UNKNOWN_TOOL`` — *name* is not registered
        * ``INVALID_ARGS`` — argument validation failed
        * ``NO_HANDLER`` — tool has no callable handler
        * ``TIMEOUT`` — handler exceeded ``tool.timeout_seconds``
        * ``EXECUTION_ERROR`` — handler raised an exception
        """
        if self._closed:
            return ToolResult(
                success=False, error_code="SERVICE_CLOSED",
                message="ToolService has been closed", trace_id=trace_id,
            )

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                success=False, error_code="UNKNOWN_TOOL",
                message=f"tool '{name}' not found", trace_id=trace_id,
            )

        ok, err = self.validate_arguments(name, arguments)
        if not ok:
            return ToolResult(
                success=False, error_code="INVALID_ARGS",
                message=err, trace_id=trace_id,
            )

        if tool.handler is None:
            return ToolResult(
                success=False, error_code="NO_HANDLER",
                message=f"tool '{name}' has no handler", trace_id=trace_id,
            )

        timeout = max(0.1, float(tool.timeout_seconds))
        t0 = time.perf_counter()

        # Filter arguments to only those the tool's schema defines —
        # extra/unknown keys are silently accepted at validation time
        # but must not be forwarded to the handler (they would cause
        # unexpected-keyword-argument errors).
        schema_props = (tool.parameters or {}).get("properties", {})
        filtered_args = {k: v for k, v in arguments.items()
                         if k in schema_props}

        try:
            future = self._get_executor().submit(tool.handler, **filtered_args)
            result = future.result(timeout=timeout)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            # ── Normalise return value into ToolResult ───────────
            if isinstance(result, ToolResult):
                tr = result
                tr.trace_id = trace_id or tr.trace_id
            else:
                tr = ToolResult(
                    success=True,
                    data={"result": result},
                    trace_id=trace_id,
                )

            # Attach timing metadata
            if tr.data is None:
                tr.data = {}
            if isinstance(tr.data, dict):
                tr.data.setdefault("execution_ms", round(elapsed_ms, 1))
            return tr

        except concurrent.futures.TimeoutError:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            return ToolResult(
                success=False,
                error_code="TIMEOUT",
                message=(
                    f"tool '{name}' exceeded {timeout:.1f}s timeout "
                    f"(elapsed {elapsed_ms:.0f}ms)"
                ),
                trace_id=trace_id,
                data={"execution_ms": round(elapsed_ms, 1)},
            )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            return ToolResult(
                success=False,
                error_code="EXECUTION_ERROR",
                message=f"{type(exc).__name__}: {str(exc)[:200]}",
                trace_id=trace_id,
                data={"execution_ms": round(elapsed_ms, 1)},
            )

    # ── Lifecycle ────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        return {
            # Despite the legacy class name, this is the production local
            # executor with validation, timeouts and injected handlers.
            "backend": "local",
            "tool_count": len(self._tools),
            "closed": self._closed,
        }

    def close(self) -> None:
        """Idempotent shutdown — release thread pool, keep registry alive.

        Stateless operations (validation, schema export, listing) remain
        available after close.  Only :meth:`execute` is gated, because it
        requires the thread-pool executor.
        """
        if self._closed:
            return
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None


# ═══════════════════════════════════════════════════════════════════
#  Built-in tool registration  (injects all external deps)
# ═══════════════════════════════════════════════════════════════════

def register_builtin_tools(
    svc: ToolService,
    weather_provider: WeatherProvider,
    device_status_provider: DeviceStatusProvider,
    reminder_handler: ReminderHandler,
    fact_handler: UserFactHandler,
) -> None:
    """Register the four Phase 2 built-in tools with injected providers.

    Callers (tests, RuntimeServices, main) are responsible for supplying
    real or fake implementations of each provider.  ToolService never
    imports MemoryService, network clients, or hardware probes directly.

    Parameters
    ----------
    svc:
        A :class:`ToolService` instance (any implementation).
    weather_provider:
        ``(city: str) -> dict`` — real: HTTP weather API; fake: canned data.
    device_status_provider:
        ``() -> dict`` — real: /proc + /sys readers; fake: canned data.
    reminder_handler:
        ``(message, delay_seconds, priority) -> dict`` — real: timer thread;
        fake: list append.
    fact_handler:
        ``(fact, category) -> dict`` — real: MemoryService.save_memory;
        fake: list append.
    """
    # ── set_reminder ────────────────────────────────────────────
    def _set_reminder(message: str, delay_seconds: int,
                      priority: str = "normal") -> ToolResult:
        data = reminder_handler(message, delay_seconds, priority)
        return ToolResult(
            success=True, data=data,
            message=f"reminder set: {message[:50]}",
        )

    svc.register_tool(ToolDefinition(
        name="set_reminder",
        description=BUILTIN_TOOLS["set_reminder"]["description"],
        parameters=BUILTIN_TOOLS["set_reminder"]["parameters"],
        handler=_set_reminder,
        timeout_seconds=10.0,
    ))

    # ── get_weather ─────────────────────────────────────────────
    def _get_weather(city: str) -> ToolResult:
        data = weather_provider(city)
        return ToolResult(
            success=True, data=data,
            message=f"weather for {city}",
        )

    svc.register_tool(ToolDefinition(
        name="get_weather",
        description=BUILTIN_TOOLS["get_weather"]["description"],
        parameters=BUILTIN_TOOLS["get_weather"]["parameters"],
        handler=_get_weather,
        timeout_seconds=10.0,
    ))

    # ── remember_user_fact ──────────────────────────────────────
    def _remember_user_fact(fact: str, category: str = "general") -> ToolResult:
        data = fact_handler(fact, category)
        return ToolResult(
            success=True, data=data,
            message=f"remembered: {fact[:50]}",
        )

    svc.register_tool(ToolDefinition(
        name="remember_user_fact",
        description=BUILTIN_TOOLS["remember_user_fact"]["description"],
        parameters=BUILTIN_TOOLS["remember_user_fact"]["parameters"],
        handler=_remember_user_fact,
        timeout_seconds=5.0,
    ))

    # ── get_device_status ───────────────────────────────────────
    def _get_device_status() -> ToolResult:
        data = device_status_provider()
        return ToolResult(
            success=True, data=data,
            message="device status retrieved",
        )

    svc.register_tool(ToolDefinition(
        name="get_device_status",
        description=BUILTIN_TOOLS["get_device_status"]["description"],
        parameters=BUILTIN_TOOLS["get_device_status"]["parameters"],
        handler=_get_device_status,
        timeout_seconds=5.0,
    ))
