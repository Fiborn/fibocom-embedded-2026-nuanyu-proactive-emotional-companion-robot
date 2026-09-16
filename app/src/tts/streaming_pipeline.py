#!/usr/bin/env python3
"""Incremental text segmentation and ordered TTS delivery."""
import collections
import re
import threading
import time
import uuid


class IncrementalSentenceSegmenter:
    """Turn arbitrary token chunks into natural, bounded Chinese speech units."""

    STRONG_ENDINGS = "。！？!?；;\n"
    WEAK_ENDINGS = "，、：,："

    def __init__(self, min_chars=6, soft_chars=16, max_chars=42,
                 first_soft_chars=None):
        self.min_chars = max(2, int(min_chars))
        self.soft_chars = max(self.min_chars, int(soft_chars))
        self.max_chars = max(self.soft_chars, int(max_chars))
        # The first sentence uses a smaller soft-split threshold: it is cut out
        # sooner (fast first-sentence onset); later sentences use the normal
        # soft_chars (long sentences, playback ≥ synthesis time, seamless).
        # Assumption: synthesis RTF ~1x — a sentence that is too short plays for
        # less time than it takes to synthesize, which guarantees a gap.
        self.first_soft_chars = max(
            self.min_chars, int(first_soft_chars) if first_soft_chars else 0)
        if self.first_soft_chars:
            self.first_soft_chars = min(self.first_soft_chars, self.soft_chars)
        self._buffer = ""
        self._first_sentence_done = False

    @property
    def pending(self):
        return self._buffer

    def feed(self, chunk):
        if chunk:
            self._buffer += str(chunk)
        # Buffer-level bracket cleanup: the AI often prefixes a sentence with an
        # action description like "（语气温和）". Chunk-level cleanup cannot handle
        # brackets spanning chunks (not yet closed); they can only be recognized
        # once the buffer is whole. Closed pairs are deleted outright (their
        # content should never be read aloud); unclosed ones wait for later
        # chunks. This keeps action descriptions from being split into a sentence
        # or padding the first sentence out.
        self._buffer = re.sub(r'[（(][^（()）]{1,15}[）)]', '', self._buffer)
        return self._extract(force=False)

    def flush(self):
        parts = self._extract(force=True)
        return parts

    def _extract(self, force):
        ready = []
        while self._buffer:
            boundary = self._find_boundary(force)
            if boundary is None:
                break
            text = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:].lstrip()
            if text:
                ready.append(text)
                self._first_sentence_done = True
            if force and not self._buffer:
                break
        return ready

    def _find_boundary(self, force):
        visible = len(re.sub(r"\s+", "", self._buffer))
        # Split on punctuation: a period/comma once ≥ min_chars cuts immediately,
        # with no character-count soft split. Sentences then break at natural
        # semantic boundaries (comma/period), which matches spoken rhythm better.
        # A fragment that is too short (< min_chars) merges into the next
        # punctuation mark before cutting.
        for index, char in enumerate(self._buffer):
            length = len(re.sub(r"\s+", "", self._buffer[:index + 1]))
            if char in self.STRONG_ENDINGS and length >= self.min_chars:
                return index + 1
            if char in self.WEAK_ENDINGS and length >= self.min_chars:
                return index + 1
            if (not self._first_sentence_done
                    and char in self.STRONG_ENDINGS + self.WEAK_ENDINGS
                    and length >= 2):
                # Very short first-sentence greeting split: e.g. "<nickname>，" — two
                # characters plus punctuation → cut immediately. The shortest
                # possible first sentence → fastest onset. Later sentences are
                # unaffected (normal thresholds resume after the first sentence).
                return index + 1

        if not self._first_sentence_done and visible >= self.first_soft_chars:
            # First-sentence soft split: a long opening with no punctuation yet is
            # cut early at first_soft_chars. A shorter first sentence enters
            # synthesis sooner → shorter onset (end-to-end latency). Later
            # sentences still split by punctuation/soft_chars, kept seamless by
            # parallel synthesis.
            limit = min(len(self._buffer), self.first_soft_chars)
            weak = max(self._buffer.rfind(mark, 0, limit + 1)
                       for mark in self.WEAK_ENDINGS)
            if weak >= self.min_chars - 1:
                return weak + 1
            return limit

        if visible >= self.max_chars:
            limit = min(len(self._buffer), self.max_chars)
            weak = max(self._buffer.rfind(mark, 0, limit + 1)
                       for mark in self.WEAK_ENDINGS)
            if weak >= self.min_chars - 1:
                return weak + 1
            space = self._buffer.rfind(" ", self.min_chars, limit + 1)
            return space + 1 if space >= self.min_chars else limit

        if force and self._buffer.strip():
            return len(self._buffer)
        return None


