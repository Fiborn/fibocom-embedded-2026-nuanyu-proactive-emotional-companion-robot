#!/usr/bin/env python3
"""ASR Worker — background thread for mic recording + Whisper transcription."""
import sys, os, time, wave, threading, queue, subprocess, tempfile
import collections
import urllib.request
import numpy as np

# Import backend
_cur = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(_cur) not in sys.path:
    sys.path.insert(0, os.path.dirname(_cur))
from asr.whisper_tiny_cpu_backend import WhisperTinyCpuBackend

MIC_DEVICE = os.environ.get("ASR_MIC_DEVICE", "plughw:CARD=Device,DEV=0")
MIC_CARD = os.environ.get("ASR_MIC_CARD", "Device")
SAMPLE_RATE = 16000
CHANNELS = 1

# VAD params
SILENCE_THRESHOLD = int(os.environ.get("ASR_SILENCE_THRESHOLD", "180"))
SILENCE_TAIL_MS = int(os.environ.get("ASR_SILENCE_TAIL_MS", "300"))
ONSET_CONFIRM_MS = int(os.environ.get("ASR_ONSET_CONFIRM_MS", "90"))
MIN_UTTERANCE_MS = 400       # minimum speech duration

# Empty-transcription throttle: after EMPTY_TRIGGER consecutive
# "speech detected but no text recognized" results (usually TTS echo or
# ambient noise tripping the VAD), force an EMPTY_COOLDOWN_S cooldown and
# stop calling SDK inference, so repeated false triggers cannot burn CPU and
# memory (one of the root causes of GIL starvation and glibc arena growth).
EMPTY_TRIGGER = int(os.environ.get("ASR_EMPTY_TRIGGER", "3"))
EMPTY_COOLDOWN_S = float(os.environ.get("ASR_EMPTY_COOLDOWN_S", "3.0"))
SDK_MIN_AUDIO_MS = int(os.environ.get("ASR_SDK_MIN_AUDIO_MS", "2000"))
MAX_UTTERANCE_SEC = int(os.environ.get("ASR_MAX_UTTERANCE_SEC", "8"))
MAX_LISTEN_SEC = int(os.environ.get("ASR_MAX_LISTEN_SEC", "30"))
FRAME_MS = 30                # analysis frame size
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
# Keep the mic muted for this long after the FINAL TTS playback ends, so the
# speaker's echo tail can never be captured as a new utterance and fed back
# into the AI. A 1s grace also absorbs any lag between the last WAV and the
# mic actually resuming. The streaming coordinator only signals "playback
# done" after the whole reply (all split WAV files) has finished.
MIC_SETTLE_MS = int(os.environ.get("ASR_MIC_SETTLE_MS", "1000"))

# ── Microphone source ─────────────────────────────────────
# board  : board USB microphone (arecord, plughw:CARD=Device)
# remote : PC microphone (HTTP stream pushed to the board's port 5002 by pc_mic_server.py)
# auto   : default. Board first; if arecord fails to start (device missing or
#          broken) fall back to remote automatically.
# When the board's external microphone is broken, set ASR_MIC_SOURCE=remote to
# force the PC mic, or rely on auto.
MIC_SOURCE = os.environ.get("ASR_MIC_SOURCE", "auto").strip().lower()
REMOTE_MIC_URL = os.environ.get(
    "ASR_REMOTE_MIC_URL", "http://127.0.0.1:5002/mic")
REMOTE_MIC_TIMEOUT = float(os.environ.get("ASR_REMOTE_MIC_TIMEOUT", "8"))


class _RemoteMicStream:
    """Streaming audio source reading raw PCM (16k/16bit/mono) from the PC's
    HTTP server.

    Same interface as arecord stdout: read(n) reads bytes, close() closes.
    The socket timeout applies to each recv; on a continuous stream a 960-byte
    frame takes ~30ms, so it never trips by mistake.
    """

    def __init__(self, url, timeout=8.0):
        self._url = url
        self._timeout = timeout
        self._resp = None

    def open(self):
        self._resp = urllib.request.urlopen(self._url, timeout=self._timeout)
        return self

    def read(self, n):
        if self._resp is None:
            raise IOError("remote mic not open")
        return self._resp.read(n)

    def close(self):
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
            self._resp = None


