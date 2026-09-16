#!/usr/bin/env python3
"""ProactiveService — decides *when* to initiate interaction.

Agent D owns this file and src/services/proactive_service.py.
"""

from __future__ import annotations
import abc
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.domain.events import RuntimeEvent


# ── Proactive decision result ─────────────────────────────────

@dataclass
class ProactiveDecision:
    """What the proactive engine decided for a single trigger event."""

    should_act: bool = False
    reason: str = ""                  # human-readable reason for logging
    priority: int = 5                 # 1-10
    suggested_scene: str = "chat"     # scene hint passed to ask_ai()
    suggested_prompt: str = ""        # optional pre-built prompt
    cooldown_seconds: int = 80        # minimum wait before next proactive act


# ── Abstract interface ────────────────────────────────────────

class ProactiveService(abc.ABC):
    """Decide when to initiate an interaction based on perception events.

    This service is a PURE DECISION MAKER.  It never calls TTS or
    the LLM directly.  The main orchestrator reads its decisions
    and acts on them.
    """

    @abc.abstractmethod
    def handle_event(self, event: RuntimeEvent) -> Optional[ProactiveDecision]:
        """Evaluate one perception event and decide whether to act.

        Returns None if the event is irrelevant, or a ProactiveDecision
        if the companion should initiate a conversation.
        """
        ...

    @abc.abstractmethod
    def get_cooldown_remaining(self) -> float:
        """Seconds until the next proactive action is allowed (0 = ready)."""
        ...

    @abc.abstractmethod
    def reset_cooldown(self, seconds: Optional[int] = None) -> None:
        """Reset the cooldown timer (e.g. after a successful user interaction).

        If *seconds* is None, uses the value from the last ProactiveDecision.
        """
        ...

    @abc.abstractmethod
    def record_rejection(self, user_id: str) -> None:
        """Note that the user declined / ignored the last proactive act.

        Future decisions should lower frequency for this user.
        """
        ...

    @abc.abstractmethod
    def set_proactivity_level(self, level: int) -> None:
        """Override the global proactivity level (1-10)."""
        ...

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Return {state, cooldown_remaining, rejection_count, …}."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...
