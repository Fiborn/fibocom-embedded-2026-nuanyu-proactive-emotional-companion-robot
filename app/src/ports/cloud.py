#!/usr/bin/env python3
"""CloudSyncService — L610 connectivity, data upload/download, event queue.

Agent E owns this file and src/services/cloud_sync_service.py.
"""

from __future__ import annotations
import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Data types ────────────────────────────────────────────────

@dataclass
class SyncRecord:
    """One item queued for upload to the cloud."""

    record_id: str
    data_type: str          # e.g. "chat_log", "device_status", "event"
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    retry_count: int = 0
    last_error: str = ""


@dataclass
class CloudCommand:
    """A command received from the cloud destined for the robot/app."""

    command_id: str
    command_type: str       # e.g. "robot_move", "speak", "alarm"
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


# ── Abstract interface ────────────────────────────────────────

class CloudSyncService(abc.ABC):
    """Abstraction over the L610 4G module and Huawei Cloud IoTDA.

    Phase 2 does NOT open the real UART or connect to Huawei Cloud.
    It defines the interface so Agent E can implement the real
    service in parallel without touching the main process.
    """

    # ── Connectivity ──────────────────────────────────────────

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Is the 4G link currently active?"""
        ...

    @abc.abstractmethod
    def connect(self) -> bool:
        """Attempt to bring up the L610 data stack.  Returns True on success."""
        ...

    @abc.abstractmethod
    def disconnect(self) -> bool:
        """Gracefully tear down the connection."""
        ...

    # ── Upload ────────────────────────────────────────────────

    @abc.abstractmethod
    def enqueue_upload(
        self, data_type: str, payload: Dict[str, Any],
    ) -> str:
        """Queue a record for upload.  Returns the record_id."""
        ...

    @abc.abstractmethod
    def pending_count(self) -> int:
        """How many records are waiting to be uploaded?"""
        ...

    @abc.abstractmethod
    def flush(self, max_items: int = 20) -> List[str]:
        """Attempt to upload pending records.  Returns list of record_ids
        that were successfully uploaded (and removed from the queue).
        """
        ...

    # ── Downlink commands (for robot / app integration) ────────

    @abc.abstractmethod
    def poll_commands(self) -> List[CloudCommand]:
        """Return queued downlink commands from the cloud.

        Phase 2 stub returns an empty list.  Agent E wires real polling
        when the L610 service is integrated.
        """
        ...

    @abc.abstractmethod
    def acknowledge_command(self, command_id: str) -> bool:
        """Mark a command as handled so it is not re-delivered."""
        ...

    # ── Lifecycle ──────────────────────────────────────────────

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Return {connected, pending_count, last_error, …}."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...
