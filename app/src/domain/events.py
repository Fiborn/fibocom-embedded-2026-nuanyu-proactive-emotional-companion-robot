#!/usr/bin/env python3
"""Core domain events for the Nuanyu AI companion.

These are the canonical event types exchanged between modules.
All five Phase-2 services consume or produce subtypes of RuntimeEvent.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from enum import Enum


class EventType(str, Enum):
    """Well-known event types.  Services may define additional subtypes."""
    # Perception
    USER_PRESENT = "user_present"
    USER_AWAY = "user_away"
    FACE_DETECTED = "face_detected"
    FACE_LOST = "face_lost"
    EMOTION_CHANGED = "emotion_changed"

    # Interaction
    USER_SPEECH = "user_speech"
    WAKE_WORD = "wake_word"
    SLEEP_ENTERED = "sleep_entered"
    SLEEP_EXITED = "sleep_exited"

    # Proactive
    SILENCE_TIMEOUT = "silence_timeout"
    SIT_TIMEOUT = "sit_timeout"
    REMINDER_DUE = "reminder_due"

    # System
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    CLOUD_SYNC_READY = "cloud_sync_ready"


@dataclass(frozen=True)
class RuntimeEvent:
    """Immutable event raised by one module and consumed by others.

    All fields are optional except event_id, event_type, and timestamp.
    Producers fill what they know; consumers ignore what they don't need.
    """

    event_id: str
    event_type: str               # EventType value or custom string
    timestamp: float              # time.time()
    source: str = ""              # module name e.g. "vision", "asr", "proactive"
    user_id: str = ""             # empty = default robot / anonymous
    session_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
