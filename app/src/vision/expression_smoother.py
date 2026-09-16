#!/usr/bin/env python3
"""Time-based expression smoothing with EMA and hysteresis.

The raw FER top-1 class is allowed to fluctuate.  A displayed expression only
changes after the new candidate remains convincingly stronger for a minimum
amount of time, and the current expression is held briefly after each switch.
"""
import time
import numpy as np

LABELS = ["angry", "disgust", "fear", "happy", "sad", "surprise", "neutral"]
EMOTION_ZH = {
    "angry": "生气", "disgust": "厌恶", "fear": "害怕",
    "happy": "开心", "sad": "难过", "surprise": "惊讶",
    "neutral": "平静", "unknown": "未识别",
}


class ExpressionSmoother:
    def __init__(self, window_size=8, alpha=0.45,
                 enter_threshold=0.42, exit_threshold=0.28,
                 switch_margin=0.06, confirm_seconds=0.18,
                 min_hold_seconds=0.45, no_face_timeout=0.75):
        self.window_size = window_size
        self.alpha = alpha
        self.enter_threshold = enter_threshold
        self.exit_threshold = exit_threshold
        self.switch_margin = switch_margin
        self.confirm_seconds = confirm_seconds
        self.min_hold_seconds = min_hold_seconds
        self.no_face_timeout = no_face_timeout

        self._history = []
        self._ema = None
        self._current_emotion = "unknown"
        self._current_zh = EMOTION_ZH["unknown"]
        self._current_conf = 0.0
        self._current_probs = {}
        self._candidate = None
        self._candidate_since = None
        self._last_switch_time = None
        self._last_face_time = None
        self._changed = False

    def update(self, probabilities: dict, has_face: bool, now=None) -> dict:
        """Update and return the stable expression state.

        ``now`` is injectable for deterministic tests; production uses the
        monotonic clock.  Switching combines four protections:

        1. EMA suppresses single-frame probability spikes.
        2. A new class must beat the current class by ``switch_margin``.
        3. It must remain the candidate for ``confirm_seconds``.
        4. The displayed class is held for at least ``min_hold_seconds``.
        """
        now = time.monotonic() if now is None else float(now)
        self._changed = False

        if not has_face:
            if self._last_face_time is not None and now - self._last_face_time >= self.no_face_timeout:
                self._switch_to("unknown", 0.0, now)
                self._history.clear()
                self._ema = None
                self._current_probs = {}
                self._reset_candidate()
            return self._make_result(now)

        self._last_face_time = now
        if not probabilities or len(probabilities) != len(LABELS):
            return self._make_result(now)

        raw = np.array([probabilities.get(label, 0.0) for label in LABELS],
                       dtype=np.float64)
        if not np.isfinite(raw).all() or raw.sum() <= 0:
            return self._make_result(now)
        raw /= raw.sum()

        if self._ema is None:
            self._ema = raw.copy()
        else:
            self._ema = self.alpha * raw + (1.0 - self.alpha) * self._ema

        self._history.append(raw)
        if len(self._history) > self.window_size:
            self._history.pop(0)

        self._current_probs = {
            label: float(self._ema[i]) for i, label in enumerate(LABELS)
        }
        top_idx = int(np.argmax(self._ema))
        top_label = LABELS[top_idx]
        top_conf = float(self._ema[top_idx])

        if self._current_emotion == "unknown":
            desired = top_label if top_conf >= self.enter_threshold else "unknown"
        else:
            current_idx = LABELS.index(self._current_emotion)
            current_conf = float(self._ema[current_idx])
            self._current_conf = current_conf
            if top_label == self._current_emotion:
                desired = self._current_emotion
            elif (top_conf >= self.enter_threshold and
                  top_conf >= current_conf + self.switch_margin):
                desired = top_label
            elif current_conf < self.exit_threshold and top_conf < self.enter_threshold:
                desired = "unknown"
            else:
                desired = self._current_emotion

        if desired == self._current_emotion:
            self._reset_candidate()
            if desired != "unknown":
                self._current_conf = float(self._ema[LABELS.index(desired)])
            return self._make_result(now)

        if desired != self._candidate:
            self._candidate = desired
            self._candidate_since = now
            return self._make_result(now)

        candidate_ready = now - self._candidate_since >= self.confirm_seconds
        hold_complete = (self._last_switch_time is None or
                         now - self._last_switch_time >= self.min_hold_seconds)
        if candidate_ready and hold_complete:
            confidence = 0.0 if desired == "unknown" else top_conf
            self._switch_to(desired, confidence, now)
            self._reset_candidate()

        return self._make_result(now)

    def _switch_to(self, emotion, confidence, now):
        if emotion != self._current_emotion:
            self._current_emotion = emotion
            self._current_zh = EMOTION_ZH.get(emotion, EMOTION_ZH["unknown"])
            self._changed = True
            self._last_switch_time = now
        self._current_conf = confidence

    def _reset_candidate(self):
        self._candidate = None
        self._candidate_since = None

    def _make_result(self, now) -> dict:
        candidate_age = 0.0
        if self._candidate_since is not None:
            candidate_age = max(0.0, now - self._candidate_since)
        return {
            "emotion": self._current_emotion,
            "emotion_zh": self._current_zh,
            "confidence": self._current_conf,
            "probabilities": dict(self._current_probs),
            "candidate": self._candidate,
            "candidate_age_ms": round(candidate_age * 1000, 1),
            "changed": self._changed,
        }
