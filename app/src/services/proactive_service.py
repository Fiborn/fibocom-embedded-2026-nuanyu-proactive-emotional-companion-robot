#!/usr/bin/env python3
"""ProactiveService — rule-based proactive decision engine.

Phase 4 (Agent D): Perception event → decision pipeline.
Decides when to initiate interaction based on perception events.
Pure decision maker — never calls TTS, LLM, vision models, or frontend.

NoOpProactiveService is retained for Phase 2A backward compatibility
(RuntimeServices + test_phase2_interfaces.py).  ProactiveServiceImpl is
the real implementation that the Master Architect will wire in later.
"""

from __future__ import annotations
import collections
import datetime
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from src.domain.events import RuntimeEvent, EventType
from src.ports.proactive import ProactiveDecision, ProactiveService


# ═══════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════

# Emotions that trigger care
CARE_EMOTIONS: Set[str] = {
    "sad", "fear", "angry",
    "悲伤", "害怕", "生气", "恐惧",
    "sorrow", "disgust", "厌恶",
}
EMOTION_STREAK_THRESHOLD: int = 3       # consecutive hits before care fires
EMOTION_WINDOW_SIZE: int = 10           # max emotion history per user

# Time thresholds (seconds) — calibrated at level 5, scaled by level
SILENCE_TIMEOUT_BASE: float = 300.0     # 5 min  @ level 5
SIT_TIMEOUT_BASE: float = 600.0         # 10 min @ level 5
GREETING_COOLDOWN_BASE: float = 120.0   # 2 min  @ level 5

# Cooldown envelope (seconds)
COOLDOWN_MIN: float = 30.0              # level 10
COOLDOWN_MAX: float = 600.0             # level 1

# Emotion-care & silence-care cooldowns (separate from the main cooldown)
EMOTION_CARE_COOLDOWN_BASE: float = 180.0   # 3 min @ level 5
SILENCE_CARE_COOLDOWN_MULT: float = 2.0     # × main cooldown

# Daily limits
DAILY_LEVEL_1: int = 2
DAILY_LEVEL_5: int = 12
DAILY_LEVEL_10: int = 30

# Dedup window (seconds) — same reason to same user within this window is skipped
DEDUP_WINDOW: float = 60.0

# Rejection backoff
REJECTION_BACKOFF_BASE: float = 120.0      # seconds at level 5
REJECTION_BACKOFF_FACTOR: float = 1.5
MAX_REJECTION_STREAK: int = 5
REJECTION_BLOCK_PRIORITY: int = 8       # block actions below this priority when streak ≥ 3


# ═══════════════════════════════════════════════════════════════════
#  Helper
# ═══════════════════════════════════════════════════════════════════

def _level_scale(level: int, base: float) -> float:
    """Scale a base value inversely to proactivity level.

    Calibrated so level 5 = 1.0× base, level 1 = 2.0×, level 10 = 0.4×.
    """
    if level <= 5:
        ratio = (5.0 - level) / 4.0   # 1.0 @ level 5, 0.0 @ level 1
        factor = 1.0 + ratio          # 2.0 @ level 1, 1.0 @ level 5
    else:
        ratio = (level - 5.0) / 5.0   # 0.0 @ level 5, 1.0 @ level 10
        factor = 1.0 - ratio * 0.6    # 1.0 @ level 5, 0.4 @ level 10
    return base * factor


def _level_interpolate(level: int, v1: float, v10: float) -> float:
    """Interpolate value between level-1 and level-10 extremes."""
    ratio = (level - 1) / 9.0
    return v1 + ratio * (v10 - v1)


# ═══════════════════════════════════════════════════════════════════
#  NoOpProactiveService  (Phase 2A backward-compat stub)
# ═══════════════════════════════════════════════════════════════════

