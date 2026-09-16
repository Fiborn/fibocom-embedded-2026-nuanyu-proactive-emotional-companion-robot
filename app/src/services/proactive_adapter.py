#!/usr/bin/env python3
"""ProactiveInteractionAdapter — bridges perception events to TTS/LLM execution."""
from __future__ import annotations
import collections, os, threading, time
from src.domain.events import RuntimeEvent


# VisionWorker continuously analyzes frames.  These timers control when the
# proactive layer turns a stable perception into a spoken demo interaction.
POSITIVE_EXPRESSION_INTERVAL_SECONDS = float(
    os.environ.get("PROACTIVE_EXPRESSION_INTERVAL_SECONDS", "40"))
PRESENCE_TRANSITION_SECONDS = float(
    os.environ.get("PROACTIVE_PRESENCE_TIMEOUT_SECONDS", "10"))

class NoOpProactiveAdapter:
    def __init__(self):
        self._decisions = collections.deque(maxlen=8)
    def tick(self, *a, **kw): pass
    def poll_decision(self): return None
    def record_executed(self, d): pass
    def record_rejection(self, uid=""): pass
    def health(self): return {"backend": "noop"}
    def close(self): pass

class ProactiveInteractionAdapter:
    def __init__(self, proactive_service=None):
        self._service = proactive_service
        self._decisions = collections.deque(maxlen=8)
        self._lock = threading.Lock()
        self._tick_count = 0
        self._face_present = False
        self._away_since = None
        self._away_announced = False
        self._return_since = None
        self._return_announced = False
        self._positive_check_at = None

    def tick(self, vision_state, schedule_due, is_sleeping, is_speaking, is_thinking):
        if self._service is None or is_sleeping: return
        now = time.time(); self._tick_count += 1
        # Use the VisionWorker's sticky "presence" (face OR motion with a
        # sustained-absence exit window) so a brief head turn no longer trips
        # the away/return state machine.
        face_present = bool(vision_state.get(
            "presence", vision_state.get("face_detected")))
        if face_present:
            if not self._face_present:
                # Delay the return greeting by ten seconds.  The old
                # immediate greeting made the demo too eager and prevented
                # the away/return behavior from being observable.
                self._return_since = now
                self._return_announced = False
                self._positive_check_at = now + POSITIVE_EXPRESSION_INTERVAL_SECONDS
                self._handle(RuntimeEvent(
                    event_id=f"p{self._tick_count}",
                    event_type="user_present", timestamp=now, source="vision",
                    payload={"suppress_greeting": True}),
                    is_speaking, is_thinking)
            elif self._positive_check_at is None:
                self._positive_check_at = now + POSITIVE_EXPRESSION_INTERVAL_SECONDS
        elif self._face_present:
            # Close the presence session and start the ten-second absence
            # timer.  A later return starts a separate ten-second timer.
            self._handle(RuntimeEvent(
                event_id=f"a{self._tick_count}", event_type="user_away",
                timestamp=now, source="vision"), is_speaking, is_thinking)
            self._away_since = now
            self._away_announced = False
            self._return_since = None
            self._positive_check_at = None
        self._face_present = face_present

        if (not face_present and self._away_since is not None
                and not self._away_announced
                and now - self._away_since >= PRESENCE_TRANSITION_SECONDS
                and not is_speaking and not is_thinking):
            decision = self._handle(RuntimeEvent(
                event_id=f"away{self._tick_count}",
                event_type="user_away_timeout", timestamp=now,
                source="vision"), is_speaking, is_thinking)
            if decision is not None:
                self._away_announced = True

        if (face_present and self._return_since is not None
                and not self._return_announced
                and now - self._return_since >= PRESENCE_TRANSITION_SECONDS
                and not is_speaking and not is_thinking):
            decision = self._handle(RuntimeEvent(
                event_id=f"return{self._tick_count}",
                event_type="user_return_timeout", timestamp=now,
                source="vision"), is_speaking, is_thinking)
            if decision is not None:
                self._return_announced = True

        # The FER worker still analyzes continuously.  Every forty seconds
        # the proactive layer samples the stable expression and only speaks
        # for the easy-to-demonstrate happy/surprise cases.
        if (face_present and self._positive_check_at is not None
                and now >= self._positive_check_at):
            emotion = str(vision_state.get("emotion", "unknown")).lower()
            if emotion in ("happy", "surprise") and not is_speaking and not is_thinking:
                self._handle(RuntimeEvent(
                    event_id=f"positive{self._tick_count}",
                    event_type="positive_expression", timestamp=now,
                    source="vision",
                    payload={
                        "emotion": emotion,
                        "emotion_zh": vision_state.get("emotion_zh", ""),
                        "confidence": vision_state.get("confidence", 0.0),
                    }), is_speaking, is_thinking)
            self._positive_check_at = now + POSITIVE_EXPRESSION_INTERVAL_SECONDS

        em = vision_state.get("emotion","unknown")
        if em in ("sad","angry","fear","disgust"):
            self._handle(RuntimeEvent(event_id=f"e{self._tick_count}",event_type="emotion_changed",timestamp=now,source="vision",payload={"emotion":em}),is_speaking,is_thinking)
        for r in (schedule_due or []):
            # The payload key must be "text": ProactiveServiceImpl._decide_reminder
            # reads payload["text"] (falling back to "title"). Sending "content"
            # here silently dropped the reminder body, so every reminder spoke
            # the generic fallback line instead of what the user actually asked
            # to be reminded of.
            self._handle(RuntimeEvent(event_id=f"r{self._tick_count}",event_type="reminder_due",timestamp=now,source="scheduler",payload={"text":r.get("content","")}),is_speaking,is_thinking)

    def _handle(self, evt, speaking, thinking):
        d = self._service.handle_event(evt)
        if d and d.should_act:
            reason_family = str(d.reason).split(":", 1)[0]
            if reason_family in ("greeting", "emotion_care") and (speaking or thinking):
                return None
            for e in self._decisions:
                if e.reason == d.reason: return None
            self._decisions.append(d)
            return d
        return None

    def poll_decision(self):
        return self._decisions[0] if self._decisions else None
    def record_executed(self, d):
        try: self._decisions.remove(d)
        except ValueError: pass
    def record_rejection(self, uid=""):
        if self._service: self._service.record_rejection(uid)
    def health(self):
        svc_h = self._service.health() if self._service else {}
        return {"backend": "real" if self._service else "noop",
                "enabled": bool(svc_h.get("enabled", True)),
                "level": svc_h.get("level", 5),
                "queued": len(self._decisions),
                "ticks": self._tick_count,
                "cooldown_remaining_s": svc_h.get("cooldown_remaining_s", 0)}
    def close(self): self._decisions.clear()
