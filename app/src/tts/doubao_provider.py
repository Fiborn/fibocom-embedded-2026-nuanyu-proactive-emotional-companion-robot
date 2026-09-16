#!/usr/bin/env python3
"""
DoubaoTTSProvider — Doubao speech synthesis 2.0 (seed-tts-2.0) streaming provider

API: POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
Auth: X-Api-Key, X-Api-Resource-Id, X-Api-Request-Id
Response: chunked JSON lines, each with base64 PCM data

Interface (matches fibo_tts_speak_async):
    speak_async(text, callback) → bool  (queued)
    stop()                         (abort net + playback)
    is_speaking() → bool
    shutdown()                     (cleanup)

Config via env:
    DOUBAO_TTS_API_KEY
    DOUBAO_TTS_RESOURCE_ID  (default: seed-tts-2.0)
    DOUBAO_TTS_VOICE_TYPE   (default: zh_female_vv_uranus_bigtts)
    DOUBAO_TTS_SAMPLE_RATE  (default: 24000)

Voice selection persisted to <NUANYU_ROOT>/data/stream_voice.json.
"""

import os, sys, time, json, uuid, base64, threading, subprocess, ssl, queue, wave, struct, audioop
import urllib.request
from src.tts.audio_router import output_mode, play_on_pc_file

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get('NUANYU_ROOT', '/userdata_fibo')

# Import the onboard playback chain (HPH routing + aplay)
sys.path.insert(0, os.path.join(NUANYU_ROOT, 'tts_client'))
import fibo_tts as _fibo_tts_module

# ═══════════════════════════════════════════════════════════════════
#  Allowed voices  —  backend-enforced allowlist
# ═══════════════════════════════════════════════════════════════════

ALLOWED_VOICES = {
    "zh_female_vv_uranus_bigtts":       "Vivi 2.0",
    "zh_female_qiaopinv_uranus_bigtts": "俏皮女声 2.0",
    "zh_male_ruyayichen_uranus_bigtts": "儒雅逸辰 2.0",
    "zh_male_dayi_uranus_bigtts":       "大壹 2.0",
}

# ═══════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════

API_KEY = ""
RESOURCE_ID = "seed-tts-2.0"
VOICE_TYPE = "zh_female_vv_uranus_bigtts"
SAMPLE_RATE = 24000
API_URL = os.environ.get(
    "DOUBAO_TTS_API_URL",
    "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
)
ALSA_DEVICE = "hw:%s,%s" % (
    os.environ.get("FIBO_TTS_HPH_CARD", "2"),
    os.environ.get("FIBO_TTS_HPH_DEVICE", "0"),
)
FIRST_CHUNK_TIMEOUT = 15.0
TOTAL_TIMEOUT = 60.0

VOICE_CONFIG_PATH = os.path.join(NUANYU_ROOT, "data", "stream_voice.json")

# Preview text — fixed, short enough for quick audition
PREVIEW_TEXT = "你好，很高兴今天也能陪着你。"


def _load_voice_preference():
    """Read persisted voice choice from disk.  Returns None on any failure."""
    try:
        if os.path.exists(VOICE_CONFIG_PATH):
            with open(VOICE_CONFIG_PATH, "r") as fh:
                data = json.load(fh)
            vt = str(data.get("voice_type", "")).strip()
            if vt in ALLOWED_VOICES:
                return vt
    except Exception:
        pass
    return None


def _save_voice_preference(voice_type):
    """Persist voice choice to disk."""
    try:
        os.makedirs(os.path.dirname(VOICE_CONFIG_PATH), exist_ok=True)
        with open(VOICE_CONFIG_PATH, "w") as fh:
            json.dump({"voice_type": voice_type}, fh)
    except Exception:
        pass


def _reload_config():
    global API_KEY, RESOURCE_ID, VOICE_TYPE, SAMPLE_RATE
    API_KEY = os.environ.get("DOUBAO_TTS_API_KEY", "")
    RESOURCE_ID = os.environ.get("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0")
    SAMPLE_RATE = int(os.environ.get("DOUBAO_TTS_SAMPLE_RATE", "24000"))

    # Voice precedence:  persistence > env > hard-coded default
    persisted = _load_voice_preference()
    if persisted:
        VOICE_TYPE = persisted
    else:
        VOICE_TYPE = os.environ.get(
            "DOUBAO_TTS_VOICE_TYPE", "zh_female_vv_uranus_bigtts")
        # Sanity-check env value
        if VOICE_TYPE not in ALLOWED_VOICES:
            VOICE_TYPE = "zh_female_vv_uranus_bigtts"

