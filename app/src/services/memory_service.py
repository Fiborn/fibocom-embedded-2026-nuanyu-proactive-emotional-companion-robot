#!/usr/bin/env python3
"""MemoryService implementations — NoOp (Phase 2A) and SQLite (Phase 2B).

Agent A owns this file and src/memory/.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from src.ports.memory import MemoryService

# The application's own directory (this file lives at app/src/services/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════
#  NoOpMemoryService  — Phase 2A pass-through placeholder
# ═══════════════════════════════════════════════════════════════════════

class NoOpMemoryService(MemoryService):
    """Pass-through implementation — zero persistence, zero side effects."""

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []
        self._memories: Dict[str, Any] = {}

    # ── Messages (in-memory only) ─────────────────────────────────

    def save_message(
        self, user_id: str, session_id: str, role: str, content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        msg_id = uuid.uuid4().hex[:12]
        self._messages.append({
            "message_id": msg_id, "user_id": user_id,
            "session_id": session_id, "role": role,
            "content": content, "metadata": metadata or {},
        })
        # Keep only the last 200 in memory
        if len(self._messages) > 200:
            self._messages = self._messages[-200:]
        return msg_id

    def get_recent_messages(
        self, user_id: str, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        user_msgs = [m for m in self._messages if m["user_id"] == user_id]
        return user_msgs[-limit:]

    def get_session_messages(
        self, session_id: str,
    ) -> List[Dict[str, Any]]:
        return [m for m in self._messages if m["session_id"] == session_id]

    # ── Memories ──────────────────────────────────────────────────

    def save_memory(self, user_id: str, key: str, value: Any) -> bool:
        self._memories[f"{user_id}:{key}"] = value
        return True

    def get_memory(self, user_id: str, key: str) -> Optional[Any]:
        return self._memories.get(f"{user_id}:{key}")

    def list_memories(
        self, user_id: str, prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        full_prefix = f"{user_id}:{prefix or ''}"
        return {
            k.split(":", 1)[1]: v
            for k, v in self._memories.items()
            if k.startswith(full_prefix)
        }

    def delete_memory(self, user_id: str, key: str) -> bool:
        full = f"{user_id}:{key}"
        if full in self._memories:
            del self._memories[full]
            return True
        return False

    # ── Lifecycle ──────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        return {
            "backend": "noop",
            "message_count": len(self._messages),
            "memory_count": len(self._memories),
        }

    def close(self) -> None:
        self._messages.clear()
        self._memories.clear()


# ═══════════════════════════════════════════════════════════════════════
#  SqliteMemoryService  — Phase 2B SQLite-backed implementation
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_DB_PATH = os.path.join(APP_DIR, "data", "nuanyu_memory.db")
DEFAULT_CONVERSATION_LIMIT = 24  # 12 user + 12 assistant messages
MEMORIES_DIR = os.path.join(APP_DIR, "memories")


class SqliteMemoryService(MemoryService):
    """SQLite-backed persistence for conversation history and long-term
    memories.

    Features
    --------
    * Cross-restart persistence via SQLite WAL
    * Strict user isolation (every query filtered by ``user_id``)
    * ≥12-turn conversation window (default 24 messages)
    * Long-term memory with importance scoring and keyword indexing
    * Lightweight keyword / importance / recency search
    * Idempotent import of legacy ``memories/{username}.json``
    * Automatic graceful degradation — a broken DB never blocks the
      voice pipeline
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        from src.memory.sqlite_store import SqliteMemoryStore
        self._store = SqliteMemoryStore(db_path)
        self._db_path = db_path

    # ── Messages ───────────────────────────────────────────────────

    def save_message(
        self, user_id: str, session_id: str, role: str, content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self._store.save_message(
            user_id, session_id, role, content, metadata,
        )

    def get_recent_messages(
        self, user_id: str, limit: int = DEFAULT_CONVERSATION_LIMIT,
    ) -> List[Dict[str, Any]]:
        return self._store.get_recent_messages(user_id, limit)

    def get_session_messages(
        self, session_id: str,
    ) -> List[Dict[str, Any]]:
        return self._store.get_session_messages(session_id)

    # ── Long-term memories ────────────────────────────────────────

    def save_memory(
        self, user_id: str, key: str, value: Any,
        importance: float = 0.5, keywords: str = "",
    ) -> bool:
        """Persist a named memory fact.  The ``importance`` and
        ``keywords`` parameters feed the lightweight search engine.
        """
        return self._store.save_memory(user_id, key, value,
                                        importance=importance,
                                        keywords=keywords)

    def get_memory(self, user_id: str, key: str) -> Optional[Any]:
        return self._store.get_memory(user_id, key)

    def list_memories(
        self, user_id: str, prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._store.list_memories(user_id, prefix)

    def delete_memory(self, user_id: str, key: str) -> bool:
        return self._store.delete_memory(user_id, key)

    # ── Convenience / extended API ─────────────────────────────────

    def remember(
        self, user_id: str, content: str, importance: float = 0.8,
    ) -> bool:
        """Explicit "记住" (remember this) — persist a free-text note as a
        long-term memory with auto-generated keywords.

        This is the recommended path for handling the Chinese user
        commands "记住 …" / "帮我记住 …" ("remember …").
        """
        from src.memory.sqlite_store import _extract_keywords_from_text
        key = f"remembered:{uuid.uuid4().hex[:8]}"
        keywords = _extract_keywords_from_text(content)
        return self._store.save_memory(user_id, key, content,
                                        importance=importance,
                                        keywords=keywords)

    def search_memories(
        self, user_id: str, query: str, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Lightweight keyword + importance + recency search over
        long-term memories.

        Returns results sorted by relevance score (best first).
        """
        return self._store.search_memories(user_id, query, limit)

    def import_user_memories(
        self, user_id: str,
        json_path: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Idempotently import a ``memories/{username}.json`` file.

        If *json_path* is not given, looks for
        ``{MEMORIES_DIR}/{user_id}.json``.
        """
        path = json_path or os.path.join(MEMORIES_DIR, f"{user_id}.json")
        return self._store.import_user_json(user_id, path, overwrite)

    def import_all_users(
        self, memories_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Import every ``*.json`` in the memories directory (skips
        ``_users.json``).

        Returns ``{user_id: {imported, skipped, errors}, …}``.
        """
        md = memories_dir or MEMORIES_DIR
        result: Dict[str, Any] = {}
        if not os.path.isdir(md):
            return result
        for fname in sorted(os.listdir(md)):
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            username = fname[:-5]  # strip ".json"
            file_path = os.path.join(md, fname)
            result[username] = self.import_user_memories(username, file_path)
        return result

    # ── Lifecycle ──────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        store_healthy = self._store.healthy
        return {
            "backend": "sqlite",
            "db_path": self._db_path,
            "healthy": store_healthy,
            "last_error": self._store.last_error if not store_healthy else "",
            "message_count": self._store.count_all_messages(),
            "memory_count": self._store.count_memories(),
        }

    def close(self) -> None:
        self._store.close()