class StreamingTTSCoordinator:
    """Bounded, cancellable, ordered adapter around an async speak function.

    Pipeline commit: segments are handed to the provider as fast as they are
    segmented (bounded by _max_pending), WITHOUT waiting for the previous one
    to finish playing. Order is preserved by the provider's seq buffer and
    playback thread; the coordinator only tracks "submitted-but-not-finished"
    via the play-complete callback so the mic stays muted until EVERY segment
    of the reply has actually finished playing.
    """

    def __init__(self, speak_async, on_activity=None, max_pending=3,
                 item_timeout=90.0, max_merged_chars=64, on_session_begin=None,
                 session_grace=10.0):
        self._speak_async = speak_async
        self._on_activity = on_activity or (lambda active: None)
        # When a new reply begins (coordinator.begin(replace=True)), notify the
        # provider: stop the previous reply's unplayed segments + reset the
        # first-sentence exclusive gate. Called outside the lock to avoid deadlock.
        self._on_session_begin = on_session_begin or (lambda: None)
        self._max_pending = max(1, int(max_pending))
        self._max_merged_chars = max(8, int(max_merged_chars))
        self._item_timeout = float(item_timeout)
        # Session-level completion watchdog: if a provider callback never fires
        # after finish() (synthesis/playback hung), pending never reaches zero →
        # the coordinator stays active forever → the mic stays muted permanently
        # ("can't hear the second sentence"). After the grace period pending is
        # released by force, guaranteeing the mic unmutes.
        self._session_grace = float(session_grace)
        self._pending = collections.deque()
        self._sessions = {}
        self._current_session = None
        self._active = False
        self._closed = False
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="StreamingTTS")
        self._worker.start()
        # Collect recent TTS latencies for monitoring
        self._tts_latency_log = collections.deque(maxlen=100)

    def get_tts_latencies(self):
        """Return recent TTS latency records."""
        return list(self._tts_latency_log)

    def begin(self, replace=True):
        session_id = uuid.uuid4().hex
        with self._condition:
            if replace:
                for state in self._sessions.values():
                    state["cancelled"] = True
                    grace_timer = state.get("grace_timer")
                    state["grace_timer"] = None
                    if grace_timer is not None:
                        grace_timer.cancel()
                for old_session_id, _text, _callback in self._pending:
                    old_state = self._sessions.get(old_session_id)
                    if old_state:
                        old_state["pending"] = max(0, old_state["pending"] - 1)
                self._pending.clear()
                for old_session_id in list(self._sessions):
                    self._maybe_finish_locked(old_session_id)
            self._sessions[session_id] = {
                "finished": False, "cancelled": False, "pending": 0,
                # The session grace guard is an inactivity timeout, not an
                # absolute deadline: long but healthy replies must keep the
                # mic muted until their final playback callback arrives.
                "last_progress": time.monotonic(),
                "grace_timer": None,
            }
            self._current_session = session_id
            self._condition.notify_all()
        if replace:
            # New reply: notify the provider outside the lock to stop the old
            # segments and reset the first-sentence exclusive gate. It MUST be
            # outside the lock: provider.stop() calls back into the coordinator
            # (which takes this lock), so calling it under the lock would deadlock.
            try:
                self._on_session_begin()
            except Exception:
                pass
        return session_id

    def submit(self, session_id, text, on_done=None):
        text = str(text or "").strip()
        if not text:
            return False
        with self._condition:
            state = self._sessions.get(session_id)
            if not state or state["cancelled"] or state["finished"]:
                return False
            task = [session_id, text, on_done]
            if len(self._pending) >= self._max_pending:
                last = self._pending[-1]
                if last[0] == session_id:
                    merged = (last[1].rstrip() + " " + text).strip()
                    if len(merged) <= self._max_merged_chars:
                        last[1] = merged
                        return True
                return False
            self._pending.append(task)
            state["pending"] += 1
            self._set_active_locked(True)
            self._condition.notify_all()
            return True

    def finish(self, session_id):
        arm_grace = False
        with self._condition:
            state = self._sessions.get(session_id)
            if state:
                state["finished"] = True
                self._maybe_finish_locked(session_id)
                arm_grace = (state["pending"] > 0
                             and session_id in self._sessions)
            self._condition.notify_all()
        # Session-level completion watchdog: pending is released by force only
        # when no segment completion callback arrives at all during the grace
        # period. Every normal callback renews it, so a long reply that is still
        # playing is not unmuted early by a fixed finish()+10s deadline.
        if arm_grace:
            self._arm_session_grace(session_id)

    def _arm_session_grace(self, session_id, delay=None):
        """Arm the existing session guard from the latest playback progress."""
        with self._condition:
            state = self._sessions.get(session_id)
            if (not state or not state["finished"]
                    or state["pending"] <= 0):
                return
            old_timer = state.get("grace_timer")
            if old_timer is not None:
                old_timer.cancel()
            timeout = self._session_grace if delay is None else max(0.05, delay)
            timer = threading.Timer(timeout, self._session_grace_expired,
                                     args=(session_id,))
            timer.daemon = True
            state["grace_timer"] = timer
            timer.start()

    def _session_grace_expired(self, session_id):
        reschedule = None
        with self._condition:
            state = self._sessions.get(session_id)
            if (not state or not state["finished"]
                    or state["pending"] <= 0):
                return
            state["grace_timer"] = None
            idle_for = time.monotonic() - state.get(
                "last_progress", time.monotonic())
            remaining = self._session_grace - idle_for
            if remaining > 0:
                # Timer scheduling can wake slightly early; preserve the
                # full no-progress interval before forcing the session idle.
                reschedule = remaining
            else:
                state["pending"] = 0
                self._maybe_finish_locked(session_id)
                self._condition.notify_all()
                print("[Coord] session force-finish (grace) pending->0",
                      flush=True)
        if reschedule is not None:
            self._arm_session_grace(session_id, delay=reschedule)

    def cancel(self, session_id):
        with self._condition:
            state = self._sessions.get(session_id)
            if not state:
                return
            grace_timer = state.get("grace_timer")
            state["grace_timer"] = None
            if grace_timer is not None:
                grace_timer.cancel()
            state["cancelled"] = True
            kept = collections.deque()
            removed = 0
            for task in self._pending:
                if task[0] == session_id:
                    removed += 1
                else:
                    kept.append(task)
            self._pending = kept
            state["pending"] = max(0, state["pending"] - removed)
            self._maybe_finish_locked(session_id)
            self._condition.notify_all()

    def speak_once(self, text, on_done=None, replace=False):
        session_id = self.begin(replace=replace)
        queued = self.submit(session_id, text, on_done=on_done)
        self.finish(session_id)
        return queued

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def is_active(self):
        """True while a TTS reply session is being synthesized/played.

        Used by the mic gate to decide whether a reply produced no audio at
        all (so the mic can be safely unmuted even though no TTS callback
        will fire).
        """
        if not self._condition.acquire(timeout=0.05):
            return self._active
        try:
            return self._active
        finally:
            self._condition.release()

    def wait_session_done(self, session_id, timeout=30.0):
        """Block until all submitted TTS items for this session complete."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while time.monotonic() < deadline:
                state = self._sessions.get(session_id)
                if not state or state["pending"] == 0:
                    return True
                self._condition.wait(timeout=1.0)
        return False

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                session_id, text, on_done = self._pending.popleft()
                state = self._sessions.get(session_id)
                if not state or state["cancelled"]:
                    self._complete_locked(session_id)
                    continue

            done = threading.Event()
            result = {"ok": False, "error": "TTS callback timeout", "latency": None}
            watchdog = [None]

            # Every segment must own its callback state. _run moves on to the
            # next segment without waiting for the provider, so defining the
            # callback directly here would let Python's closure share the
            # session_id/done/result/on_done rebound by the following loop
            # iteration, making the first segment's callback treat every later
            # segment as a duplicate callback. That is the root cause of
            # "only one CALLBACK" being received.
            def _make_callback(callback_session_id, callback_done,
                               callback_result, callback_on_done,
                               callback_watchdog):
                # Playback-complete callback: invoked by the provider when
                # playback finishes. The done flag guarantees that "real callback
                # vs timeout watchdog" is handled exactly once.
                def callback(ok, error="", latency=None):
                    print("[Coord] cb-reached session=%s ok=%s" %
                          (callback_session_id[:8], ok), flush=True)
                    with self._condition:
                        print("[Coord] cb-locked done=%s session=%s" %
                              (callback_done.is_set(), callback_session_id[:8]),
                              flush=True)
                        if callback_done.is_set():
                            return
                        callback_done.set()
                        callback_result["ok"] = bool(ok)
                        callback_result["error"] = str(error or "")
                        if isinstance(latency, dict):
                            callback_result["latency"] = latency
                        item_timer = callback_watchdog[0]
                        session_state = self._sessions.get(callback_session_id)
                        session_timer = None
                        if session_state is not None:
                            session_state["last_progress"] = time.monotonic()
                            session_timer = session_state.get("grace_timer")
                            session_state["grace_timer"] = None
                    if item_timer is not None:
                        item_timer.cancel()
                    if session_timer is not None:
                        session_timer.cancel()
                    print("[Coord] CALLBACK ok=%s err=%s" %
                          (ok, str(error)[:40]), flush=True)
                    callback_latency = callback_result.get("latency")
                    if (isinstance(callback_latency, dict)
                            and "first_audio_ms" in callback_latency):
                        self._tts_latency_log.append(callback_latency)
                    if callback_on_done:
                        try:
                            callback_on_done(bool(ok), str(error or ""),
                                             callback_latency)
                        except Exception:
                            pass
                    with self._condition:
                        self._complete_locked(callback_session_id)
                        should_arm_grace = (
                            callback_session_id in self._sessions
                            and self._sessions[callback_session_id]["finished"]
                            and self._sessions[callback_session_id]["pending"] > 0)
                        self._condition.notify_all()
                    if should_arm_grace:
                        self._arm_session_grace(callback_session_id)
                return callback

            callback = _make_callback(session_id, done, result, on_done, watchdog)

            try:
                queued = bool(self._speak_async(text, callback=callback))
                print("[Coord] speak queued=%s session=%s text=%r"
                      % (queued, session_id[:8], text[:20]), flush=True)
                if not queued:
                    callback(False, "TTS queue rejected")
            except Exception as exc:
                print("[Coord] speak EXC: %s" % str(exc)[:80], flush=True)
                callback(False, str(exc))

            # Timeout watchdog: when the provider never calls back, release
            # pending by force so the mic is never muted forever. A normal
            # callback has already done.set(), so the watchdog just returns when
            # it fires, without reprocessing.
            watchdog[0] = threading.Timer(self._item_timeout, callback,
                                          args=(False, "TTS callback timeout"))
            watchdog[0].daemon = True
            watchdog[0].start()
            # The provider may complete the callback synchronously inside
            # speak_async(); in that case the timer is created after the
            # callback, so cancel it once here to avoid leaving a pointless
            # timer thread behind.
            if done.is_set():
                watchdog[0].cancel()
            print("[Coord] watchdog armed", flush=True)
            # Do NOT wait for playback to finish — submit the next segment right
            # away so the provider's synthesis pool works in parallel: while
            # segment N plays, N+1/N+2 are already synthesized, so the gap is
            # zero. Releasing pending is left to the playback-complete callback
            # (order unchanged, mic unmute timing unchanged).

    def _complete_locked(self, session_id):
        state = self._sessions.get(session_id)
        if state:
            state["pending"] = max(0, state["pending"] - 1)
            self._maybe_finish_locked(session_id)

    def _maybe_finish_locked(self, session_id):
        state = self._sessions.get(session_id)
        if not state:
            return
        if state["pending"] == 0 and (state["finished"] or state["cancelled"]):
            del self._sessions[session_id]
        # Only report "inactive" once EVERY session is finished/cancelled AND
        # has no segments still queued for playback. Without the `finished`
        # check, the mic unmutes in the gap between two WAV files of the same
        # reply while the LLM is still generating the next segment — letting
        # the speaker's voice be captured and fed back into the AI.
        all_idle = all(
            item.get("pending", 0) == 0
            and (item.get("finished") or item.get("cancelled"))
            for item in self._sessions.values()
        )
        if all_idle:
            self._set_active_locked(False)

    def _set_active_locked(self, active):
        if self._active == active:
            return
        self._active = active
        try:
            self._on_activity(active)
        except Exception:
            pass