_reload_config()


# ═══════════════════════════════════════════════════════════════════
#  Provider class
# ═══════════════════════════════════════════════════════════════════

class DoubaoTTSProvider:
    """Doubao TTS streaming provider — plays while receiving."""

    def __init__(self):
        _reload_config()
        self._loaded = bool(API_KEY)
        self._speaking = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._req_queue = queue.Queue(maxsize=16)
        self._worker_started = False
        self._worker_lock = threading.Lock()
        self._player = None
        self._player_lock = threading.Lock()

        if self._loaded:
            label = ALLOWED_VOICES.get(VOICE_TYPE, VOICE_TYPE)
            print("[Doubao] Ready speaker=%s (%s) rate=%dHz" %
                  (VOICE_TYPE, label, SAMPLE_RATE))
        else:
            print("[Doubao] DOUBAO_TTS_API_KEY not set — disabled")

    # ── public interface ──

    @property
    def loaded(self):
        return self._loaded

    @property
    def current_voice(self):
        """Return the active voice_type string (e.g. zh_female_vv_uranus_bigtts)."""
        return VOICE_TYPE

    @property
    def current_voice_label(self):
        """Return the human-readable label for the active voice."""
        return ALLOWED_VOICES.get(VOICE_TYPE, VOICE_TYPE)

    def speak_async(self, text, callback=None):
        """Non-blocking playback. Queued; does not interrupt the sentence being played. callback(ok, error_str)"""
        if not self._loaded or not text or len(str(text).strip()) < 2:
            if callback:
                callback(False, "not loaded or empty text")
            return False

        text_clean = str(text).strip()
        with self._lock:
            try:
                if self._req_queue.full():
                    try:
                        self._req_queue.get_nowait()
                    except Exception:
                        pass
                self._req_queue.put_nowait((text_clean, callback))
            except Exception:
                if callback:
                    callback(False, "queue full")
                return False

        self._ensure_worker()
        return True

    def speak_preview(self, callback=None):
        """Play the fixed preview text with the currently selected voice."""
        return self.speak_async(PREVIEW_TEXT, callback)

    def stop(self):
        """Abort the current playback"""
        with self._lock:
            self._stop_locked()
        self._close_player()

    def is_speaking(self):
        with self._lock:
            return self._speaking

    def shutdown(self):
        """Release resources"""
        self.stop()
        self._close_player()

    def health(self):
        return {
            "loaded": self._loaded,
            "speaker": VOICE_TYPE,
            "speaker_label": ALLOWED_VOICES.get(VOICE_TYPE, VOICE_TYPE),
            "sample_rate": SAMPLE_RATE,
            "endpoint": API_URL,
            "speaking": self._speaking,
        }

    # ── Voice switching ──────────────────────────────────────────

    @staticmethod
    def get_available_voices():
        """Return the allowed voice list (safe for API exposure)."""
        return [
            {"voice_type": vt, "label": label}
            for vt, label in ALLOWED_VOICES.items()
        ]

    @staticmethod
    def set_voice(voice_type):
        """Switch the active voice.  Returns (ok, message)."""
        global VOICE_TYPE
        vt = str(voice_type).strip()
        if vt not in ALLOWED_VOICES:
            return False, "unsupported voice_type: %s (allowed: %s)" % (
                vt, ", ".join(ALLOWED_VOICES.keys()))
        old = VOICE_TYPE
        VOICE_TYPE = vt
        _save_voice_preference(vt)
        label = ALLOWED_VOICES[vt]
        print("[Doubao] voice switched: %s → %s (%s)" % (old, vt, label),
              flush=True)
        return True, label

    # ── internal ──

    def _ensure_worker(self):
        with self._worker_lock:
            if not self._worker_started:
                self._worker_started = True
                threading.Thread(target=self._queue_worker,
                                 daemon=True, name="DoubaoQ").start()

    def _queue_worker(self):
        """Process the queue serially: one sentence at a time, no skipping"""
        while True:
            text, callback = self._req_queue.get()
            try:
                self._stream_worker(text, callback)
            except Exception:
                if callback:
                    try:
                        callback(False, "worker crash")
                    except Exception:
                        pass
            try:
                self._req_queue.task_done()
            except Exception:
                pass

    def _stop_locked(self):
        self._stop_event.set()
        self._speaking = False

    def _stream_worker(self, text, callback):
        """Background thread: HTTP → collect PCM → write WAV → HPH playback"""
        t_start = time.perf_counter()
        total_pcm = bytearray()
        first_audio = False
        first_ms = 0
        ok = False
        error = ""
        rate_state = None
        streamed_source_bytes = 0
        first_play_started = None
        player = None
        self._stop_event.clear()
        # Snapshot the current voice at call time so queued items use the
        # voice that was active when they were enqueued.
        current_speaker = VOICE_TYPE

        try:
            # 1. Build the request
            reqid = uuid.uuid4().hex
            payload = json.dumps({
                "user": {"uid": "nuanyu-stream"},
                "req_params": {
                    "text": text,
                    "speaker": current_speaker,
                    "audio_params": {
                        "format": "pcm",
                        "sample_rate": SAMPLE_RATE,
                    }
                }
            }, ensure_ascii=False).encode("utf-8")

            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "X-Api-Key": API_KEY,
                "X-Api-Resource-Id": RESOURCE_ID,
                "X-Api-Request-Id": reqid,
            }

            req = urllib.request.Request(
                API_URL, data=payload, headers=headers, method="POST")
            ctx = ssl.create_default_context()

            # 2. Send the request
            resp = urllib.request.urlopen(
                req, timeout=TOTAL_TIMEOUT, context=ctx)
            # Qualcomm HPH requires 48 kHz stereo S16_LE. Feed it as chunks so
            # speech starts while the remaining cloud audio is still arriving.
            # Demo deployments may route synthesized speech to the PC.
            # Collect the cloud PCM and send one WAV to the PC player instead
            # of opening the unavailable board HPH route.
            mode = output_mode()
            use_pc_speaker = mode == "pc"
            board_stream_failed = False
            player = None if use_pc_speaker else self._open_player()

            # 3. Stream-read every PCM chunk
            buf = b""
            while not self._stop_event.is_set():
                try:
                    chunk = resp.read(8192)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    code = obj.get("code", -1)
                    if code not in (0, 20000000):
                        error = "API code=%s: %s" % (
                            code, obj.get("message", "")[:100])
                        break

                    data_b64 = obj.get("data", "")
                    if data_b64:
                        pcm = base64.b64decode(data_b64)
                        total_pcm.extend(pcm)
                        if player is not None:
                            converted, rate_state = audioop.ratecv(
                                pcm, 2, 1, SAMPLE_RATE, 48000, rate_state)
                            hal_pcm = audioop.tostereo(converted, 2, 1.0, 1.0)
                            if self._write_pcm(player, hal_pcm):
                                streamed_source_bytes += len(pcm)
                                if first_play_started is None:
                                    first_play_started = time.perf_counter()
                            else:
                                board_stream_failed = True
                                player = None
                        if not first_audio:
                            first_audio = True
                            first_ms = (time.perf_counter() - t_start) * 1000
                            label = ALLOWED_VOICES.get(
                                current_speaker, current_speaker)
                            sys.stdout.write(
                                "[Doubao] first_audio=%.0fms speaker=%s (%s)\n"
                                % (first_ms, current_speaker, label))
                            sys.stdout.flush()

                if error:
                    break

                if not first_audio and \
                   (time.perf_counter() - t_start) > FIRST_CHUNK_TIMEOUT:
                    error = "first chunk timeout"
                    break

            if (not error and streamed_source_bytes > 0
                    and not board_stream_failed):
                # stdin writes can run ahead of the speaker by one ALSA buffer.
                # Keep ASR muted until the queued PCM should have drained.
                audio_seconds = len(total_pcm) / float(SAMPLE_RATE * 2)
                played_for = (time.perf_counter() - first_play_started
                              if first_play_started else 0.0)
                remaining = max(0.0, audio_seconds - played_for)
                if remaining:
                    time.sleep(remaining)
                ok = True
            elif not error and len(total_pcm) > 0:
                # Compatibility fallback when raw streaming cannot be opened.
                wav_path = "/tmp/doubao_%s.wav" % uuid.uuid4().hex[:8]
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(bytes(total_pcm))

                if use_pc_speaker:
                    played = play_on_pc_file(wav_path)
                    if played:
                        sys.stdout.write("[Doubao] PC speaker playback OK\n")
                        sys.stdout.flush()
                else:
                    played = _fibo_tts_module._play_wav(wav_path)
                    if not played and mode == "auto":
                        played = play_on_pc_file(wav_path)
                        if played:
                            sys.stdout.write(
                                "[Doubao] board unavailable; PC fallback OK\n"
                            )
                            sys.stdout.flush()

                # Outcome: the first playback call above already covers
                # board-first + PC fallback. Do NOT call _play_wav again here —
                # that would play the same sentence's WAV twice (twice on the
                # board in board mode, PC + board in PC mode).
                if played:
                    ok = True
                else:
                    error = "audio playback failed"
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

        except urllib.error.HTTPError as e:
            error = "HTTP %d" % e.code
        except Exception as e:
            error = str(e)[:200]
        finally:
            total_ms = (time.perf_counter() - t_start) * 1000
            audio_dur = len(total_pcm) / (SAMPLE_RATE * 2) * 1000
            rtf = total_ms / audio_dur if audio_dur > 0 else 0
            label = ALLOWED_VOICES.get(current_speaker, current_speaker)

            status = "OK" if ok else "FAIL"
            sys.stdout.write(
                "[Doubao] %s speaker=%s (%s) text=%dchars pcm=%dB "
                "dur=%.0fms total=%.0fms rtf=%.2f err=%s\n" %
                (status, current_speaker, label, len(text), len(total_pcm),
                 audio_dur, total_ms, rtf, error[:80])
            )
            sys.stdout.flush()

            with self._lock:
                self._speaking = False

            if callback:
                try:
                    callback(ok, error, {
                        "first_audio_ms": int(first_ms or total_ms),
                        "backend": "stream",
                    })
                except Exception:
                    pass

    # ── Audio playback (persistent aplay process) ──

    def _open_player(self):
        """Start a resident aplay process and return the Popen object"""
        with self._player_lock:
            if self._player is not None and self._player.poll() is None:
                return self._player

            cmd = [
                "aplay", "-q", "-D", ALSA_DEVICE,
                "-f", "S16_LE", "-r", "48000", "-c", "2",
                "--period-size=1024", "--buffer-size=4096",
            ]
            try:
                self._player = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return self._player
            except Exception as e:
                sys.stdout.write("[Doubao] aplay start failed: %s\n" % str(e))
                sys.stdout.flush()
                return None

    def _write_pcm(self, player, data):
        """Write PCM to aplay's stdin"""
        if player is None or player.poll() is not None:
            return False
        try:
            player.stdin.write(data)
            player.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _close_player(self):
        """Shut down the aplay process"""
        with self._player_lock:
            if self._player is not None:
                try:
                    self._player.stdin.close()
                except Exception:
                    pass
                try:
                    self._player.terminate()
                    self._player.wait(timeout=2)
                except Exception:
                    try:
                        self._player.kill()
                    except Exception:
                        pass
                self._player = None


