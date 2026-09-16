#!/usr/bin/env python3
"""MemoryService — conversation history and long-term memory persistence.

Agent A owns this file and src/services/memory_service.py.
"""

from __future__ import annotations
import abc
from typing import Any, Dict, List, Optional, Tuple


class MemoryService(abc.ABC):
    """Persistent storage for user/assistant messages and long-term memories.

    Phase 2 minimum:  SQLite-backed with ≥10-turn conversation history
    that survives process restart.

    Phase 2 does NOT include:
      - vector databases / embeddings
      - semantic search over full history
      - memory summarisation (use last-N window instead)
    """

    # ── Conversation messages ──────────────────────────────────

    @abc.abstractmethod
    def save_message(
        self, user_id: str, session_id: str, role: str, content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist one message. Returns a unique message_id."""
        ...

    @abc.abstractmethod
    def get_recent_messages(
        self, user_id: str, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return the most recent messages for *user_id*.

        Each dict:  {message_id, role, content, created_at, …}
        Ordered oldest → newest.
        """
        ...

    @abc.abstractmethod
    def get_session_messages(
        self, session_id: str,
    ) -> List[Dict[str, Any]]:
        """Return all messages in one session."""
        ...

    # ── Long-term memories (key-value notes) ───────────────────

    @abc.abstractmethod
    def save_memory(
        self, user_id: str, key: str, value: Any,
    ) -> bool:
        """Persist a named memory fact.  Overwrites if key exists."""
        ...

    @abc.abstractmethod
    def get_memory(self, user_id: str, key: str) -> Optional[Any]:
        """Retrieve a single memory by key."""
        ...

    @abc.abstractmethod
    def list_memories(
        self, user_id: str, prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return all memories for *user_id*, optionally filtered by key prefix."""
        ...

    @abc.abstractmethod
    def delete_memory(self, user_id: str, key: str) -> bool:
        """Remove a memory. Returns True if it existed."""
        ...

    # ── Lifecycle ──────────────────────────────────────────────

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Return {backend, message_count, memory_count, …}."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        """Release any resources (idempotent)."""
        ...