class NoOpProactiveService(ProactiveService):
    """Never triggers proactive actions — pass-through for existing logic.

    Retained for backward compatibility with RuntimeServices dynamic
    loader and test_phase2_interfaces.py.
    """

    def __init__(self):
        self._rejection_count: Dict[str, int] = {}
        self._level: int = 5
        self._cooldown_until: float = 0.0

    def handle_event(self, event: RuntimeEvent) -> Optional[ProactiveDecision]:
        return None   # never recommend action

    def get_cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def reset_cooldown(self, seconds: Optional[int] = None) -> None:
        self._cooldown_until = time.time() + (seconds or 80)

    def record_rejection(self, user_id: str) -> None:
        self._rejection_count[user_id] = (
            self._rejection_count.get(user_id, 0) + 1)

    def set_proactivity_level(self, level: int) -> None:
        self._level = max(1, min(10, int(level)))

    def health(self) -> Dict[str, Any]:
        return {
            "backend": "noop",
            "level": self._level,
            "cooldown_remaining_s": self.get_cooldown_remaining(),
            "rejection_count": sum(self._rejection_count.values()),
        }

    def close(self) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════
#  ProactiveServiceImpl  —  real rule-based engine
# ═══════════════════════════════════════════════════════════════════

class ProactiveServiceImpl(ProactiveService):
    """Rule-based proactive decision engine.

    Evaluates perception events and returns a ProactiveDecision when
    the companion should initiate interaction.

    Rules (evaluated in priority order):
      1. REMINDER_DUE         → priority 9
      2. Timed presence change → priority 8
      3. Greeting on entry     → priority 8
      4. Positive expression   → priority 7
      5. Emotion care          → priority 7
      6. Silence / sit         → priority 4

    Gate checks applied before every decision:
      - Global enabled flag
      - Cooldown timer
      - Daily per-user limit
      - Dedup (same reason, same user, short window)
      - Rejection backoff (lowers frequency after user rejections)
    """

    def __init__(self, time_fn: Optional[Callable[[], float]] = None):
        self._time: Callable[[], float] = time_fn or time.time

        # ── Global config ──
        self._enabled: bool = True
        self._level: int = 5                           # 1-10

        # ── Cooldown ──
        self._cooldown_until: float = 0.0
        self._last_decision_cooldown: int = 80

        # ── Per-user emotion history ──
        self._emotion_history: Dict[str, Deque[Tuple[float, str]]] = {}

        # ── Per-user presence / interaction tracking ──
        self._user_present_since: Dict[str, float] = {}
        self._last_interaction: Dict[str, float] = {}

        # ── Greeted users (per session / until close) ──
        self._greeted_users: Set[str] = set()

        # ── Last proactive decision per user (for dedup) ──
        self._last_proactive: Dict[str, Tuple[float, str]] = {}

        # ── Daily tracking: (user_id, "YYYY-MM-DD") → count ──
        self._daily_counts: Dict[Tuple[str, str], int] = collections.defaultdict(int)

        # ── Rejection tracking ──
        self._rejection_count: Dict[str, int] = collections.defaultdict(int)
        self._rejection_streak: Dict[str, int] = collections.defaultdict(int)

        # ── Pending reminders per user ──
        self._pending_reminders: Dict[str, List[Dict[str, Any]]] = (
            collections.defaultdict(list)
        )

    # ═══════════════════════════════════════════════════════════════
    #  Public API  —  ProactiveService abstract methods
    # ═══════════════════════════════════════════════════════════════

    def handle_event(self, event: RuntimeEvent) -> Optional[ProactiveDecision]:
        """Evaluate one perception event and decide whether to act.

        Returns None if the event is irrelevant or gated out,
        otherwise a ProactiveDecision describing the action.
        """
        # ── Gate 0: global switch ──
        if not self._enabled:
            return None

        now = self._time()
        user_id = event.user_id or "default"

        # ── Update state BEFORE cooldown check ──
        # (interaction events must reduce rejection streak even during cooldown)
        self._update_state(event, user_id, now)

        # ── Gate 1: cooldown ──
        # Presence transitions are safety/continuity messages.  They must not
        # be lost because a positive-expression reminder happened shortly
        # before the user left or returned.
        presence_transition = event.event_type in (
            "user_away_timeout", "user_return_timeout")
        if self._cooldown_until > now and not presence_transition:
            return None

        # ── Decision routing ──
        decision = self._decide(event, user_id, now)

        if decision is None or not decision.should_act:
            return None

        # ── Gate 2: daily limit ──
        if not self._check_daily_limit(user_id, now):
            return None

        # ── Gate 3: dedup ──
        if self._is_duplicate(user_id, decision.reason, now):
            return None

        # ── Gate 4: rejection backoff ──
        if self._rejection_block(user_id, decision.priority):
            return None

        # ── Commit: record decision, bump daily counter, set cooldown ──
        self._last_proactive[user_id] = (now, decision.reason)
        today = self._today(now)
        self._daily_counts[(user_id, today)] += 1
        self._cooldown_until = now + decision.cooldown_seconds
        self._last_decision_cooldown = decision.cooldown_seconds

        return decision

    def get_cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._time())

    def reset_cooldown(self, seconds: Optional[int] = None) -> None:
        if seconds is not None:
            self._cooldown_until = self._time() + seconds
        else:
            self._cooldown_until = 0.0

    def record_rejection(self, user_id: str) -> None:
        self._rejection_count[user_id] += 1
        self._rejection_streak[user_id] += 1
        # Immediate cooldown escalation using dedicated backoff base
        backoff = _level_scale(self._level, REJECTION_BACKOFF_BASE)
        backoff *= REJECTION_BACKOFF_FACTOR ** min(
            self._rejection_streak[user_id], MAX_REJECTION_STREAK
        )
        self._cooldown_until = self._time() + backoff

    def set_proactivity_level(self, level: int) -> None:
        self._level = max(1, min(10, int(level)))

    def health(self) -> Dict[str, Any]:
        return {
            "backend": "rule_based",
            "enabled": self._enabled,
            "level": self._level,
            "cooldown_remaining_s": self.get_cooldown_remaining(),
            "rejection_count": dict(self._rejection_count),
            "rejection_streak": dict(self._rejection_streak),
            "greeted_users": sorted(self._greeted_users),
            "daily_counts": {str(k): v for k, v in self._daily_counts.items()},
            "pending_reminders": {
                k: len(v) for k, v in self._pending_reminders.items()
            },
        }

    def close(self) -> None:
        self._emotion_history.clear()
        self._greeted_users.clear()
        self._pending_reminders.clear()
        self._daily_counts.clear()
        self._rejection_count.clear()
        self._rejection_streak.clear()
        self._user_present_since.clear()
        self._last_interaction.clear()
        self._last_proactive.clear()

    # ═══════════════════════════════════════════════════════════════
    #  Extended public helpers  (not in the abstract interface)
    # ═══════════════════════════════════════════════════════════════

    def record_emotion_observation(self, user_id: str, emotion: str) -> None:
        """Feed an emotion reading into the history (for adapters).

        Call this when an external adapter has already verified a
        sustain threshold and wants to pre-populate the per-user
        emotion history before emitting EMOTION_CHANGED.
        """
        if not emotion or not emotion.strip():
            return
        e = emotion.strip().lower()
        if user_id not in self._emotion_history:
            self._emotion_history[user_id] = collections.deque(
                maxlen=EMOTION_WINDOW_SIZE
            )
        self._emotion_history[user_id].append((self._time(), e))

    def set_enabled(self, enabled: bool) -> None:
        """Global on/off switch for the proactive engine."""
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def level(self) -> int:
        return self._level

    @property
    def rejection_streak(self) -> Dict[str, int]:
        return dict(self._rejection_streak)

    @property
    def daily_counts(self) -> Dict[str, int]:
        return {str(k): v for k, v in self._daily_counts.items()}

    # ═══════════════════════════════════════════════════════════════
    #  State update
    # ═══════════════════════════════════════════════════════════════

    def _update_state(self, event: RuntimeEvent, user_id: str,
                      now: float) -> None:
        etype = event.event_type

        # ── Presence ──
        if etype == EventType.USER_PRESENT:
            self._user_present_since.setdefault(user_id, now)
            # Initialize interaction clock so silence is measured from entry
            self._last_interaction.setdefault(user_id, now)

        elif etype == EventType.SESSION_STARTED:
            self._user_present_since.setdefault(user_id, now)
            self._last_interaction.setdefault(user_id, now)

        elif etype == EventType.USER_AWAY:
            self._user_present_since.pop(user_id, None)
            self._greeted_users.discard(user_id)

        # ── Emotion ──
        elif etype == EventType.EMOTION_CHANGED:
            emotion = (event.payload or {}).get("emotion", "").strip().lower()
            if emotion:
                if user_id not in self._emotion_history:
                    self._emotion_history[user_id] = collections.deque(
                        maxlen=EMOTION_WINDOW_SIZE
                    )
                self._emotion_history[user_id].append((now, emotion))

        # ── Interaction (speech / wake-word resets rejection streak) ──
        if etype in (EventType.USER_SPEECH, EventType.WAKE_WORD):
            self._last_interaction[user_id] = now
            # Each successful interaction reduces the rejection streak by 1
            if self._rejection_streak.get(user_id, 0) > 0:
                self._rejection_streak[user_id] -= 1

    # ═══════════════════════════════════════════════════════════════
    #  Decision rules  (called in priority order)
    # ═══════════════════════════════════════════════════════════════

    def _decide(self, event: RuntimeEvent, user_id: str,
                now: float) -> Optional[ProactiveDecision]:
        etype = event.event_type

        # Rule 1: Reminder due — highest priority
        if etype == EventType.REMINDER_DUE:
            return self._decide_reminder(event, user_id)

        # Rule 2: Greeting on first detection / session start
        if etype in (EventType.USER_PRESENT, EventType.SESSION_STARTED):
            if (etype == EventType.USER_PRESENT
                    and (event.payload or {}).get("suppress_greeting")):
                return None
            return self._decide_greeting(user_id)

        # Timed presence and positive-expression decisions are emitted by the
        # perception adapter after stable camera observations.
        if etype == "positive_expression":
            return self._decide_positive_expression(event)
        if etype == "user_away_timeout":
            return ProactiveDecision(
                should_act=True,
                reason="presence_away",
                priority=8,
                suggested_scene="presence_away",
                suggested_prompt="我一直都在，随时等你回来。",
                cooldown_seconds=10,
            )
        if etype == "user_return_timeout":
            return ProactiveDecision(
                should_act=True,
                reason="presence_return",
                priority=8,
                suggested_scene="presence_return",
                suggested_prompt="你回来啦，我一直在这儿等你呢。",
                cooldown_seconds=10,
            )

        # Rule 4: Emotion-based care
        if etype == EventType.EMOTION_CHANGED:
            return self._decide_emotion_care(user_id)

        # Rule 5: Silence / sit timeout — light interaction
        if etype in (EventType.SILENCE_TIMEOUT, EventType.SIT_TIMEOUT):
            return self._decide_silence_care(event, user_id, now)

        return None

    def _decide_positive_expression(self, event: RuntimeEvent) -> ProactiveDecision:
        emotion = str((event.payload or {}).get("emotion", "happy")).lower()
        if emotion == "surprise":
            prompt = "看到你刚刚有点惊讶，是发生什么有趣的事了吗？"
        else:
            prompt = "看到你最近心情不错呢，感觉今天状态很好。"
        return ProactiveDecision(
            should_act=True,
            reason="positive_expression:%s" % emotion,
            priority=7,
            suggested_scene="positive_expression",
            suggested_prompt=prompt,
            cooldown_seconds=40,
        )

    def _decide_reminder(self, event: RuntimeEvent,
                         _user_id: str) -> ProactiveDecision:
        payload = event.payload or {}
        text = payload.get("text", payload.get("title", ""))
        return ProactiveDecision(
            should_act=True,
            reason="reminder",
            priority=9,
            suggested_scene="reminder",
            suggested_prompt=text,
            cooldown_seconds=int(_level_scale(self._level, GREETING_COOLDOWN_BASE)),
        )

    def _decide_greeting(self, user_id: str) -> Optional[ProactiveDecision]:
        if user_id in self._greeted_users:
            return None
        self._greeted_users.add(user_id)
        return ProactiveDecision(
            should_act=True,
            reason="greeting",
            priority=8,
            suggested_scene="greeting",
            suggested_prompt="",
            cooldown_seconds=int(_level_scale(self._level, GREETING_COOLDOWN_BASE)),
        )

    def _decide_emotion_care(self, user_id: str) -> Optional[ProactiveDecision]:
        history = self._emotion_history.get(user_id)
        if not history or len(history) < EMOTION_STREAK_THRESHOLD:
            return None

        recent = list(history)[-EMOTION_STREAK_THRESHOLD:]
        negative = [(t, e) for t, e in recent if e in CARE_EMOTIONS]

        if len(negative) < EMOTION_STREAK_THRESHOLD:
            return None

        # Dominant negative emotion
        counts: Dict[str, int] = {}
        for _, emo in negative:
            counts[emo] = counts.get(emo, 0) + 1
        dominant = max(counts, key=lambda k: counts[k])

        scene_map: Dict[str, str] = {
            "sad": "comfort_sad",   "悲伤": "comfort_sad",
            "fear": "comfort_fear", "害怕": "comfort_fear", "恐惧": "comfort_fear",
            "angry": "comfort_angry", "生气": "comfort_angry",
            "sorrow": "comfort_sad",
            "disgust": "comfort_angry", "厌恶": "comfort_angry",
        }

        return ProactiveDecision(
            should_act=True,
            reason="emotion_care:%s" % dominant,
            priority=7,
            suggested_scene=scene_map.get(dominant, "comfort"),
            suggested_prompt="",
            cooldown_seconds=int(_level_scale(self._level,
                                              EMOTION_CARE_COOLDOWN_BASE)),
        )

    def _decide_silence_care(self, event: RuntimeEvent, user_id: str,
                             now: float) -> Optional[ProactiveDecision]:
        # Must have a user present
        if user_id not in self._user_present_since:
            return None

        last_interaction = self._last_interaction.get(user_id, 0.0)
        silence_duration = now - last_interaction

        etype = event.event_type
        if etype == EventType.SILENCE_TIMEOUT:
            threshold = _level_scale(self._level, SILENCE_TIMEOUT_BASE)
        else:
            threshold = _level_scale(self._level, SIT_TIMEOUT_BASE)

        if silence_duration < threshold:
            return None

        return ProactiveDecision(
            should_act=True,
            reason="silence_care",
            priority=4,
            suggested_scene="light_chat",
            suggested_prompt="",
            cooldown_seconds=int(_level_scale(self._level, COOLDOWN_MAX)
                                 * SILENCE_CARE_COOLDOWN_MULT),
        )

    # ═══════════════════════════════════════════════════════════════
    #  Gate checks
    # ═══════════════════════════════════════════════════════════════

    def _check_daily_limit(self, user_id: str, now: float) -> bool:
        today = self._today(now)
        count = self._daily_counts.get((user_id, today), 0)
        return count < self._daily_max()

    def _is_duplicate(self, user_id: str, reason: str, now: float) -> bool:
        last = self._last_proactive.get(user_id)
        if last is None:
            return False
        last_time, last_reason = last
        if now - last_time > DEDUP_WINDOW:
            return False
        # Exact same reason → duplicate
        if last_reason == reason:
            # Positive-expression checks are intentionally periodic in the
            # competition demo; the service cooldown (40 s) is their gate.
            if reason.split(":", 1)[0] == "positive_expression":
                return False
            return True
        # Same reason family → duplicate (e.g. emotion_care:sad vs emotion_care:fear)
        last_family = last_reason.split(":")[0]
        this_family = reason.split(":")[0]
        if this_family == "positive_expression":
            return False
        return last_family == this_family

    def _rejection_block(self, user_id: str, priority: int) -> bool:
        """Block non-essential actions when user has been rejecting."""
        streak = self._rejection_streak.get(user_id, 0)
        if streak < 3:
            return False
        # Priority ≥ REJECTION_BLOCK_PRIORITY always passes
        return priority < REJECTION_BLOCK_PRIORITY

    # ═══════════════════════════════════════════════════════════════
    #  Level-based thresholds
    # ═══════════════════════════════════════════════════════════════

    def _daily_max(self) -> int:
        if self._level <= 1:
            return DAILY_LEVEL_1
        if self._level <= 5:
            return DAILY_LEVEL_5 + (self._level - 5)  # progressively scale
        if self._level >= 10:
            return DAILY_LEVEL_10
        ratio = (self._level - 5) / 5.0
        return int(DAILY_LEVEL_5 + ratio * (DAILY_LEVEL_10 - DAILY_LEVEL_5))

    @staticmethod
    def _today(now: float) -> str:
        return datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