class _BoardMicStream:
    """Raw arecord PCM stream from the board USB mic (same interface as the remote source)."""

    def __init__(self, device):
        self._proc = subprocess.Popen(
            ["arecord", "-D", device,
             "-f", "S16_LE", "-r", str(SAMPLE_RATE),
             "-c", str(CHANNELS),
             "--buffer-time=40000",
             "-t", "raw"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if self._proc.poll() is not None:
            raise IOError("arecord failed to start (device=%s)" % device)

    def read(self, n):
        return self._proc.stdout.read(n)

    def close(self):
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:
            pass

class ASRWorker:
    def __init__(self, model_path=None):
        self.backend = WhisperTinyCpuBackend(model_path)
        self._running = False
        self._thread = None
        self._result_queue = queue.Queue(maxsize=10)
        self._state = "idle"
        self._lock = threading.Lock()
        self._last_text = ""
        self._system_speaking = False
        # Manual competition mute is independent from the half-duplex TTS gate.
        # The worker/model stay alive; only microphone capture is suppressed.
        self._manual_muted = False
        self._resume_at = 0.0   # monotonic time when the mic may listen again
        self._backend_lock = threading.Lock()
        self._last_gain_check = 0.0
        self._last_capture = {}
        self._mic_source_active = MIC_SOURCE
        self._stats = {"total": 0, "success": 0, "crash": 0, "restarts": 0}
        # Empty-transcription throttle state
        self._empty_count = 0
        self._empty_cooldown_until = 0.0

    @property
    def state(self):
        with self._lock:
            return self._state

    @state.setter
    def state(self, val):
        with self._lock:
            self._state = val

    def set_system_speaking(self, v):
        self._system_speaking = v
        if v:
            self._resume_at = 0.0
        else:
            # Hold the mic muted briefly after playback stops so the
            # speaker's echo tail cannot be picked up as a new utterance.
            self._resume_at = time.monotonic() + MIC_SETTLE_MS / 1000.0

    def set_manual_mute(self, muted):
        """Enable/disable the operator-controlled microphone mute."""
        self._manual_muted = bool(muted)
        if self._manual_muted:
            # Abort an in-flight capture at its next frame boundary.
            self._resume_at = 0.0
        elif not self._system_speaking:
            # Do not replay stale audio captured just before the button press.
            self._resume_at = time.monotonic()

    def is_manual_muted(self):
        return bool(self._manual_muted)

    def _mic_muted(self):
        """True while the system is speaking or during the echo-settle window."""
        return (self._manual_muted or self._system_speaking
                or time.monotonic() < self._resume_at)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ASRWorker")
        self._thread.start()
        print("[ASR] Worker started, mic=" + MIC_DEVICE)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.backend.close()

    def get_result(self):
        try:
            return self._result_queue.get_nowait()
        except queue.Empty:
            return None

    def health(self):
        return {
            "loaded": self.backend.loaded,
            "state": self._state,
            "backend": "whisper_tiny_cpu",
            "source": self._mic_source_active,
            "manual_muted": bool(self._manual_muted),
            "stats": dict(self._stats),
            "last_text": self._last_text,
            "last_capture": dict(self._last_capture),
        }

    def transcribe_file(self, wav_path, timeout=30):
        """Transcribe a WAV with the same board model without racing the mic loop."""
        with self._backend_lock:
            return self.backend.transcribe(wav_path, timeout=timeout)

    def _run(self):
        """Main loop: listen -> detect speech -> record -> transcribe."""
        self.state = "listening"
        while self._running:
            try:
                # Empty-transcription cooldown: pause inference after repeated
                # pure-noise false triggers to save CPU / memory.
                if time.monotonic() < self._empty_cooldown_until:
                    time.sleep(0.2)
                    continue

                if self._mic_muted():
                    time.sleep(0.1)
                    continue

                # Record a short chunk and check for speech
                chunk_path = tempfile.mktemp(suffix='.wav', dir='/tmp')
                listen_started = time.monotonic()
                uttered = self._listen_and_record(chunk_path)

                if uttered and os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                    self.state = "transcribing"
                    infer_started = time.monotonic()
                    result = self.transcribe_file(chunk_path, timeout=30)
                    infer_ms = (time.monotonic() - infer_started) * 1000.0
                    end_to_end_ms = (time.monotonic() - listen_started) * 1000.0
                    self._last_capture.update({
                        "inference_ms": round(infer_ms, 1),
                        "end_to_end_ms": round(end_to_end_ms, 1),
                    })
                    self._stats["total"] += 1

                    if result["success"] and result["text"]:
                        self._stats["success"] += 1
                        self._last_text = result["text"]
                        self._empty_count = 0  # real speech → reset the counter
                        print(f"[ASR] text=\"{result['text']}\" "
                              f"endpoint={self._last_capture.get('speech_ms', 0):.0f}ms "
                              f"infer={result['inference_ms']:.0f}ms "
                              f"delivery={self._last_capture.get('speech_ms', 0) + infer_ms:.0f}ms "
                              f"rtf={result['rtf']:.2f}")
                        try:
                            self._result_queue.put_nowait(result)
                        except queue.Full:
                            pass
                    else:
                        print(f"[ASR] no text, err={result.get('error','')}")
                        # Only "recognized successfully but text is empty"
                        # (TTS echo / ambient-noise false trigger) counts toward
                        # the cooldown; a failed SDK call (e.g. DSP busy, ret!=0)
                        # does not — it may be transient, and counting it would
                        # make a busy board progressively deaf.
                        if result.get("success"):
                            self._empty_count += 1
                            if self._empty_count >= EMPTY_TRIGGER:
                                self._empty_cooldown_until = time.monotonic() + EMPTY_COOLDOWN_S
                                print(f"[ASR] {self._empty_count} consecutive empty -> cooldown {EMPTY_COOLDOWN_S}s",
                                      flush=True)
                                self._empty_count = 0

                    self.state = "listening"

                # Cleanup temp WAV
                try:
                    os.unlink(chunk_path)
                except Exception:
                    pass

            except Exception as e:
                print(f"[ASR] Worker error: {e}")
                self._stats["crash"] += 1
                time.sleep(1)

    def _open_mic_reader(self):
        """Open the recording stream according to ASR_MIC_SOURCE; None on failure.

        board  : board USB mic via arecord
        remote : PC mic HTTP stream (needs pc_mic_server.py on the PC + adb reverse)
        auto   : board first, falling back to the PC mic when arecord fails to
                 start (device missing or broken)
        """
        mode = MIC_SOURCE
        if mode == "remote":
            try:
                self._mic_source_active = "remote"
                return _RemoteMicStream(
                    REMOTE_MIC_URL, timeout=REMOTE_MIC_TIMEOUT).open()
            except Exception as e:
                self._mic_source_active = "none"
                print("[ASR] remote mic unavailable: %s" % str(e)[:100], flush=True)
                return None
        # board / auto
        try:
            self._mic_source_active = "board"
            return _BoardMicStream(MIC_DEVICE)
        except Exception as e:
            if mode == "auto":
                self._stats["fallback"] = self._stats.get("fallback", 0) + 1
                print("[ASR] board mic failed (%s) -> PC mic fallback"
                      % str(e)[:80], flush=True)
                try:
                    self._mic_source_active = "remote"
                    return _RemoteMicStream(
                        REMOTE_MIC_URL, timeout=REMOTE_MIC_TIMEOUT).open()
                except Exception as e2:
                    self._mic_source_active = "none"
                    print("[ASR] remote mic fallback also failed: %s"
                          % str(e2)[:100], flush=True)
                    return None
            self._mic_source_active = "none"
            print("[ASR] board mic failed: %s" % str(e)[:100], flush=True)
            return None

    def _listen_and_record(self, out_path):
        """Record until speech detected + silence tail. Returns True if utterance captured."""
        # Re-applying gain is cheap and makes hot-unplug/replug recover without
        # restarting the EXE. Limit it to once per 15 seconds.
        now = time.monotonic()
        if now - self._last_gain_check >= 15.0:
            try:
                subprocess.run(
                    ["amixer", "-q", "-c", MIC_CARD,
                     "sset", "Mic", "100%", "cap"],
                    timeout=3,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            self._last_gain_check = now

        max_listen_frames = int(MAX_LISTEN_SEC * 1000 / FRAME_MS)
        max_speech_frames = int(MAX_UTTERANCE_SEC * 1000 / FRAME_MS)
        tail_frames = int(SILENCE_TAIL_MS / FRAME_MS)
        onset_frames = max(1, int(ONSET_CONFIRM_MS / FRAME_MS))
        min_frames = int(MIN_UTTERANCE_MS / FRAME_MS)

        # Recording source: board mic (arecord) or PC mic (HTTP stream); falls
        # back when the device is broken or missing.
        reader = self._open_mic_reader()
        if reader is None:
            return False

        speech_frames = []
        pre_roll = collections.deque(maxlen=max(1, int(300 / FRAME_MS)))
        in_speech = False
        silence_count = 0
        onset_count = 0
        total_frames = 0
        noise_samples = []
        capture_started = time.monotonic()
        speech_started = None

        try:
            while total_frames < max_listen_frames:
                if self._mic_muted():
                    # TTS playback started while we were listening. Abort this
                    # capture so the speaker's own voice is never transcribed
                    # and fed back into the AI.
                    return False
                raw = reader.read(FRAME_SAMPLES * 2)  # S16_LE = 2 bytes per sample
                if len(raw) < FRAME_SAMPLES * 2:
                    break
                if not in_speech:
                    pre_roll.append(raw)

                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                rms = np.sqrt(np.mean(samples ** 2))

                # Calibrate for 300 ms instead of trusting a single frame.
                # A single quiet first frame made USB-mic/AGC noise look like
                # speech, causing repeated false transcriptions that blocked
                # the real utterance behind them.
                if len(noise_samples) < 10:
                    noise_samples.append(float(rms))
                    continue
                noise_level = max(float(np.median(noise_samples)), 10.0)

                total_frames += 1

                if not in_speech:
                    trigger_level = max(float(SILENCE_THRESHOLD), noise_level * 2.5)
                    if rms > trigger_level:
                        onset_count += 1
                    else:
                        onset_count = 0
                    if onset_count >= onset_frames:
                        in_speech = True
                        silence_count = 0
                        speech_started = time.monotonic()
                        # Preserve the start of the utterance. Previously the
                        # trigger frame was dropped, often clipping short wake
                        # words and leaving an SDK input with too little speech.
                        speech_frames.extend(pre_roll)
                        self.state = "recording"
                    else:
                        noise_level = noise_level * 0.95 + rms * 0.05  # adaptive
                        noise_samples.append(float(rms))
                        if len(noise_samples) > 30:
                            del noise_samples[:-30]
                else:
                    speech_frames.append(raw)
                    if len(speech_frames) >= max_speech_frames:
                        break
                    if rms < noise_level * 1.5 or rms < SILENCE_THRESHOLD:
                        silence_count += 1
                    else:
                        silence_count = 0

                    if silence_count >= tail_frames:
                        break

        finally:
            try:
                reader.close()
            except Exception:
                pass

        # Write WAV
        if in_speech and len(speech_frames) >= min_frames:
            # Fibocom Whisper rejects sub-second WAVs. Padding happens after
            # endpointing, so it improves short-command reliability without
            # adding any user-facing VAD wait.
            sdk_min_frames = int(SDK_MIN_AUDIO_MS / FRAME_MS)
            if len(speech_frames) < sdk_min_frames:
                silence = b'\x00' * (FRAME_SAMPLES * 2)
                speech_frames.extend(
                    [silence] * (sdk_min_frames - len(speech_frames)))
            all_data = b''.join(speech_frames)
            with wave.open(out_path, 'wb') as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(all_data)
            self._last_capture = {
                "capture_ms": round((time.monotonic() - capture_started) * 1000.0, 1),
                "speech_ms": round((time.monotonic() - (speech_started or capture_started)) * 1000.0, 1),
                "audio_ms": len(speech_frames) * FRAME_MS,
                "noise_rms": round(float(noise_level), 1),
            }
            return True
        return False