# ═══════════════════════════════════════════════════════════════════
#  Global singleton & public API
# ═══════════════════════════════════════════════════════════════════

_global_doubao = None
_global_lock = threading.Lock()


def get_doubao_provider():
    global _global_doubao
    if _global_doubao is not None:
        return _global_doubao
    with _global_lock:
        if _global_doubao is not None:
            return _global_doubao
        _global_doubao = DoubaoTTSProvider()
        return _global_doubao


def doubao_speak_async(text, callback=None):
    provider = get_doubao_provider()
    return provider.speak_async(text, callback)


def doubao_stop():
    provider = get_doubao_provider()
    provider.stop()


def doubao_is_speaking():
    provider = get_doubao_provider()
    return provider.is_speaking()


def doubao_shutdown():
    global _global_doubao
    if _global_doubao is not None:
        _global_doubao.shutdown()
        _global_doubao = None


def doubao_set_voice(voice_type):
    """Switch the active Doubao voice.  Returns (ok, message)."""
    provider = get_doubao_provider()
    return provider.set_voice(voice_type)


def doubao_get_voices():
    """Return the allowed voice list."""
    return DoubaoTTSProvider.get_available_voices()


def doubao_current_voice():
    """Return (voice_type, label) for the active voice."""
    provider = get_doubao_provider()
    return provider.current_voice, provider.current_voice_label


def doubao_preview(callback=None):
    """Play the fixed preview text with the current voice."""
    provider = get_doubao_provider()
    return provider.speak_preview(callback)
