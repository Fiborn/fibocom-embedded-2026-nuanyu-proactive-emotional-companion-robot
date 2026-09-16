#!/usr/bin/env python3
"""NoOpCloudSyncService — Phase 2A placeholder.

Agent E replaces this with the real L610 + Huawei Cloud integration
in Phase 5 (L610 & Cloud Sync).

The NoOp always reports 'disconnected' and never queues or sends data.
"""

from __future__ import annotations
import uuid
from typing import Any, Dict, List

from src.ports.cloud import CloudCommand, CloudSyncService, SyncRecord


class NoOpCloudSyncService(CloudSyncService):
    """Pass-through — no connectivity, no storage, no side effects."""

    def __init__(self):
        self._pending: List[SyncRecord] = []
        self._commands: List[CloudCommand] = []

    # ── Connectivity ──────────────────────────────────────────

    def is_connected(self) -> bool:
        return False

    def connect(self) -> bool:
        return False

    def disconnect(self) -> bool:
        return True

    # ── Upload ────────────────────────────────────────────────

    def enqueue_upload(
        self, data_type: str, payload: Dict[str, Any],
    ) -> str:
        rid = uuid.uuid4().hex[:12]
        self._pending.append(SyncRecord(
            record_id=rid, data_type=data_type, payload=payload))
        return rid

    def pending_count(self) -> int:
        return len(self._pending)

    def flush(self, max_items: int = 20) -> List[str]:
        # NoOp:  never actually uploads — keeps items in queue
        return []

    # ── Downlink ──────────────────────────────────────────────

    def poll_commands(self) -> List[CloudCommand]:
        return list(self._commands)

    def acknowledge_command(self, command_id: str) -> bool:
        before = len(self._commands)
        self._commands = [c for c in self._commands
                          if c.command_id != command_id]
        return len(self._commands) < before

    # ── Lifecycle ──────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        return {
            "backend": "noop",
            "connected": False,
            "pending_count": len(self._pending),
        }

    def close(self) -> None:
        self._pending.clear()
        self._commands.clear()
