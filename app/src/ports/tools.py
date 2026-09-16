#!/usr/bin/env python3
"""ToolService — tool registration, schema, validation, execution.

Agent C owns this file and src/services/tool_service.py.
"""

from __future__ import annotations
import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.domain.models import ToolResult


# ── Tool schema (OpenAI-compatible JSON Schema subset) ────────

@dataclass
class ToolDefinition:
    """Metadata for a registered tool.

    ``parameters`` follows the JSON Schema subset that DeepSeek / OpenAI
    function-calling APIs accept.
    """

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    #  { type: "object",
    #    properties: { … },
    #    required: ["…"], }

    # Runtime metadata (not exposed to LLM)
    handler: Optional[Callable[..., ToolResult]] = None
    require_auth: bool = False
    timeout_seconds: float = 15.0


# ── Abstract interface ────────────────────────────────────────

class ToolService(abc.ABC):
    """Register, discover, validate and execute tools.

    Phase 2 does NOT implement actual LLM function-calling loops.
    It exposes tool schemas so that the main agent loop can inject
    them into the LLM request when the architecture design is ready.
    """

    @abc.abstractmethod
    def register_tool(self, tool: ToolDefinition) -> bool:
        """Register one tool.  Returns False if name already exists."""
        ...

    @abc.abstractmethod
    def unregister_tool(self, name: str) -> bool:
        """Remove a tool by name."""
        ...

    @abc.abstractmethod
    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Look up a tool definition (including handler)."""
        ...

    @abc.abstractmethod
    def list_tools(self) -> List[ToolDefinition]:
        """Return every registered tool."""
        ...

    @abc.abstractmethod
    def get_tool_schemas(
        self, names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return JSON-Schema-compatible descriptions for the LLM.

        When *names* is None, return all.
        Result is a list of {name, description, parameters} dicts.
        """
        ...

    @abc.abstractmethod
    def validate_arguments(
        self, name: str, arguments: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Check that *arguments* match the tool's parameter schema.

        Returns (ok, error_message).
        """
        ...

    @abc.abstractmethod
    def execute(
        self, name: str, arguments: Dict[str, Any], trace_id: str = "",
    ) -> ToolResult:
        """Run a tool synchronously.  Must not block the main loop
        for more than the tool's configured timeout.
        """
        ...

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...
