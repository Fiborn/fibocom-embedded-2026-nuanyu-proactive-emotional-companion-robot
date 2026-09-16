#!/usr/bin/env python3
"""Core message / context / result models shared across Phase-2 modules.

These are plain dataclasses — no framework, no external deps.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List


# ═══════════════════════════════════════════════════════════════
#  AgentContext  —  assembled before each LLM call
# ═══════════════════════════════════════════════════════════════

@dataclass
class AgentContext:
    """Everything the agent needs to produce one response.

    Built by the main orchestrator before calling ask_ai().
    """

    user_id: str = ""
    session_id: str = ""
    user_text: str = ""

    # Conversation history (most recent N messages)
    recent_messages: List[Dict[str, str]] = field(default_factory=list)
    # [{role: "user"|"assistant", content: "…"}, …]

    # Long-term memories recalled for this request
    recalled_memories: List[str] = field(default_factory=list)

    # Active persona parameters (from PersonaService)
    persona: Dict[str, Any] = field(default_factory=dict)
    # e.g. {"name": "小陪", "tone": "温柔", "style": "简短中文", ...}

    # Current perceptual snapshot
    perception: Dict[str, Any] = field(default_factory=dict)
    # e.g. {"face_detected": True, "emotion": "happy", "motion": True}

    # Tool schemas available for this call
    available_tools: List[Dict[str, Any]] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
#  AgentAction  —  one decision / output from the agent
# ═══════════════════════════════════════════════════════════════

@dataclass
class AgentAction:
    """A single action the agent has decided to take.

    May be a text reply, a tool call, or both.
    """

    action_type: str = "reply"   # "reply" | "tool_call" | "proactive" | "none"
    text: str = ""
    tool_name: str = ""
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
    priority: int = 5            # 1 (low) .. 10 (high)
    trace_id: str = ""


# ═══════════════════════════════════════════════════════════════
#  ToolResult  —  result of executing one tool
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolResult:
    """Return value from a tool execution.

    success=True + data=… for normal results.
    success=False + error_code / message for failures.
    """

    success: bool = False
    data: Any = None
    error_code: str = ""
    message: str = ""
    trace_id: str = ""
