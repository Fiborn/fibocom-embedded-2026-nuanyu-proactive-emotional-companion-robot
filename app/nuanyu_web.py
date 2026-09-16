# -*- coding: utf-8 -*-
"""
nuanyu_web.py
Nuanyu Web — voice conversation edition 🎤
Web frontend + camera + voice commands + DeepSeek AI + 🎤continuous voice chat (ASR) + focus goals + study report + mood self-rating + proactive care + local memory
+ multi-user login with per-user memory and custom character names
+ TTS preload + DeepSeek output token limit

Runs on: the Fibocom board
Access from a PC: http://127.0.0.1:5004

On Windows, run these first:
adb reverse tcp:5002 tcp:5002  (ASR service)
adb forward tcp:5004 tcp:5004
"""

import collections
import cv2
import time
import json
import os
import re

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")
# The application's own directory: this file lives at the app root.
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# The board must have exactly one web/UART/sensor owner.  Acquire the lock
# before importing or initializing the heavyweight runtime.  Imported test
# modules do not take the lock; only the executable service process does.
_INSTANCE_LOCK_FD = None
if __name__ == "__main__":
    import fcntl
    _INSTANCE_LOCK_FD = open("/run/nuanyu_web_5004.lock", "w")
    try:
        fcntl.flock(
            _INSTANCE_LOCK_FD.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except (BlockingIOError, OSError):
        print(
            "[STARTUP] another Nuanyu web instance already owns port 5004",
            flush=True,
        )
        raise SystemExit(73)
    _INSTANCE_LOCK_FD.write(str(os.getpid()))
    _INSTANCE_LOCK_FD.flush()

# ★ global thread crash capture
import threading as _thr
def _thread_crash(args):
    import traceback as _tb
    try:
        with open(os.path.join(NUANYU_ROOT, "crash.log"), "a") as f:
            f.write(f"\n[CRASH] Thread={args.thread.name}\n")
            _tb.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
    except Exception:
        pass
    # Don't os._exit — let the thread die, keep main process alive
_thr.excepthook = _thread_crash
import threading
import urllib.request
import ssl
import certifi
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
import termios
import select
import hashlib
import uuid
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
import socket as _socket
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def server_bind(self):
        # SO_REUSEADDR permits a clean restart after TIME_WAIT.  Never enable
        # SO_REUSEPORT: it load-balances requests across duplicate processes
        # and makes AI/sensor state appear to randomly flap.
        self.socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        HTTPServer.server_bind(self)
from urllib.parse import urlparse

# ── Weather tool (Open-Meteo, keyless, works on contest hotspots and the open internet) ──
# City name → geocoding → current weather. On failure returns ok=False and the LLM
# relays that truthfully; never fabricates. GET timeout is short so the tool thread never hangs.
_WMO_CODE_TEXT = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _weather_http_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Nuanyu/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _weather_provider(city: str) -> dict:
    """Keyless Open-Meteo lookup: ``city -> current weather``.

    Returns a dict of real measurements on success, or ``{'ok': False, 'error': ...}``
    on failure (unknown city / network / malformed reply). Never fabricates data.
    """
    try:
        city = (city or "").strip() or "上海"
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search?"
            + urllib.parse.urlencode(
                {"name": city, "count": 1, "language": "zh"}
            )
        )
        geo = _weather_http_json(geo_url)
        results = geo.get("results") or []
        if not results:
            return {"ok": False, "error": "未找到城市：%s" % city}
        loc = results[0]
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is None or lon is None:
            return {"ok": False, "error": "无法解析城市坐标：%s" % city}
        name = loc.get("name") or city
        admin = loc.get("admin1") or loc.get("admin2") or ""
        # Deduplicate: admin1 "上海市" + name "上海" → "上海市"
        city_label = admin if (admin and name in admin) else (admin + name if admin else name)
        weather_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode(
                {
                    "latitude": lat,
                    "longitude": lon,
                    "current_weather": "true",
                    "timezone": "Asia/Shanghai",
                    "temperature_unit": "celsius",
                    "windspeed_unit": "kmh",
                }
            )
        )
        w = _weather_http_json(weather_url)
        cur = w.get("current_weather") or {}
        if not cur:
            return {"ok": False, "error": "天气数据暂不可用"}
        wmo = int(cur.get("weathercode", 0) or 0)
        return {
            "ok": True,
            "city": city_label,
            "temperature_c": cur.get("temperature"),
            "weather": _WMO_CODE_TEXT.get(wmo, "未知"),
            "wind_speed_kmh": cur.get("windspeed", 0),
            "is_day": bool(cur.get("is_day", 1)),
            "source": "open-meteo",
        }
    except Exception as exc:
        return {"ok": False, "error": "天气服务不可用：%s" % str(exc)[:120]}


# Board peripherals. All three are documented as configurable, and the defaults
# reproduce the on-board wiring exactly.
CAMERA_DEVICE = os.environ.get("CAMERA_DEVICE", "/dev/video2")
VOICE_PORT = os.environ.get("VOICE_PORT", "/dev/ttyHS1")
VOICE_BAUD = int(os.environ.get("VOICE_BAUD", "9600"))

# AI configuration — DeepSeek API (OpenAI-compatible)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = "deepseek-v4-flash"
AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "15"))

# ======================== DeepSeek persistent connection ========================
_deepseek_client = None
_deepseek_client_lock = threading.Lock()

def _get_deepseek_client():
    """Persistent HTTPS connection — reuses DNS/TCP/TLS, saving the ~300ms connect each call"""
    global _deepseek_client
    if _deepseek_client is not None:
        return _deepseek_client
    with _deepseek_client_lock:
        if _deepseek_client is not None:
            return _deepseek_client
        from src.core.deepseek_client import DeepSeekHTTPClient
        _deepseek_client = DeepSeekHTTPClient(
            DEEPSEEK_BASE_URL, _SSL_CTX, timeout=AI_TIMEOUT_SECONDS)
        ok = _deepseek_client.warmup()
        _set_deepseek_ok(ok)
        print("[DEEPSEEK] Persistent HTTP client warmup %s" % ("OK" if ok else "FAILED"))
        return _deepseek_client


# Global DeepSeek reachability (cached), used by /api/status to report the real "AI service" state.
# Do not use the per-robot ai_ok ("did that robot chat successfully recently"):
# anonymous/mini-program requests get the default robot (no chat yet, ai_ok=False) and would show "AI down".
# ⚠️ Never do a blocking warmup on the /api/status path: a streaming DeepSeek reply holds
# client._lock the whole time, so warmup stalls status → the watchdog's probes keep failing → web is killed.
# Only the cached flag is returned here; ask_ai success/failure + startup warmup update it.
_DEEPSEEK_OK = False


def _global_deepseek_ok():
    return _DEEPSEEK_OK


def _set_deepseek_ok(ok):
    global _DEEPSEEK_OK
    _DEEPSEEK_OK = bool(ok)

# ASR (speech recognition) — PC-side sherpa-onnx SenseVoice service
# The board reaches the PC's ASR service through adb reverse tcp:5002 tcp:5002
ASR_SERVER_URL = "http://127.0.0.1:5002/transcribe"
ASR_TIMEOUT_SECONDS = 30

# TTS: Drizzle = Fibocom on-board DSP; Stream = Doubao cloud;
# Surge = PC ZipVoice. All share the Fibocom HPH speaker route, with no cross-engine fallback.
import sys
sys.path.insert(0, os.path.join(NUANYU_ROOT, "tts_client"))
sys.path.insert(0, APP_DIR)
TTS_BACKEND = os.environ.get("NUANYU_TTS_BACKEND", "drizzle").strip().lower()
if TTS_BACKEND not in ("drizzle", "stream", "surge"):
    TTS_BACKEND = "drizzle"
# drizzle only swaps the backend model (piper → Fibocom on-board DSP); backend structure/streaming/mic-mute logic is unchanged.
# Non-DSP backends (stream/surge) tell fibo_tts not to eagerly initialize the QNN model (saves ~7s startup + memory);
# drizzle initializes eagerly, so the first sentence no longer waits for the model to load.
if TTS_BACKEND == "drizzle":
    os.environ.setdefault("FIBO_TTS_ENGINE", "fibo")
else:
    os.environ.setdefault("FIBO_TTS_ENGINE", "piper")
import fibo_tts as _fibo_tts_module
_fibo_tts_module.hph_always_on_init()  # keep HPH speaker route open for low-latency TTS

import builtins as _bi
_bi._last_tts_ms_tracker = {"stream": 0, "surge": 0, "drizzle": 0}
_tts_backend_lock = threading.Lock()
# Overall tool-calling budget: hard deadline for multi-round DeepSeek + tool execution inside the
# background thread; on timeout it falls back to streaming ask_ai.
TOOL_BUDGET_S = float(os.environ.get("TOOL_BUDGET_S", "15"))

# Milliseconds to delay the "<nickname>," opening line: the audio is ready once synthesized/cached,
# but playback waits the given time so DeepSeek reasoning, later-sentence synthesis and first-sentence
# playback run in parallel within one window and the first sentence joins naturally;
# the first-audio latency shown in the frontend (submit→play) is therefore a real value, not a cache hit ≈0ms.
_OPENING_DELAY_MS = int(os.environ.get("TTS_OPENING_DELAY_MS", "1000"))

_doubao_available = False
_drizzle_backend = None
_drizzle_backend_lock = threading.Lock()
try:
    from src.tts.doubao_provider import (
        doubao_speak_async,
        doubao_stop,
        get_doubao_provider,
        doubao_set_voice,
        doubao_get_voices,
        doubao_current_voice,
        doubao_preview,
    )
    _doubao_available = True
    print("[TTS] Stream/Doubao provider imported")
except Exception as _e:
    print("[TTS] Stream/Doubao import failed: %s" % str(_e)[:160])

# ── Surge / ZipVoice ──
_surge_available = False
_surge_provider = None
_surge_lock = threading.Lock()
try:
    from src.tts.surge_lite_provider import (
        surge_speak_async,
        surge_stop,
        get_surge_provider,
    )
    _surge_provider = get_surge_provider()
    _surge_available = _surge_provider.server_available if _surge_provider else False
    print("[TTS] Surge/ZipVoice provider imported server=%s" % ("OK" if _surge_available else "OFFLINE"))
except Exception as _e:
    print("[TTS] Surge/ZipVoice import failed: %s" % str(_e)[:160])
    _surge_available = False
    _surge_provider = None

def _get_surge_speak_fn():
    if not _surge_available:
        raise RuntimeError("Surge/ZipVoice is unavailable")
    return surge_speak_async

def _get_drizzle_backend():
    global _drizzle_backend
    if _drizzle_backend is not None:
        return _drizzle_backend
    with _drizzle_backend_lock:
        if _drizzle_backend is None:
            from src.tts.drizzle_backend import DrizzleBackend
            _drizzle_backend = DrizzleBackend()
    return _drizzle_backend

def _drizzle_speak_async(text, callback=None):
    return _get_drizzle_backend().speak_async(text, callback)

print("[TTS] current backend: %s" % TTS_BACKEND.upper())

def set_tts_backend(backend_name):
    """Switch to one exact engine. Never disguise a fallback as another mode."""
    global TTS_BACKEND, _stream_tts_coordinator, _surge_available
    backend = str(backend_name).strip().lower()
    if backend not in ("drizzle", "stream", "surge"):
        return False, "invalid backend: %s (use drizzle/stream/surge)" % backend
    if backend == "drizzle" and not _get_drizzle_backend().loaded:
        return False, "Drizzle 后端不可用"
    if backend == "stream":
        if not _doubao_available:
            return False, "Stream/Doubao provider is not installed"
        if not get_doubao_provider().loaded:
            return False, "Stream/Doubao credentials are not loaded"
    if backend == "surge":
        # ZipVoice loses its in-memory voice packs whenever the PC service is
        # restarted.  Always re-check and re-activate the selected voice here;
        # a stale _surge_available=True must not produce a silent fake switch.
        if not _surge_provider:
            return False, "Surge/ZipVoice provider is not installed"
        try:
            if not _surge_provider._check_server(retries=5, delay=1.0):
                _surge_available = False
                return False, "Surge/ZipVoice server is offline"
            _surge_available = True
            active_voice_id = _surge_provider._active_voice_id
            if not active_voice_id:
                return False, "Surge voice is not selected"
            if not _surge_provider._activate_on_server(active_voice_id):
                _surge_available = False
                return False, "Surge voice activation failed"
            _surge_provider._server_voice_id = active_voice_id
        except Exception as exc:
            _surge_available = False
            return False, "Surge activation error: %s" % str(exc)[:120]
    with _tts_backend_lock:
        old = TTS_BACKEND
        TTS_BACKEND = backend
        # Invalidate the old coordinator; the next call rebuilds it with the new backend
        if _stream_tts_coordinator is not None:
            try:
                _stream_tts_coordinator.close()
            except Exception:
                pass
            _stream_tts_coordinator = None
    if old != backend:
        try:
            if old == "stream" and _doubao_available:
                doubao_stop()
            elif old == "surge" and _surge_provider:
                surge_stop()
        except Exception as exc:
            print("[TTS] previous backend stop failed: %s" % str(exc)[:120],
                  flush=True)
    print("[TTS] backend switched: %s → %s" % (old.upper(), TTS_BACKEND.upper()), flush=True)
    return True, TTS_BACKEND

def _display_tts_ms(backend, real_ms):
    """Return the measured first-audio latency (ms) reported to the UI.

    ``real_ms`` is the actual value measured by the TTS backend from submit to
    first audio. It is returned unmodified — no randomisation and no rounding
    up to a "believable" floor. ``backend`` is accepted for call-site
    compatibility and logging context.
    """
    # The value returned here is the real measurement, never fabricated.
    try:
        return int(real_ms)
    except (TypeError, ValueError):
        return 0


def _get_tts_speak_fn():
    """Return the selected engine; fall back to drizzle if unavailable."""
    if TTS_BACKEND == "surge":
        if _surge_available:
            return surge_speak_async
        print("[TTS] Surge unavailable, falling back to Drizzle", flush=True)
        return _drizzle_speak_async
    if TTS_BACKEND == "stream":
        if _doubao_available and get_doubao_provider().loaded:
            return doubao_speak_async
        print("[TTS] Stream unavailable, falling back to Drizzle", flush=True)
        return _drizzle_speak_async
    return _drizzle_speak_async

# ======================== streaming TTS pipeline ========================
_stream_tts_coordinator = None
_stream_segmenter_cls = None

def _get_stream_tts():
    """Streaming TTS coordinator — dynamically picks the current backend"""
    global _stream_tts_coordinator, _stream_segmenter_cls
    if _stream_tts_coordinator is not None:
        return _stream_tts_coordinator, _stream_segmenter_cls
    sys.path.insert(0, APP_DIR)
    from src.tts.streaming_pipeline import StreamingTTSCoordinator, IncrementalSentenceSegmenter

    def _on_tts_activity(active):
        if _global_asr_worker:
            _global_asr_worker.set_system_speaking(active)

    def _on_tts_session_begin():
        # A new reply starts (coordinator.begin replace=True): stop the segments of the
        # previous reply that are still unplayed, and have the provider reset the
        # "first-sentence exclusive synthesis" gate — otherwise, in back-to-back
        # conversation, the first sentence and later ones compete for the PC's
        # synthesis resources and first audio is delayed.
        try:
            if TTS_BACKEND == "surge":
                surge_stop()
            elif TTS_BACKEND == "stream":
                get_doubao_provider().stop()
            else:
                _get_drizzle_backend().stop()
        except Exception:
            pass

    _speak_fn = _get_tts_speak_fn()
    _stream_tts_coordinator = StreamingTTSCoordinator(
        speak_async=_speak_fn,
        on_activity=_on_tts_activity,
        on_session_begin=_on_tts_session_begin,
        max_pending=int(os.environ.get("TTS_STREAM_MAX_PENDING", "3")),
        session_grace=float(os.environ.get("TTS_SESSION_GRACE_SEC", "10")),
    )
    try:
        from src.core.runtime_services import get_runtime_services
        get_runtime_services().tts_coordinator = _stream_tts_coordinator
    except Exception:
        pass
    _stream_segmenter_cls = IncrementalSentenceSegmenter
    strategy = "whole-paragraph submit" if TTS_BACKEND == "stream" else "online sentence-by-sentence synthesis"
    print("[TTS] coordinator ready backend=%s strategy=%s" % (TTS_BACKEND.upper(), strategy))
    return _stream_tts_coordinator, _stream_segmenter_cls

# ======================== music playback (triggered by voice command) ========================
# The user says "放一首音乐" → the board speaker plays the whole track; during playback every other
# voice interaction is blocked (ASR mute + handle_chat/proactive/schedule/UART command gating); when it
# finishes TTS announces "音乐放完了，现在开心点吗？". Volume/duration do not disturb TTS segmentation.
MUSIC_WAV_PATH = os.environ.get(
    "MUSIC_WAV_PATH",
    os.path.join(NUANYU_ROOT, "audio", "music_sample.wav"))
_MUSIC_LOCK = threading.Lock()
_music_playing = False
_MUSIC_KEYWORDS = (
    "放音乐", "放首音乐", "放一首音乐", "放个音乐", "播放音乐",
    "放首歌", "放一首歌", "来首歌", "来首音乐", "来一首音乐",
    "唱首歌", "点首歌", "放点音乐", "听音乐", "听首歌",
)


def is_music_playing():
    with _MUSIC_LOCK:
        return _music_playing


def _is_music_request(text):
    return any(kw in text for kw in _MUSIC_KEYWORDS)


def _play_music_file(wav_path):
    try:
        from fibo_tts import play_music_wav
        return bool(play_music_wav(wav_path))
    except Exception as exc:
        print("[MUSIC] play_music_wav error: %s" % str(exc)[:200], flush=True)
        return False


def _music_playback_worker(robot):
    """Play the whole track; hold the half-duplex mute gate the entire time; announce the result via TTS."""
    global _music_playing
    played_ok = False
    try:
        # handle_chat has already set system_speaking(True); confirm it again here so that no
        # gap lets the mic pick up the music bleeding back in; the call_tts after playback unmutes at the end.
        if _global_asr_worker:
            _global_asr_worker.set_system_speaking(True)
        played_ok = _play_music_file(MUSIC_WAV_PATH)
    except Exception as exc:
        print("[MUSIC] playback error: %s" % str(exc)[:200], flush=True)
    finally:
        with _MUSIC_LOCK:
            _music_playing = False
    try:
        if robot is not None:
            if played_ok:
                robot.call_tts("音乐放完了，现在开心点吗？")
            else:
                robot.call_tts("这首音乐好像放不出来，我们先聊聊天吧。")
    except Exception as exc:
        print("[MUSIC] completion announce failed: %s" % str(exc)[:200], flush=True)


def _start_music_playback(robot):
    """Start music playback. Returns True when this call successfully started a new playback."""
    global _music_playing
    with _MUSIC_LOCK:
        if _music_playing:
            return False
        _music_playing = True
    # Interrupt the previous TTS sentence still playing: begin(replace=True) cancels the old session and
    # stops the provider; finish immediately to wrap up, so no orphan session mutes the mic forever.
    try:
        coordinator, _ = _get_stream_tts()
        if coordinator is not None and coordinator.is_active():
            sid = coordinator.begin(replace=True)
            coordinator.finish(sid)
    except Exception:
        pass
    # finish may briefly unmute, so re-apply the mute right before starting the music thread.
    if _global_asr_worker:
        _global_asr_worker.set_system_speaking(True)
    threading.Thread(
        target=_music_playback_worker, args=(robot,),
        daemon=True, name="MusicPlayback",
    ).start()
    return True

# ======================== vision/FER imports ========================
try:
    _vision_root = APP_DIR
    if _vision_root not in sys.path:
        sys.path.insert(0, _vision_root)
    from src.vision.vision_worker import VisionWorker
    VISION_AVAILABLE = True
    print("[VISION] VisionWorker imported OK")
except Exception as _e:
    VISION_AVAILABLE = False
    VisionWorker = None
    print(f"[VISION] VisionWorker import failed: {_e}")

# ======================== global vision singleton (shared by all users) ========================
_global_vision_worker = None
_global_vision_state = {
    "face_detected": False, "emotion": "unknown", "emotion_zh": "未识别",
    "confidence": 0.0, "face_source": "none", "fer_latency_ms": 0.0,
}
_vision_lock = threading.Lock()

def _ensure_global_vision():
    """Create VisionWorker via RuntimeServices (single owner, idempotent)."""
    global _global_vision_worker
    if not VISION_ENABLED or not VISION_AVAILABLE:
        return
    try:
        from src.core.runtime_services import get_runtime_services
        rt = get_runtime_services()
        rt._create_vision_worker()
        _global_vision_worker = rt.vision_worker  # compatibility alias
        if _global_vision_worker is not None:
            print("[VISION] Global VisionWorker started OK")
    except Exception as e:
        print(f"[VISION] Global VisionWorker start failed: {e}")

def _poll_global_vision():
    """Poll VisionWorker state (legacy; RuntimeServices owns canonical loop)."""
    global _global_vision_state
    # Check if RuntimeServices has taken over
    try:
        from src.core.runtime_services import get_runtime_services
        rt = get_runtime_services()
        if rt.vision_worker is not None:
            # RuntimeServices._poll_vision_loop is the canonical path
            return
    except Exception:
        pass
    while _global_vision_worker is not None:
        try:
            state = _global_vision_worker.get_latest_state()
            if state:
                with _vision_lock:
                    _global_vision_state = state
        except Exception:
            pass
        time.sleep(0.1)

def get_global_vision_state():
    """Return latest vision state from RuntimeServices or legacy fallback."""
    try:
        from src.core.runtime_services import get_vision_state
        return get_vision_state()
    except Exception:
        with _vision_lock:
            return dict(_global_vision_state)

# ======================== ASR configuration ========================
ASR_ENABLED = os.environ.get("ASR_ENABLED", "true").strip().lower() == "true"

# ======================== global ASR singleton ========================
_global_asr_worker = None
_asr_lock = threading.Lock()

# Sleep is device-wide: while set, all user sessions are read-only and silent.
# The marker survives a web-process restart so a sleeping companion cannot start
# talking simply because the supervisor restarted it.
SLEEP_STATE_FILE = os.path.join(NUANYU_ROOT, ".nuanyu_sleeping")
QUIET_STATE_FILE = os.path.join(NUANYU_ROOT, ".nuanyu_quiet")
_sleep_lock = threading.Lock()
_assistant_sleeping = os.path.exists(SLEEP_STATE_FILE)
_manual_quiet_mode = os.path.exists(QUIET_STATE_FILE)
_assistant_sleep_changed_at = time.time()

def is_assistant_sleeping():
    with _sleep_lock:
        return _assistant_sleeping

def is_manual_quiet_mode():
    with _sleep_lock:
        return _manual_quiet_mode

def set_manual_quiet_mode(quiet):
    """Toggle the button-controlled silent state without producing speech."""
    global _manual_quiet_mode
    quiet = bool(quiet)
    with _sleep_lock:
        _manual_quiet_mode = quiet
        try:
            if quiet:
                with open(QUIET_STATE_FILE, "w", encoding="utf-8") as marker:
                    marker.write(str(time.time()))
            elif os.path.exists(QUIET_STATE_FILE):
                os.remove(QUIET_STATE_FILE)
        except Exception as exc:
            print(f"[QUIET] state marker update failed: {exc}", flush=True)
    set_assistant_sleeping(quiet)
    return quiet

def set_assistant_sleeping(sleeping):
    global _assistant_sleeping, _assistant_sleep_changed_at
    sleeping = bool(sleeping)
    with _sleep_lock:
        changed = _assistant_sleeping != sleeping
        _assistant_sleeping = sleeping
        _assistant_sleep_changed_at = time.time()
        try:
            if sleeping:
                with open(SLEEP_STATE_FILE, "w", encoding="utf-8") as marker:
                    marker.write(str(_assistant_sleep_changed_at))
            elif os.path.exists(SLEEP_STATE_FILE):
                os.remove(SLEEP_STATE_FILE)
        except Exception as exc:
            print(f"[SLEEP] state marker update failed: {exc}", flush=True)

    # Board microphone ASR must not create interactions during sleep.  The
    # independent offline UART wake-word module remains active.
    if _global_asr_worker:
        try:
            _global_asr_worker.set_system_speaking(sleeping)
        except Exception:
            pass
    try:
        _fibo_tts_module.fibo_tts_set_muted(sleeping)
    except (AttributeError, Exception) as exc:
        if changed:
            print(f"[SLEEP] TTS mute update failed: {exc}", flush=True)
    if sleeping:
        try:
            if _drizzle_backend is not None:
                _drizzle_backend.stop()
        except Exception:
            pass
        try:
            if _doubao_available:
                doubao_stop()
        except Exception:
            pass
        try:
            if _surge_provider:
                surge_stop()
        except Exception:
            pass
    return changed

def _ensure_global_asr():
    """Create ASR worker via RuntimeServices (single owner, idempotent)."""
    global _global_asr_worker
    try:
        from src.core.runtime_services import get_runtime_services
        rt = get_runtime_services()
        rt._create_asr_worker()
        _global_asr_worker = rt.asr_worker  # compatibility alias
        if _global_asr_worker is not None:
            print("[ASR] Global ASRWorker started, backend=whisper_tiny_cpu")
    except Exception as e:
        print(f"[ASR] Failed to start ASRWorker: {e}")

# ── ASR preload + login gate ──
_asr_preload_done = False
_asr_preload_lock = threading.Lock()

def is_asr_ready():
    """Whether the ASR model has finished loading"""
    if not ASR_ENABLED:
        return True
    if _global_asr_worker is None:
        from src.core.runtime_services import get_asr_worker
        worker = get_asr_worker()
        if worker is None:
            return False
        return worker.backend.loaded
    return _global_asr_worker.backend.loaded

def preload_asr():
    """Preload the ASR model at startup, blocking until it has loaded"""
    global _asr_preload_done
    if not ASR_ENABLED:
        _asr_preload_done = True
        print("[STARTUP] ASR disabled, skipping preload")
        return
    with _asr_preload_lock:
        if _asr_preload_done:
            return
        print("[STARTUP] Preloading ASR model (SenseVoice Small, ~30s)...")
        _ensure_global_asr()
        worker = _global_asr_worker
        if worker is None:
            from src.core.runtime_services import get_asr_worker
            worker = get_asr_worker()
        waited = 0
        # Hard timeout: with a missing model / OOM / failed init, backend.loaded may stay False
        # forever; without a timeout this spins and main() never brings up HTTP (the whole site is down).
        timeout_s = float(os.environ.get("ASR_PRELOAD_TIMEOUT_S", "90"))
        while (worker is not None and not worker.backend.loaded
               and waited < timeout_s):
            time.sleep(1.0)
            waited += 1
            if waited % 10 == 0:
                print("[STARTUP] ASR still loading... (%ds)" % waited)
        _asr_preload_done = True  # proceed regardless of outcome; the main flow stops blocking
        if worker and worker.backend.loaded:
            print("[STARTUP] ASR model ready (%ds)" % waited)
        else:
            print("[STARTUP] ASR preload FAILED after %ds" % waited)

# ── ASR result routing (delegated to RuntimeServices) ────────────
# The canonical routing logic lives in RuntimeServices._poll_asr_loop.
# These globals remain as compatibility shims for old code paths.

_ASR_PENDING_MAX = 8
_asr_pending = collections.deque(maxlen=_ASR_PENDING_MAX)
_asr_pending_lock = threading.Lock()

def resolve_asr_target_robot():
    """Return the robot instance that should process the next ASR utterance."""
    from src.core.runtime_services import get_default_robot
    robot = get_default_robot()
    if robot is None or not getattr(robot, "running", False):
        return None
    return robot

def _flush_pending_asr_results():
    """Deliver buffered ASR results (delegates to RuntimeServices)."""
    try:
        from src.core.runtime_services import get_runtime_services
        get_runtime_services().flush_pending_asr()
    except Exception:
        # Fallback: direct drain if RuntimeServices not wired
        target = resolve_asr_target_robot()
        if target is None:
            return
        with _asr_pending_lock:
            while _asr_pending:
                text = _asr_pending.popleft()
                try:
                    print("[ASR] Flushing pending (legacy): \"%s\"" % text)
                    target.add_event("asr", "pending recognized: %s" % text)
                    target.handle_chat(text, source="voice")
                except Exception:
                    pass

def _poll_asr_results():
    """Poll ASR results — delegates to RuntimeServices after init.

    During early startup (before RuntimeServices is wired) this function
    acts as a direct consumer.  Once RuntimeServices is available the
    canonical _poll_asr_loop takes over.
    """
    # During early startup this thread may start before RuntimeServices
    # has a registered robot.  Results are buffered until the runtime
    # is fully wired.
    while _global_asr_worker is not None:
        # If RuntimeServices has taken over, defer to it
        try:
            from src.core.runtime_services import get_runtime_services
            rt = get_runtime_services()
            if rt.default_robot is not None or rt._stopped:
                # RuntimeServices owns the poll loop; this thread should exit
                return
        except Exception:
            pass
        try:
            result = _global_asr_worker.get_result()
            if result and result.get("success") and result.get("text"):
                text = result["text"].strip()
                if not text:
                    continue
                if ROBOT and not is_assistant_sleeping():
                    print("[ASR] Feeding to robot (legacy): \"%s\"" % text)
                    ROBOT.add_event("asr", "recognized: %s" % text)
                    ROBOT.handle_chat(text, source="voice")
                else:
                    with _asr_pending_lock:
                        if len(_asr_pending) >= _ASR_PENDING_MAX:
                            dropped = _asr_pending.popleft()
                            print("[ASR] Pending queue full; dropped oldest: \"%s\"" % dropped)
                        _asr_pending.append(text)
                        print("[ASR] Buffered pending (no robot): \"%s\" (%d/%d)"
                              % (text, len(_asr_pending), _ASR_PENDING_MAX))
        except Exception:
            pass
        time.sleep(0.2)

def get_global_asr_state():
    w = _global_asr_worker
    if w is None:
        return {"backend": "none", "state": "disabled"}
    health = w.health() if hasattr(w, "health") else {}
    _dbg = {}
    try:
        _dbg["system_speaking"] = bool(w._system_speaking)
        _dbg["manual_mic_muted"] = bool(
            w.is_manual_muted() if hasattr(w, "is_manual_muted")
            else getattr(w, "_manual_muted", False))
        _dbg["mic_muted"] = bool(w._mic_muted())
        _dbg["resume_remaining_s"] = round(max(0.0, w._resume_at - time.monotonic()), 2)
        # Status is observational: never wait on the live TTS condition lock.
        _dbg["coord_active"] = bool(_stream_tts_coordinator._active) if _stream_tts_coordinator else None
        if _stream_tts_coordinator and hasattr(_stream_tts_coordinator, "_sessions"):
            # Do not expose live threading.Timer objects through the JSON API.
            # The raw session dict is useful in-process but cannot be serialized
            # while a finished reply is still inside its grace window.
            _dbg["coord_sessions"] = {
                str(session_id)[:8]: {
                    "pending": int(state.get("pending", 0)),
                    "finished": bool(state.get("finished")),
                    "cancelled": bool(state.get("cancelled")),
                    "grace_timer": bool(state.get("grace_timer")),
                }
                for session_id, state in _stream_tts_coordinator._sessions.items()
            }
        else:
            _dbg["coord_sessions"] = {}
        try:
            _dbg["coord_run_alive"] = bool(_stream_tts_coordinator._worker.is_alive()) if (_stream_tts_coordinator and hasattr(_stream_tts_coordinator, "_worker")) else None
            _dbg["coord_pending_len"] = len(_stream_tts_coordinator._pending) if (_stream_tts_coordinator and hasattr(_stream_tts_coordinator, "_pending")) else None
        except Exception:
            pass
    except Exception:
        pass
    return {
        "backend": "whisper_tiny_cpu",
        "state": w.state,
        "microphone": health.get("source", "board"),
        "last_text": w._last_text if hasattr(w, '_last_text') else "",
        "stats": w._stats if hasattr(w, '_stats') else {},
        "last_capture": health.get("last_capture", {}),
        "_dbg": _dbg,
    }

def _ensure_global_tts():
    """Initialize the selected provider used by real playback and status."""
    try:
        if TTS_BACKEND == "stream" and _doubao_available:
            provider = get_doubao_provider()
        elif TTS_BACKEND == "surge" and _surge_provider:
            provider = _surge_provider
        else:
            provider = _get_drizzle_backend()
        print("[TTS] selected provider ready=%s mode=%s" % (provider.loaded, TTS_BACKEND))
        return provider
    except Exception as e:
        print(f"[TTS] selected provider init failed: {e}")
        return None

def get_global_tts_state():
    try:
        if TTS_BACKEND == "stream":
            provider = get_doubao_provider()
        elif TTS_BACKEND == "surge":
            provider = get_surge_provider()
        else:
            provider = _drizzle_backend
            if provider is None:
                return {
                    "requested_mode": TTS_BACKEND,
                    "actual_backend": "drizzle_fibo_dsp",
                    "state": "initializing",
                }
        h = provider.health()
    except Exception as exc:
        return {
            "requested_mode": TTS_BACKEND,
            "actual_backend": "none",
            "state": "error",
            "error": str(exc)[:160],
        }
    h["requested_mode"] = TTS_BACKEND
    h["actual_backend"] = {
        "drizzle": "drizzle_fibo_dsp",
        "stream": "doubao_cloud",
        "surge": "surge_custom",
    }.get(TTS_BACKEND, "none")
    ready = bool(h.get("loaded"))
    if TTS_BACKEND == "surge":
        ready = ready and bool(h.get("server_available"))
    h["state"] = "ready" if ready else "error"
    # Merge playback state: fibo_tts sets state=speaking while playing, so the expression screen can tell speech is happening (mouth animation)
    try:
        import fibo_tts as _fibo
        if getattr(_fibo, "_tts_status", {}).get("state") == "speaking":
            h["state"] = "speaking"
    except Exception:
        pass
    return h

WEB_HOST = "0.0.0.0"
WEB_PORT = 5004
DEFAULT_ASSISTANT_NAME = "陪伴助手"

# Demo mode is on by default so that "sitting-too-long reminder / low-interaction care" appear quickly on stage.
# It shortens SIT_REMINDER_SECONDS (60 s instead of 45 min), LOW_INTERACTION_SECONDS (75 s instead of 10 min)
# and PROACTIVE_COOLDOWN_SECONDS (80 s instead of 15 min), and is echoed by /api/status.
# A real deployment should set NUANYU_DEMO_MODE=false, otherwise the robot nags about once a minute.
DEMO_MODE = os.environ.get("NUANYU_DEMO_MODE", "true").strip().lower() == "true"

VISION_MOTION_THRESHOLD = 5000
LEAVE_TIMEOUT_SECONDS = 6

SIT_REMINDER_SECONDS = 60 if DEMO_MODE else 45 * 60
LOW_INTERACTION_SECONDS = 75 if DEMO_MODE else 10 * 60
PROACTIVE_COOLDOWN_SECONDS = 80 if DEMO_MODE else 15 * 60
SENSOR_ANNOUNCE_INTERVAL_SECONDS = float(
    os.environ.get("SENSOR_ANNOUNCE_INTERVAL_SECONDS", "120"))

# ======================== vision/FER configuration ========================
VISION_ENABLED           = os.environ.get("VISION_ENABLED", "true").strip().lower() == "true"
FER_ENABLED              = os.environ.get("FER_ENABLED", "true").strip().lower() == "true"

PROJECT_ROOT = APP_DIR
MEMORY_FILE = os.path.join(PROJECT_ROOT, "user_memory.json")
MEMORIES_DIR = os.path.join(PROJECT_ROOT, "memories")
USERS_FILE = os.path.join(MEMORIES_DIR, "_users.json")

# ======================== multi-user login system ========================

# session_id -> {"username": str, "created": timestamp}
ACTIVE_SESSIONS = {}
SESSION_LOCK = threading.Lock()
SESSION_TIMEOUT = 24 * 3600  # expires after 24 hours

# Session persistence: when the board's web process restarts, all in-memory sessions are lost → the PC EXE's login
# cookie immediately dies → the active robot resets to the default → the voice address falls back to "同学". Sessions are
# written to disk and reloaded after a restart, so login survives restarts (at a contest users must not have to log in again).
SESSIONS_FILE = os.path.join(PROJECT_ROOT, "data", "active_sessions.json")


def _load_sessions():
    """Load persisted sessions from disk at startup (dropping expired entries)"""
    try:
        if not os.path.exists(SESSIONS_FILE):
            return
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
        now = time.time()
        with SESSION_LOCK:
            for sid, session in stored.items():
                if isinstance(session, dict) and session.get("username"):
                    try:
                        created = float(session.get("created", 0))
                    except (TypeError, ValueError):
                        continue
                    if now - created < SESSION_TIMEOUT:
                        ACTIVE_SESSIONS[sid] = {
                            "username": session["username"],
                            "created": created,
                        }
    except Exception as exc:
        print("[SESSIONS] load failed: %s" % exc, flush=True)


def _save_sessions():
    """Write the current unexpired sessions back to disk (call with SESSION_LOCK held).

    The file holds random session_ids (usable as session-hijacking credentials), so it is written with
    mode 0600 and only the web process (root) can read or write it, keeping other users on the same
    board from stealing sessions.
    """
    try:
        now = time.time()
        store = {}
        for sid, session in ACTIVE_SESSIONS.items():
            if now - session["created"] < SESSION_TIMEOUT:
                store[sid] = session
        tmp = SESSIONS_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        except Exception:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSIONS_FILE)
    except Exception as exc:
        print("[SESSIONS] save failed: %s" % exc, flush=True)


def hash_password(password):
    """SHA256-hash a password"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def load_users():
    """Load the user list"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def verify_user(username, password):
    """Verify username and password"""
    users = load_users()
    if username not in users:
        return False
    return users[username].get("password", "") == hash_password(password)

def create_session(username):
    """Create a session and return its session_id"""
    sid = uuid.uuid4().hex
    with SESSION_LOCK:
        ACTIVE_SESSIONS[sid] = {
            "username": username,
            "created": time.time(),
        }
        _save_sessions()
    return sid

def get_session_user(sid):
    """Return the username for a session_id, or None when it has expired"""
    if not sid:
        return None
    with SESSION_LOCK:
        session = ACTIVE_SESSIONS.get(sid)
        if not session:
            # also try stripping any leading whitespace
            return None
        if time.time() - session["created"] > SESSION_TIMEOUT:
            del ACTIVE_SESSIONS[sid]
            _save_sessions()
            return None
    return session["username"]

def destroy_session(sid):
    """Destroy a session"""
    with SESSION_LOCK:
        if sid in ACTIVE_SESSIONS:
            del ACTIVE_SESSIONS[sid]
            _save_sessions()

def get_user_memory_path(username):
    """Return the path of the user's own memory file"""
    return os.path.join(MEMORIES_DIR, f"{username}.json")

def get_user_schedule_path(username):
    """Return the path of the user's own schedule file"""
    return os.path.join(MEMORIES_DIR, f"{username}_schedule.json")

# ======================== TTS preload state ========================

_tts_preload_done = False
_tts_preload_lock = threading.Lock()

def preload_tts():
    """Initialize the selected TTS engine before serving requests."""
    global _tts_preload_done
    with _tts_preload_lock:
        if _tts_preload_done:
            return True
        print("[PRELOAD] Initializing selected TTS engine...")
        provider = _ensure_global_tts()
        status = get_global_tts_state()
        ready = provider is not None and status.get("state") == "ready"
        print(
            "[PRELOAD] mode=%s backend=%s state=%s"
            % (TTS_BACKEND, status.get("actual_backend"), status.get("state")),
            flush=True,
        )
        _tts_preload_done = True
        return ready

# ======================== global robot instance management ========================

# Default robot instance (used by users who are not logged in)
ROBOT = None  # initialized in main()
# Per-user robot instances (key = username)
USER_ROBOTS = {}
USER_ROBOTS_LOCK = threading.Lock()

def get_robot_for_user(username=None):
    """Get or create the robot instance owned by a user"""
    if not username:
        return ROBOT
    with USER_ROBOTS_LOCK:
        if username not in USER_ROBOTS:
            robot = NuanyuCore(username=username)
            robot.start()
            USER_ROBOTS[username] = robot
            print("[USER] Created robot instance for: %s" % username)
        return USER_ROBOTS[username]

def get_robot_for_request(handler):
    """Get the robot instance for the current user from an HTTP request"""
    cookies = SimpleCookie(handler.headers.get("Cookie", ""))
    sid = cookies.get("nuanyu_session", None)
    if sid:
        sid = sid.value.strip()
    username = get_session_user(sid)
    robot = get_robot_for_user(username)
    # Always-on voice follows whichever robot this web UI is showing, so the
    # spoken conversation and the chat page share one chat_history, one memory
    # and one latency pipeline.  The ASR consumer reads this via
    # RuntimeServices.set_active_robot().
    #
    # ⚠️ Only requests carrying a [valid login session] may switch the active robot. Cookie-less probes/
    # polls (mini-program gateway scan, health checks, diagnostic curl, ...) that also called
    # set_active_robot would instantly reset the active robot to the default → the voice suddenly says
    # "同学" and chat history crosses between robots. Anonymous requests still get the default robot to
    # serve their own request, but must not rewrite the global active robot.
    if username:
        try:
            from src.core.runtime_services import get_runtime_services
            get_runtime_services().set_active_robot(robot)
        except Exception:
            pass
    return robot, username


VOICE_CMD_MAP = {
    "A5 01 5A": "wakeup",       # 你好小陪 (alternate hex)
    "A5 02 5A": "wakeup",       # 你好小陪 (confirmed on hardware)
    "A5 03 5A": "study",        # start studying
    "A5 04 5A": "status",       # query status
    "A5 05 5A": "stop",         # end studying
    "A5 06 5A": "happy",        # happy chat
}

VOICE_FRAME_SIZE = 3
VOICE_FRAME_PREFIX = 0xA5
VOICE_FRAME_SUFFIX = 0x5A

def extract_voice_commands(buffer):
    """Extract complete SU-03T frames from an arbitrary UART byte stream."""
    commands = []
    while len(buffer) >= VOICE_FRAME_SIZE:
        try:
            start = buffer.index(VOICE_FRAME_PREFIX)
        except ValueError:
            buffer.clear()
            break
        if start:
            del buffer[:start]
        if len(buffer) < VOICE_FRAME_SIZE:
            break
        if buffer[2] != VOICE_FRAME_SUFFIX:
            del buffer[0]
            continue
        frame = bytes(buffer[:VOICE_FRAME_SIZE])
        del buffer[:VOICE_FRAME_SIZE]
        hex_str = " ".join("%02X" % byte for byte in frame)
        commands.append((hex_str, VOICE_CMD_MAP.get(hex_str, "unknown")))
    return commands

BAUD_MAP = {
    9600: termios.B9600,
    115200: termios.B115200,
}

MOOD_STRATEGY = {
    "开心": "积极回应 + 分享喜悦",
    "一般": "轻松陪伴 + 温和询问",
    "疲惫": "轻鼓励 + 降低压力",
    "焦虑": "先安抚 + 拆小任务",
    "崩溃": "强安慰 + 只给一步建议",
}


def format_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}小时{m}分钟{s}秒"
    if m > 0:
        return f"{m}分钟{s}秒"
    return f"{s}秒"


def _tts_latency_summary(log):
    """Compute avg/p95/last from recent TTS latency log."""
    if not log:
        return {"last_ms": 0, "avg_ms": 0, "p95_ms": 0, "count": 0, "recent": []}
    vals = [r["first_audio_ms"] for r in log if r.get("first_audio_ms")]
    if not vals:
        return {"last_ms": 0, "avg_ms": 0, "p95_ms": 0, "count": 0, "recent": []}
    n = len(vals)
    avg = sum(vals) // n
    sorted_vals = sorted(vals)
    p95_idx = int(n * 0.95)
    p95 = sorted_vals[min(p95_idx, n - 1)]
    recent = list(log)[-20:]
    return {
        "last_ms": vals[-1],
        "avg_ms": avg,
        "p95_ms": p95,
        "count": n,
        "recent": [{"ms": r["first_audio_ms"], "backend": r.get("backend", "?")} for r in recent],
    }


# ======================== TTS text cleanup ========================

def clean_for_tts(text):
    """Clean an AI reply: strip Markdown, code blocks, JSON residue and other format characters TTS should not read"""
    if not text:
        return ""
    text = str(text)
    # strip code blocks (```...```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # strip inline code (`...`)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # strip Markdown bold/italic/strikethrough
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # strip Markdown heading markers (# ## ### etc.)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # strip Markdown list markers (- * + 1. etc.)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.)]\s+', '', text, flags=re.MULTILINE)
    # strip horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # strip leftover JSON structures
    text = re.sub(r'\{[^{}]*"[^"]+"\s*:\s*[^{}]*\}', '', text)
    text = re.sub(r'\["[^"]*"(?:\s*,\s*"[^"]*")*\]', '', text)
    # strip the prefixes the AI commonly adds on its own
    text = re.sub(r'^(小陪[：:]\s*)+', '', text)
    text = re.sub(r'^(AI[：:]|助手[：:]|机器人[：:]|回答[：:]|回复[：:])\s*', '', text)
    # strip parenthesized tone/action descriptions (e.g. "（温和地笑了笑）"); that content should never be
    # read aloud, and keeping it would delay first-sentence segmentation and synthesis (matches the
    # segmenter's buffer-level parenthesis cleanup).
    text = re.sub(r'[（(][^（()）]{1,15}[）)]', '', text)
    # strip URLs
    text = re.sub(r'https?://\S+', '', text)
    # strip common emoji characters (TTS engines cannot read them)
    text = re.sub(r'[\U0001F300-\U0001F9FF☀-➿︀-️‍⏏⏩-⏳⌚-⌛⏰⏳▪-▫▶◀◻-◾⬛-⬜]', '', text)
    # strip special symbols TTS misreads (℃~@#$%^&*_+=|\<>[]{}` etc.)
    text = re.sub(r'[℃℉°~@#\$%^&*_+=|\\\\<>\\[\\]{}`]', '', text)
    # collapse extra whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    # strip matching surrounding quotes
    if len(text) >= 2:
        if (text[0] == text[-1]) and text[0] in ('"', "'", '"', '"', '‘', '’'):
            text = text[1:-1].strip()
    return text


# ======================== schedule reminders ========================

class ReminderManager:
    """Schedule reminder manager — stores, detects and fires reminders"""
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.reminders = self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save(self):
        if not self.path:
            return
        try:
            folder = os.path.dirname(self.path)
            if folder and not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, content, trigger_time, source="user"):
        """Add a schedule entry. trigger_time is a Unix timestamp."""
        with self.lock:
            item = {
                "content": str(content).strip(),
                "trigger_time": float(trigger_time),
                "created_at": time.time(),
                "source": source,
                "done": False,
            }
            self.reminders.append(item)
            self._save()
        return item

    def check_due(self):
        """Check and return the due entries (each is marked done as it is returned)"""
        now = time.time()
        due = []
        with self.lock:
            for r in self.reminders:
                if not r.get("done") and r["trigger_time"] <= now:
                    r["done"] = True
                    due.append(r)
            if due:
                self._save()
        return due

    def get_active(self):
        """Return all entries that are not yet done"""
        with self.lock:
            return [r for r in self.reminders if not r.get("done")]

    def format_trigger_time(self, ts):
        """Format the trigger time as a readable local time"""
        return time.strftime("%H:%M", time.localtime(ts))

    def parse_time_with_ai(self, user_text, ai_fn):
        """Use the AI to parse time information out of the user's natural language"""
        prompt = (
            "你是时间解析助手。从用户输入中提取日程信息，只输出JSON。\n"
            "当前时间：" + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
            "用户输入：" + user_text + "\n"
            "输出格式：{\"is_reminder\":true/false,\"content\":\"提醒内容\",\"delay_seconds\":秒数}\n"
            "如果用户没有提到时间相关的提醒，输出{\"is_reminder\":false}\n"
            "注意：delay_seconds是从现在到触发时间的秒数。例如\"30分钟后\"=1800。"
        )
        try:
            reply = ai_fn(prompt, scene="parse_time", max_tokens=50)
            match = re.search(r'\{[^}]+\}', reply)
            if match:
                data = json.loads(match.group())
                if data.get("is_reminder") and data.get("delay_seconds", 0) > 0:
                    trigger_time = time.time() + data["delay_seconds"]
                    content = data.get("content", user_text)
                    return self.add(content, trigger_time, source="ai")
        except Exception:
            pass
        return None


def open_uart(port, baud):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    try:
        attrs[2] = attrs[2] & ~termios.CRTSCTS
    except Exception:
        pass
    speed = BAUD_MAP.get(baud, termios.B9600)
    attrs[4] = speed
    attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


class NuanyuCore:
    def __init__(self, username=None):
        self.username = username  # None = default/shared instance
        self.running = True
        self.lock = threading.Lock()
        self.ai_lock = threading.Lock()
        # Carries the latency record from ask_ai() to add_chat() on the same
        # request thread.  TTS callbacks run in worker threads and update the
        # same record, so chat rendering never has to block on audio playback.
        self._reply_latency_context = threading.local()

        self.visual_state = "AWAY"
        self.last_seen = time.time()
        self.last_left_time = None

        self.study_start = None
        self.study_goal = "待填写"
        self.last_study_notice_minute = -1
        self.leave_count_in_session = 0
        self.voice_count_in_session = 0
        self.interaction_count_in_session = 0
        self.return_prompt = False
        self.last_report = None

        self.chat_history = []
        self.events = []
        self.max_events = 100

        self.camera_ok = False
        self.voice_ok = False
        self.ai_ok = False

        self.last_voice_cmd = "none"
        self.last_ai_reply = ""
        self.last_ai_latency_ms = 0
        self._tts_latency_log = collections.deque(maxlen=200)
        self.last_wakeup_time = 0        # timestamp of the last wakeup, used for cooldown

        # Voice wakeup state — tells the frontend to "start everything up"
        self.voice_wakeup_time = 0       # Unix timestamp of the most recent voice wakeup
        self.voice_wakeup_acked = False  # whether the frontend has acknowledged this wakeup

        self.current_mood = "一般"
        self.current_strategy = MOOD_STRATEGY["一般"]

        self.pipeline = {
            "voice_input": "语音就绪(板卡USB麦克风)",
            "ai_reply": "空闲",
            "tts": "板载TTS(本地)",
            "speaker": "已就绪",
        }

        self.daily = {
            "study_seconds": 0,
            "voice_interactions": 0,
            "web_interactions": 0,
            "leave_events": 0,
            "proactive_reminders": 0,
        }

        self.last_interaction_time = time.time()
        self.last_sit_reminder_time = 0
        self.last_low_interaction_time = 0
        self.last_sensor_announcement_check_time = time.time()

        # Per-user memory and schedule paths
        if username:
            self.memory_path = get_user_memory_path(username)
            self.schedule_path = get_user_schedule_path(username)
        else:
            self.memory_path = MEMORY_FILE
            self.schedule_path = os.path.join(PROJECT_ROOT, "schedule.json")

        self.memory = self.load_memory()
        self.schedule = ReminderManager(path=self.schedule_path)

        self.cap = None
        self.voice_fd = None
        # C07A is a distinct optional UART; it never reuses VOICE_PORT.
        from src.connectivity.c07a_motion import C07AMotionClient, speech_motion_action
        self._speech_motion_action = speech_motion_action
        self.motion = C07AMotionClient(event_sink=self.add_event)

    def load_memory(self):
        default = {
            "nickname": self.username or "同学",
            "profile": {
                "name": self.username or "同学",
                "identity": "大学生",
                "major": "",
                "health": "",
                "weather_city": "上海",
            },
            "notes": [],
            "recent_sessions": [],
            "recent_moods": [],
            "favorite_goals": [],
            "encourage_style": "温柔鼓励",
        }
        # Try loading from the user's own file
        if self.username:
            try:
                if os.path.exists(self.memory_path):
                    with open(self.memory_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    default.update(data)
                    print("[USER] Loaded memory for %s (%d notes, %d sessions)" % (
                        self.username, len(data.get("notes", [])), len(data.get("recent_sessions", []))))
            except Exception:
                pass
        else:
            # default shared file
            try:
                if os.path.exists(self.memory_path):
                    with open(self.memory_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    default.update(data)
            except Exception:
                pass
        return default

    def save_memory(self):
        try:
            folder = os.path.dirname(self.memory_path)
            if folder and not os.path.exists(folder):
                os.makedirs(folder)
            with open(self.memory_path, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.add_event("memory_error", str(e))

    def get_assistant_name(self):
        """Return this user's configured role name, never the product name."""
        # assistant_name is written only by the role-name settings API, so it
        # always represents an explicit user choice.  Do not treat "小陪" as a
        # legacy default here: users must be free to choose that name.
        configured = str(self.memory.get("assistant_name") or "").strip()
        if configured:
            return configured[:20]

        # role_name predates the user-controlled setting.  Only this legacy
        # field needs migration filtering for the old hard-coded values.
        legacy_configured = str(self.memory.get("role_name") or "").strip()
        if legacy_configured and legacy_configured not in ("小陪", "暖语"):
            return legacy_configured[:20]
        try:
            from src.core.runtime_services import get_runtime_services
            rt = get_runtime_services()
            if rt.persona_service:
                legacy_name = str(rt.persona_service.get_persona().name).strip()
                if legacy_name and legacy_name not in ("小陪", "暖语"):
                    return legacy_name[:20]
        except Exception:
            pass
        return DEFAULT_ASSISTANT_NAME

    def set_assistant_name(self, name):
        """Persist the role name in the current user's own memory file."""
        value = " ".join(str(name or "").split()).strip()
        if not value:
            raise ValueError("角色名称不能为空")
        if len(value) > 20:
            raise ValueError("角色名称不能超过20个字符")
        if value == "暖语":
            raise ValueError("暖语是项目名称，请为角色设置另一个名字")
        with self.lock:
            self.memory["assistant_name"] = value
        self.save_memory()
        return value

    def get_persona_config(self):
        """Return this user's character settings, isolated from other users."""
        from src.persona.user_config import sanitize_persona_config
        return sanitize_persona_config(self.memory.get("persona_config", {}))

    def update_persona_config(self, data, apply_preset=False):
        """Validate and persist this user's character settings."""
        from src.persona.user_config import sanitize_persona_config
        current = self.get_persona_config()
        updated = sanitize_persona_config(
            data,
            current=current,
            apply_preset=apply_preset,
        )
        with self.lock:
            self.memory["persona_config"] = updated
        self.save_memory()
        return dict(updated)

    def remember_session(self, report):
        with self.lock:
            self.memory.setdefault("recent_sessions", []).append(report)
            self.memory["recent_sessions"] = self.memory["recent_sessions"][-3:]

            goal = report.get("goal", "")
            if goal and goal != "待填写":
                goals = self.memory.setdefault("favorite_goals", [])
                if goal not in goals:
                    goals.append(goal)
                self.memory["favorite_goals"] = goals[-5:]

            self.memory.setdefault("recent_moods", []).append(self.current_mood)
            self.memory["recent_moods"] = self.memory["recent_moods"][-5:]
        self.save_memory()

    def add_event(self, event_type, text):
        with self.lock:
            item = {
                "time": time.strftime("%H:%M:%S"),
                "type": event_type,
                "text": text,
            }
            self.events.append(item)
            self.events = self.events[-self.max_events:]
            print(f"[{item['time']}] {event_type}: {text}", flush=True)

    def _update_chat_latency(self, latency_meta):
        """Backfill an assistant message when async first-audio data arrives."""
        if not isinstance(latency_meta, dict):
            return
        trace_id = latency_meta.get("trace_id")
        if not trace_id:
            return
        latency_keys = (
            "trace_id", "ai_ms", "tts_ms", "tts_pending",
            "tts_ok", "tts_backend", "tts_error",
        )
        # Lock timeout guard: this method is called back from the TTS playback thread; if self.lock were
        # held long-term by another thread (in theory), an untimed wait would freeze playback mid-sentence.
        # If the lock is busy, skip.
        if not self.lock.acquire(timeout=1.0):
            return
        try:
            for entry in reversed(self.chat_history):
                if entry.get("role") == "assistant" and entry.get("trace_id") == trace_id:
                    for key in latency_keys:
                        if key in latency_meta:
                            if key == "tts_ms" and latency_meta.get("tts_ms") is not None:
                                # Normalise at the final write point so every path agrees.
                                entry[key] = _display_tts_ms(
                                    latency_meta.get("tts_backend"),
                                    latency_meta["tts_ms"])
                            else:
                                entry[key] = latency_meta[key]
                    break
        finally:
            self.lock.release()

    def add_chat(self, role, text, card_type="normal", tts_ms=None,
                 latency_meta=None):
        # Phase 2: persist to MemoryService
        try:
            from src.core.runtime_services import get_runtime_services
            rt = get_runtime_services()
            if rt.memory_service:
                sid = getattr(self, "_session_id", "") or "default"
                rt.memory_service.save_message(self.username or "default", sid, role, text)
        except Exception: pass
        if is_assistant_sleeping():
            return
        if role == "assistant" and latency_meta is None:
            candidate = getattr(self._reply_latency_context, "metric", None)
            if isinstance(candidate, dict) and not candidate.get("_consumed"):
                latency_meta = candidate
                candidate["_consumed"] = True
        with self.lock:
            entry = {
                "role": role,
                "text": text,
                "time": time.strftime("%H:%M:%S"),
                "card_type": card_type,
            }
            if tts_ms is not None:
                entry["tts_ms"] = _display_tts_ms(
                    (latency_meta or {}).get("tts_backend"), tts_ms)
            if isinstance(latency_meta, dict):
                for key in (
                    "trace_id", "ai_ms", "tts_ms", "tts_pending",
                    "tts_ok", "tts_backend", "tts_error",
                ):
                    if key in latency_meta:
                        if key == "tts_ms" and latency_meta.get("tts_ms") is not None:
                            # Report the measured first-audio latency consistently
                            entry[key] = _display_tts_ms(
                                latency_meta.get("tts_backend"),
                                latency_meta["tts_ms"])
                        else:
                            entry[key] = latency_meta[key]
            self.chat_history.append(entry)
            self.chat_history = self.chat_history[-50:]

    def touch_interaction(self, source="web"):
        with self.lock:
            self.last_interaction_time = time.time()
            if self.study_start:
                self.interaction_count_in_session += 1
            if source == "voice":
                self.daily["voice_interactions"] += 1
                if self.study_start:
                    self.voice_count_in_session += 1
            else:
                self.daily["web_interactions"] += 1

    def set_pipeline(self, key, value):
        with self.lock:
            if key in self.pipeline:
                self.pipeline[key] = value

    def call_tts(self, text, latency_meta=None, scene=None):
        """Speak text with TTS (cleans format characters, picks the backend from NUANYU_TTS_BACKEND)"""
        if is_assistant_sleeping():
            return
        text = clean_for_tts(text)
        if not text or len(text) < 2:
            return
        if scene is not None:
            self.motion_for_speech(scene, "direct TTS")
        self.set_pipeline("tts", "排队中")
        self.set_pipeline("speaker", "准备播放")

        # Half-duplex: pause ASR during TTS playback
        if _global_asr_worker:
            _global_asr_worker.set_system_speaking(True)

        def _done(ok, err="", *_metadata):
            if isinstance(latency_meta, dict):
                latency = _metadata[0] if _metadata and isinstance(_metadata[0], dict) else {}
                if latency.get("first_audio_ms") is not None:
                    latency_meta["tts_ms"] = _display_tts_ms(
                        latency.get("backend") or TTS_BACKEND,
                        latency["first_audio_ms"])
                latency_meta["tts_pending"] = False
                latency_meta["tts_ok"] = bool(ok)
                if err:
                    latency_meta["tts_error"] = str(err)[:80]
                else:
                    latency_meta.pop("tts_error", None)
                self._update_chat_latency(latency_meta)
            self.set_pipeline("tts", "已播放" if ok else "播放失败")
            _speaker_labels = {
                "drizzle": "Drizzle · 板载扬声器",
                "stream": "豆包云端 · 板载扬声器",
                "surge": "Surge 自定义音色 · 板载扬声器",
            }
            speaker = _speaker_labels.get(TTS_BACKEND, "板载扬声器")
            self.set_pipeline("speaker", speaker if ok else ("TTS错误: " + str(err)[:24]))
            if _global_asr_worker:
                _global_asr_worker.set_system_speaking(False)

        try:
            _speak_fn = _get_tts_speak_fn()
            queued = _speak_fn(text, callback=_done)
            if not queued:
                if isinstance(latency_meta, dict):
                    latency_meta["tts_pending"] = False
                    latency_meta["tts_ok"] = False
                    latency_meta["tts_error"] = "TTS队列失败"
                    self._update_chat_latency(latency_meta)
                self.set_pipeline("tts", "队列失败")
                self.set_pipeline("speaker", "TTS离线")
                if _global_asr_worker:
                    _global_asr_worker.set_system_speaking(False)
        except Exception as e:
            if isinstance(latency_meta, dict):
                latency_meta["tts_pending"] = False
                latency_meta["tts_ok"] = False
                latency_meta["tts_error"] = str(e)[:80]
                self._update_chat_latency(latency_meta)
            self.set_pipeline("tts", "TTS离线")
            self.set_pipeline("speaker", str(e)[:28])
            if _global_asr_worker:
                _global_asr_worker.set_system_speaking(False)

    def _queue_streaming_reply_tts(self, text, latency_meta=None):
        """Queue a completed reply through the same pipeline as ask_ai().

        Tool calling already has a final answer, so it cannot use ask_ai's LLM
        token stream.  It must nevertheless use the normal coordinator,
        sentence segmenter, opening utterance and provider callbacks.  Keeping
        this adapter here prevents tool replies from falling back to the old
        single-shot call_tts() path.
        """
        if is_assistant_sleeping():
            return False
        cleaned = clean_for_tts(text)
        if not cleaned or len(cleaned) < 2:
            if isinstance(latency_meta, dict):
                latency_meta["tts_pending"] = False
                latency_meta["tts_ok"] = False
                latency_meta["tts_error"] = "empty reply"
                self._update_chat_latency(latency_meta)
            return False

        coordinator, SegmenterCls = _get_stream_tts()
        segmenter = SegmenterCls(
            min_chars=int(os.environ.get("TTS_STREAM_MIN_CHARS", "6")),
            soft_chars=int(os.environ.get("TTS_STREAM_SOFT_CHARS", "12")),
            max_chars=int(os.environ.get("TTS_STREAM_MAX_CHARS", "42")),
            first_soft_chars=int(
                os.environ.get("TTS_STREAM_FIRST_SOFT_CHARS", "8")),
        )
        session = coordinator.begin(replace=True)
        first_result = [False]
        result_lock = threading.Lock()

        def _record_done(ok, error="", latency=None):
            with result_lock:
                if first_result[0]:
                    return
                first_result[0] = True
                if isinstance(latency_meta, dict):
                    if isinstance(latency, dict) and latency.get("first_audio_ms") is not None:
                        latency_meta["tts_ms"] = _display_tts_ms(
                            latency.get("backend") or TTS_BACKEND,
                            latency["first_audio_ms"])
                    latency_meta["tts_pending"] = False
                    latency_meta["tts_ok"] = bool(ok)
                    if error:
                        latency_meta["tts_error"] = str(error)[:80]
                    else:
                        latency_meta.pop("tts_error", None)
                    self._update_chat_latency(latency_meta)
            self.set_pipeline("tts", "played" if ok else "playback failed")

        def _opening_done(ok, error="", latency=None):
            # Match ask_ai(): the opening audio is the first-audio metric when
            # it succeeds, and later answer callbacks must not overwrite it.
            _record_done(ok, error, latency)

        self.set_pipeline("tts", "streaming")
        self.set_pipeline("speaker", "board speaker")
        try:
            speak_fn = _get_tts_speak_fn()
            opening = "%s，" % self.memory.get("nickname", "同学")
            # The "<nickname>," opening is played only for surge (zero-shot cloning); stream/drizzle go
            # straight to the content, so that not every sentence starts with "<nickname>," which looks fake.
            if TTS_BACKEND == "surge":
                if _surge_available:
                    speak_fn(opening, callback=_opening_done,
                             delay_ms=_OPENING_DELAY_MS)
        except Exception as exc:
            print("[TTS] tool opening failed: %s" % str(exc)[:120], flush=True)

        if TTS_BACKEND == "stream":
            segments = [cleaned]
        else:
            segments = [clean_for_tts(s) for s in segmenter.feed(cleaned)]
            segments.extend(clean_for_tts(s) for s in segmenter.flush())
        nickname = self.memory.get("nickname", "同学")
        submitted = 0
        for segment in segments:
            if not segment or len(segment) < 2:
                continue
            if segment.rstrip("，。！？!?、") == nickname:
                continue
            if coordinator.submit(session, segment, on_done=_record_done):
                submitted += 1
        coordinator.finish(session)
        if not submitted and not coordinator.is_active() and _global_asr_worker:
            _global_asr_worker.set_system_speaking(False)
        return submitted > 0

    def infer_mood_from_text(self, text):
        t = str(text)
        if any(k in t for k in ("开心", "高兴", "不错", "好棒", "爽", "顺利")):
            return "开心"
        if any(k in t for k in ("累", "困", "疲惫", "没力气", "好累")):
            return "疲惫"
        if any(k in t for k in ("焦虑", "紧张", "慌", "害怕", "担心")):
            return "焦虑"
        if any(k in t for k in ("崩溃", "哭", "受不了", "难受", "完蛋")):
            return "崩溃"
        return None

    def set_mood(self, mood, source="web"):
        if is_assistant_sleeping():
            return ""
        mood = str(mood).strip()
        if mood not in MOOD_STRATEGY:
            mood = "一般"
        with self.lock:
            self.current_mood = mood
            self.current_strategy = MOOD_STRATEGY[mood]
        self.add_event("mood", f"{source}: {mood}")
        self.memory.setdefault("recent_moods", []).append(mood)
        self.memory["recent_moods"] = self.memory["recent_moods"][-5:]
        self.save_memory()

        reply = self.ask_ai(
            f"我现在的情绪状态是{mood}，请根据这个状态陪我一句。",
            scene="mood",
            max_tokens=60
        )
        self.add_chat("assistant", reply, card_type="mood")
        return reply

    def _motion_client(self):
        """Use the one hardware-owned C07A client for every logged-in user."""
        if self.username and ROBOT is not None and ROBOT is not self:
            return ROBOT.motion
        return self.motion

    def motion_trigger(self, action, reason=""):
        """Best-effort high-level motion feedback; never blocks chat."""
        try:
            return self._motion_client().trigger(action, reason)
        except Exception as exc:
            self.add_event("c07a_error", str(exc)[:120])
            return False

    def motion_for_speech(self, scene="chat", reason="assistant speech"):
        """Start one deterministic in-place gesture for a spoken response."""
        try:
            action = self._speech_motion_action(scene, self.current_mood)
            accepted = self._motion_client().trigger_after_stop(
                action, "%s (%s)" % (reason, scene or "chat")
            )
            if accepted:
                self.add_event("c07a_speech", "%s -> %s" % (
                    scene or "chat", action.value))
            return accepted
        except Exception as exc:
            self.add_event("c07a_error", str(exc)[:120])
            return False

    def ask_ai(self, user_text, scene="chat", max_tokens=80):
        """Call the AI (DeepSeek); max_tokens keeps the reply short

        Lifecycle: created → streaming → synthesizing → finished
                                        └→ failed → fallback → finished
        """
        trace_id = uuid.uuid4().hex[:8]   # unique per request
        self.motion_trigger("THINK", "deepseek request")
        _latency_started = time.perf_counter()
        _state = "created"
        self._last_tts_ms = 0
        _reply_latency = {
            "trace_id": trace_id,
            "ai_ms": None,
            "tts_ms": None,
            "tts_pending": scene != "parse_time",
            "tts_ok": None,
            "tts_backend": TTS_BACKEND,
        }
        self._reply_latency_context.metric = _reply_latency
        print("[LATENCY:%s] ai_start epoch=%.6f scene=%s" %
              (trace_id, time.time(), scene), flush=True)
        user_text = str(user_text).strip()
        with self.lock:
            mood = self.current_mood
            goal = self.study_goal

        # user personalization info
        nickname = self.memory.get("nickname", "同学")
        assistant_name = self.get_assistant_name()
        system_prompt = (
            f"你的名字叫{assistant_name}。你的主人是{nickname}。你是{nickname}的温柔简短中文桌面陪伴机器人。"
            f"你必须自称'{assistant_name}'或'我'，称呼用户为'{nickname}'。"
            "回复1-2句中文，口语化，不要Markdown。"
            "句子要短（每句10-20字），多用逗号自然分隔成短句，绝对避免长难句和复杂从句。"
            "用户难过/焦虑/疲惫时先安慰，再给一个小建议。"
            "不要说教，不要暴露模型身份。"
            # The opening address is played by the system, so the AI reply must not carry one. The rule is
            # stated twice: without a thinking mode, repeating an instruction markedly improves compliance
            # (it effectively gives the model room to "think").
            "回复中不要称呼用户名字，直接说内容。"
            "记住：你的回复绝对不能以任何称呼开头（如'同学，'、'昵称，'），开场称呼由系统播放，你只需要说内容。"
            f"心情：{mood}。目标：{goal}。"
        )

        scene_hint = {
            "wakeup": "刚被唤醒，自然问候+提示功能。",
            "user_return": "用户回到桌前，自然问候。",
            "user_leave": "用户离开了，说明会等待。",
            "study_start": "用户开始专注，鼓励一句。",
            "study_status": "用户在学习中，简短鼓励或休息建议。",
            "study_end": "学习结束，根据报告总结鼓励。",
            "happy": "用户开心，积极回应。",
            "mood": "用户选了心情，简短陪伴回应。",
            "proactive_sit": "久坐提醒，轻柔。",
            "proactive_low": "用户安静太久，温柔询问。",
            "proactive_voice": "用户明确要求你主动发起互动，请直接问一个轻松、具体的问题，让对话自然开始。",
            "sensor_report": "依据板端实时传感器数据播报，数值准确，缺失项不提，不夸大风险。",
            "chat": "自然聊天，理解并回应。",
        }.get(scene, "自然聊天。")


        # Phase 2: PersonaService override
        try:
            from src.core.runtime_services import get_runtime_services as _grs2
            _rt = _grs2()
            if _rt.persona_service:
                pp = _rt.persona_service.build_system_prompt(
                    "default",
                    {"mood": mood, "goal": goal, "role_name": assistant_name},
                )
                if pp: system_prompt = pp
        except Exception: pass
        try:
            from src.persona.user_config import build_user_persona_prompt
            persona_hint = build_user_persona_prompt(
                self.get_persona_config(),
                seed=trace_id,
            )
            system_prompt = system_prompt + "\n" + persona_hint
        except Exception:
            pass

        # Append after PersonaService's override so every persona receives the
        # same fresh board readings shown by /api/sensors.
        try:
            from src.sensors.ai_context import build_sensor_context
            sensor_hint = build_sensor_context()
        except Exception:
            sensor_hint = "板端传感器状态暂不可用；不得编造传感器数值。"

        # User-scoped persistent memory and recent conversation history.
        # add_chat() persists the current user turn before ask_ai(), so the
        # composer removes that final duplicate when it matches user_text.
        recent_messages = []
        long_term_memories = {}
        try:
            _rt3 = _grs2()
            if _rt3.memory_service:
                user_id = self.username or "default"
                recent_messages = _rt3.memory_service.get_recent_messages(user_id, limit=24)
                long_term_memories = _rt3.memory_service.list_memories(user_id)
        except Exception: pass
        from src.memory.ai_context import build_ai_messages, build_memory_context
        memory_context = build_memory_context(self.memory, long_term_memories)
        messages = build_ai_messages(
            system_prompt,
            scene_hint,
            sensor_hint,
            memory_context,
            recent_messages,
            user_text,
            history_limit=0 if scene == "parse_time" else 24,
        )

        tts_enabled = (scene != "parse_time")
        _tts_session = None
        coordinator = None
        _greeting_submitted = False
        _first_token_received = False
        _first_token_ms = None   # when DeepSeek's first output token arrived, relative to ask_ai
        _submitted_segments = []
        _first_tts_result_recorded = False
        _first_tts_result_lock = threading.Lock()
        full_text = ""
        _llm_done_ms = 0.0
        _speech_motion_attempted = False
        _speech_motion_sent = False

        self._opening_recorded = False  # flag: the "<nickname>," opening was recorded (separate from the AI's first sentence)

        def _dispatch_speech_motion():
            """Dispatch exactly one gesture for this assistant utterance."""
            nonlocal _speech_motion_attempted, _speech_motion_sent
            if _speech_motion_attempted or not tts_enabled:
                return
            _speech_motion_attempted = True
            _speech_motion_sent = bool(self.motion_for_speech(
                scene, "assistant speech"))

        def _record_tts_done(ok, error="", latency=None):
            """Record TTS asynchronously without delaying the chat response."""
            nonlocal _first_tts_result_recorded
            # One reply may be split into several TTS jobs.  Only the first
            # job is allowed to finalize "first audio"; later callbacks used
            # to overwrite tts_ms seconds later and made a displayed latency
            # appear to change after it had already settled.
            # Once the opening is recorded, the AI's first sentence no longer claims the latency metric
            # (the opening is the first thing the user hears).
            with _first_tts_result_lock:
                if not _first_tts_result_recorded and not self._opening_recorded:
                    _first_tts_result_recorded = True
                    if isinstance(latency, dict) and latency.get("first_audio_ms"):
                        provider_ms = int(latency["first_audio_ms"])
                        # Baseline is "AI's first output token" → first audio (excluding DeepSeek
                        # thinking time, which matches what the user feels: how long after the AI starts
                        # answering before speech is heard)
                        stable_latency = dict(latency)
                        # The aggregated status latency reports the same real
                        # first-audio measurement as the chat bubble. It is
                        # derived from the provider's raw first_audio_ms.
                        display_ms = _display_tts_ms(
                            latency.get("backend") or TTS_BACKEND,
                            provider_ms)
                        stable_latency["first_audio_ms"] = display_ms
                        self._tts_latency_log.append(stable_latency)
                        self._last_tts_ms = display_ms
                        _reply_latency["tts_ms"] = display_ms
                    _reply_latency["tts_pending"] = False
                    _reply_latency["tts_ok"] = bool(ok)
                    if error:
                        _reply_latency["tts_error"] = str(error)[:80]
                    else:
                        _reply_latency.pop("tts_error", None)
                    self._update_chat_latency(_reply_latency)
            if ok:
                self.set_pipeline("tts", "已播放")
                self.set_pipeline("speaker", "板载扬声器")
            else:
                self.set_pipeline("tts", "播放失败")
                self.set_pipeline("speaker", "TTS错误: " + str(error or "unknown")[:24])

        self.set_pipeline("ai_reply", "生成中(DeepSeek·流式)")
        try:
            _state = "streaming"
            _tts_mode_for_reply = TTS_BACKEND
            coordinator, SegmenterCls = _get_stream_tts()
            seg_kwargs = dict(
                min_chars=int(os.environ.get("TTS_STREAM_MIN_CHARS", "6")),
                soft_chars=int(os.environ.get("TTS_STREAM_SOFT_CHARS", "12")),
                max_chars=int(os.environ.get("TTS_STREAM_MAX_CHARS", "42")),
                first_soft_chars=int(
                    os.environ.get("TTS_STREAM_FIRST_SOFT_CHARS", "8")),
            )
            segmenter = SegmenterCls(**seg_kwargs)
            _tts_session = coordinator.begin(replace=True)
            if tts_enabled:
                # The opening is played directly by _speak_fn (not inside the coordinator). Some callers
                # (proactive care / schedule / sensor announcements) do not pre-mute the way handle_chat
                # does, so mute the mic here for all of them: it keeps the robot's own speech from being
                # picked up by the VAD and turned into a self-conversation. handle_chat already mutes, and
                # muting again is idempotent.
                if _global_asr_worker:
                    _global_asr_worker.set_system_speaking(True)
                # Opening: right after the user stops talking, play "<nickname>," (without waiting for DeepSeek).
                # Two characters is very short → first audio = the opening's synthesis time, so DeepSeek latency
                # is bypassed entirely; DeepSeek generates the reply in parallel inside the opening's
                # synthesis/playback window. begin()'s stop has already cleared old sentences, the opening
                # (seq=0) uses the first-sentence-exclusive + 2-way window, and AI segments (seq=1..) join
                # seamlessly in order. surge has a text cache (fetch=0ms).
                _speak_fn = _get_tts_speak_fn()  # current backend's speak (kept in sync with the coordinator)
                def _on_opening_done(ok, error="", latency=None):
                    # Latency baseline: the first thing the user actually hears = playback of the
                    # "<nickname>," opening. The opening may complete after the AI's first sentence (the two
                    # run in parallel in the 2-way window), so the separate _opening_recorded flag makes the
                    # opening's record take priority over the AI's first sentence.
                    nonlocal _first_tts_result_recorded
                    if ok and isinstance(latency, dict) and latency.get("first_audio_ms") is not None:
                        self._opening_recorded = True
                        with _first_tts_result_lock:
                            if not _first_tts_result_recorded:
                                _first_tts_result_recorded = True
                                _reply_latency["tts_ms"] = int(latency["first_audio_ms"])
                                _reply_latency["tts_ok"] = True
                                _reply_latency["opening_audio"] = True
                                # The opening has produced sound: end the "computing first audio" state
                                # (otherwise the AI's first-sentence callback is skipped by opening_recorded
                                # and tts_pending never closes)
                                _reply_latency["tts_pending"] = False
                                self._update_chat_latency(_reply_latency)
                        # Frontend latency metric: the opening is the first sound the user hears
                        _opening = dict(latency)
                        _opening["first_audio_ms"] = int(latency["first_audio_ms"])
                        self._tts_latency_log.append(_opening)
                    # The opening finished and the coordinator holds no segments (empty reply/failure) →
                    # unmute, so the mic is not locked forever by the mute applied at the start of ask_ai.
                    # If AI segments are still playing, coordinator.is_active() is true and the coordinator
                    # unmutes once it finishes.
                    try:
                        if _global_asr_worker and not coordinator.is_active():
                            _global_asr_worker.set_system_speaking(False)
                    except Exception:
                        pass
                try:
                    _opening_text = "%s，" % nickname
                    if _tts_mode_for_reply == "surge" and _surge_available:
                        # The "<nickname>," opening is ready as soon as it is synthesized but playback is
                        # delayed by _OPENING_DELAY_MS: that leaves a reasoning window for DeepSeek's first
                        # sentence to be synthesized, so later sentences join seamlessly after it; the
                        # first-audio latency (submit→play) is thus a real value, not fake 2~4ms data.
                        _greeting_submitted = bool(
                            _speak_fn(_opening_text, callback=_on_opening_done,
                                      delay_ms=_OPENING_DELAY_MS))
                    else:
                        # stream/drizzle do not play the "<nickname>," opening (consistent with ask_ai's opening);
                        # otherwise the audio would say "<nickname>," while the text does not — even more fake.
                        _greeting_submitted = False
                except Exception:
                    _greeting_submitted = False
            _first_segment_submitted = False
            _tts_lat_count_before = len(coordinator.get_tts_latencies())
            _session_latencies = []
            if tts_enabled:
                self.set_pipeline("tts", "流式播报中")
                self.set_pipeline("speaker", "板载扬声器")

            with self.ai_lock:
                client = _get_deepseek_client()
                data_stream = {
                    "model": DEEPSEEK_MODEL,
                    "thinking": {"type": "disabled"},
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": max_tokens,
                    "stream": True
                }
                sse_lines = client.stream_chat(data_stream, DEEPSEEK_API_KEY)
                print("[LATENCY:%s] deepseek_headers elapsed_ms=%.1f" %
                      (trace_id, (time.perf_counter() - _latency_started) * 1000), flush=True)
                _latency_first_token = False
                for line in sse_lines:
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content") or delta.get("reasoning_content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if not content:
                        continue
                    if not _latency_first_token:
                        _latency_first_token = True
                        _first_token_received = True
                        _first_token_ms = (time.perf_counter() - _latency_started) * 1000
                        print("[LATENCY:%s] deepseek_first_token elapsed_ms=%.1f" %
                              (trace_id, _first_token_ms), flush=True)
                    full_text += content

                    if tts_enabled and _tts_mode_for_reply in ("drizzle", "surge"):
                        # Clean before feeding the segmenter: a leading parenthesized action description
                        # (e.g. "（温和地笑了笑）") accumulating in the buffer would make the first sentence
                        # split along the parenthesized long sentence and delay first audio; after cleaning,
                        # a first sentence like "同学，" splits out at its very short 2-3 characters.
                        for sentence in segmenter.feed(clean_for_tts(content)):
                            clean = clean_for_tts(sentence)
                            if clean and len(clean) >= 2:
                                # The system already played the "<nickname>," opening, so a pure-address
                                # sentence from the AI (e.g. "同学，") is skipped to avoid repeating the address
                                if clean.rstrip("，,。！？!?") == nickname:
                                    continue
                                if not _first_segment_submitted:
                                    _first_segment_submitted = True
                                    print("[LATENCY:%s] first_tts_segment elapsed_ms=%.1f" %
                                          (trace_id, (time.perf_counter() - _latency_started) * 1000), flush=True)
                                _dispatch_speech_motion()
                                _submitted_segments.append(clean)
                                coordinator.submit(
                                    _tts_session, clean, on_done=_record_tts_done)

            _llm_done_ms = (time.perf_counter() - _latency_started) * 1000
            print("[LATENCY:%s] deepseek_done elapsed_ms=%.1f chars=%d" %
                  (trace_id, _llm_done_ms, len(full_text)), flush=True)
            _state = "synthesizing"
            if tts_enabled:
                if _tts_mode_for_reply == "stream":
                    pending_speech = [clean_for_tts(full_text)]
                else:
                    pending_speech = [
                        clean_for_tts(sentence) for sentence in segmenter.flush()
                    ]
                for clean in pending_speech:
                    if clean and len(clean) >= 2:
                        # Skip pure-address sentences (they duplicate the opening; same as the streaming submit path)
                        if clean.rstrip("，,。！？!?") == nickname:
                            continue
                        if not _first_segment_submitted:
                            _first_segment_submitted = True
                            print("[LATENCY:%s] first_tts_segment elapsed_ms=%.1f" %
                                  (trace_id, (time.perf_counter() - _latency_started) * 1000), flush=True)
                        _dispatch_speech_motion()
                        _submitted_segments.append(clean)
                        coordinator.submit(
                            _tts_session, clean, on_done=_record_tts_done)
                coordinator.finish(_tts_session)

            _state = "finished"

        except Exception as e:
            # ── Streaming failed ─────────────────────────────────
            _state = "failed"
            print("[LATENCY:%s] STREAMING FAILED (%s_first_token=%s): %s"
                  % (trace_id, "after" if _first_token_received else "before",
                     _first_token_received, str(e)[:200]), flush=True)

            # Cancel the failed streaming session so pending TTS items
            # are not accidentally played when we fall back.
            if _tts_session and coordinator:
                try:
                    coordinator.cancel(_tts_session)
                except Exception:
                    pass

            # Attempt non-streaming fallback.
            try:
                ai_url = DEEPSEEK_BASE_URL + "/v1/chat/completions"
                data_fallback = {
                    "model": DEEPSEEK_MODEL,
                    "thinking": {"type": "disabled"},
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": max_tokens,
                    "stream": False
                }
                req2 = urllib.request.Request(
                    ai_url,
                    data=json.dumps(data_fallback, ensure_ascii=True).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + DEEPSEEK_API_KEY,
                    }
                )
                with self.ai_lock:
                    r2 = urllib.request.urlopen(req2, timeout=AI_TIMEOUT_SECONDS, context=_SSL_CTX)
                    raw = r2.read().decode("utf-8", errors="ignore")
                obj = json.loads(raw)
                answer = obj["choices"][0]["message"]["content"].strip()
                full_text = answer
                _state = "finished"
                # Do not replay segments already submitted during streaming.
                # speak_once with replace=True cancels any leftover session state.
                if tts_enabled:
                    clean = clean_for_tts(answer)
                    if clean and len(clean) >= 2 and not _submitted_segments:
                        # Only synthesize when NO segments were submitted before failure.
                        coordinator, _ = _get_stream_tts()
                        _dispatch_speech_motion()
                        coordinator.speak_once(clean, replace=True)
                        _lats = coordinator.get_tts_latencies()
                        if _lats:
                            new_during = _lats[_tts_lat_count_before:]
                            if new_during:
                                self._tts_latency_log.append(new_during[-1])
                                _session_latencies.append(new_during[-1])

            except Exception as e2:
                with self.lock:
                    self.ai_ok = False
                _set_deepseek_ok(False)
                self.set_pipeline("ai_reply", "失败")
                _reply_latency["ai_ms"] = round(
                    (time.perf_counter() - _latency_started) * 1000, 1)
                _reply_latency["tts_pending"] = False
                _reply_latency["tts_ok"] = False
                _reply_latency["tts_error"] = "AI服务不可用"
                print("[LATENCY:%s] FALLBACK ALSO FAILED: %s" % (trace_id, str(e2)[:200]), flush=True)
                return "我现在连不上AI服务，请稍后重试。"  # sanitized: do not expose raw exception details

        # ── Common post-processing (runs for both success and fallback) ──
        if not full_text.strip():
            answer = "嗯，我在听呢。"
        else:
            answer = full_text.strip()

        # Frontend display matches playback: the chat reply gets the "<nickname>," prefix (the opening
        # already played, so the frontend text shows "<nickname>, ..." too). Guard against a double
        # prefix when the AI occasionally brings its own address/action prefix.
        # ⚠️ Only surge (zero-shot cloning) keeps the "<nickname>," prefix; stream/drizzle speak the
        # content directly, so the code no longer prepends "<nickname>," (consistent with the opening).
        if tts_enabled and scene != "parse_time" and answer and TTS_BACKEND == "surge":
            core = re.sub(r'^[（(][^（()）]{1,15}[）)]\s*', '', answer)
            # When the AI brings its own address (e.g. "同学饿啦" with no comma), treat it as already addressed, avoiding "同学，同学饿啦"
            if not core.startswith(nickname):
                answer = "%s，%s" % (nickname, answer)

        with self.lock:
            self.ai_ok = True
            self.last_ai_reply = answer
            self.last_ai_latency_ms = round((time.perf_counter() - _latency_started) * 1000, 1)
        _set_deepseek_ok(True)
        self.set_pipeline("ai_reply", "已生成")
        total_ms = self.last_ai_latency_ms
        _reply_latency["ai_ms"] = round(_llm_done_ms or total_ms, 1)
        if not tts_enabled:
            _reply_latency["tts_pending"] = False
        print("[LATENCY:%s] ai_return elapsed_ms=%.1f state=%s" %
              (trace_id, total_ms, _state), flush=True)
        self.add_event(
            "latency",
            "AI响应: %.0fms (首字后完成)，TTS后台播放 trace=%s"
            % (_llm_done_ms or total_ms, trace_id),
        )
        if not _speech_motion_sent:
            self.motion_trigger("STOP", "deepseek complete")
        # Fallback unmute: the opening was not submitted and the coordinator holds no segments → the mute
        # applied at the start of this function must be lifted here (otherwise the mic stays locked forever).
        # Once the opening is submitted, _on_opening_done / the coordinator lifts the mute after playback.
        if tts_enabled and not _greeting_submitted:
            try:
                _coord, _ = _get_stream_tts()
                if _global_asr_worker and not _coord.is_active():
                    _global_asr_worker.set_system_speaking(False)
            except Exception:
                if _global_asr_worker:
                    try:
                        _global_asr_worker.set_system_speaking(False)
                    except Exception:
                        pass
        return answer

    def start_study(self, source="web", goal=None):
        if goal is None:
            goal = self.study_goal
        goal = str(goal).strip() or "待填写"

        if self.study_start:
            if goal != "待填写":
                self.study_goal = goal
            reply = f"专注模式已经开着啦。本次目标：{self.study_goal}。"
            self.add_chat("assistant", reply)
            return reply

        self.study_start = time.time()
        self.study_goal = goal
        self.last_study_notice_minute = -1
        self.leave_count_in_session = 0
        self.voice_count_in_session = 0
        self.interaction_count_in_session = 0
        self.return_prompt = False
        self.last_sit_reminder_time = 0
        self.last_low_interaction_time = 0
        self.last_interaction_time = time.time()

        self.add_event("study", f"start by {source}, goal={goal}")

        reply = self.ask_ai(
            f"我要开始学习了，本次目标是：{goal}。",
            scene="study_start",
            max_tokens=60
        )
        self.add_chat("assistant", reply)
        return reply

    def stop_study(self):
        if not self.study_start:
            reply = "现在还没有开启专注模式。"
            self.add_chat("assistant", reply)
            return reply

        elapsed = int(time.time() - self.study_start)
        self.study_start = None
        self.last_study_notice_minute = -1
        self.return_prompt = False

        report = {
            "goal": self.study_goal,
            "duration_seconds": elapsed,
            "duration_text": format_time(elapsed),
            "leave_count": self.leave_count_in_session,
            "voice_count": self.voice_count_in_session,
            "interaction_count": self.interaction_count_in_session,
            "mood": self.current_mood,
            "strategy": self.current_strategy,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        with self.lock:
            self.daily["study_seconds"] += elapsed

        self.last_report = report
        self.remember_session(report)

        self.add_event("study", f"stop, duration {format_time(elapsed)}")

        report_text = (
            f"本次学习报告\n"
            f"学习目标：{report['goal']}\n"
            f"学习时长：{report['duration_text']}\n"
            f"离座次数：{report['leave_count']}次\n"
            f"语音互动：{report['voice_count']}次\n"
            f"总互动：{report['interaction_count']}次\n"
            f"情绪状态：{report['mood']}\n"
        )

        reply = self.ask_ai(
            report_text + "请给出一句总结和一个下次建议。",
            scene="study_end",
            max_tokens=80
        )

        final = report_text + self.get_assistant_name() + "建议：" + reply
        self.add_chat("assistant", final, card_type="report")
        return final

    def show_status_text(self):
        with self.lock:
            visual = "在桌前" if self.visual_state == "PRESENT" else "不在桌前/待机"
            if self.study_start:
                elapsed = int(time.time() - self.study_start)
                study = format_time(elapsed)
            else:
                study = "未开启"
            voice = "已连接" if self.voice_ok else "未连接"
            ai = "在线" if self.ai_ok else "未知/离线"
            goal = self.study_goal
            mood = self.current_mood
        return f"视觉状态：{visual}；专注计时：{study}；本次目标：{goal}；今日情绪：{mood}；语音模块：{voice}；AI服务：{ai}。"

    def handle_command(self, cmd, source="web", payload=None):
        payload = payload or {}
        cmd = str(cmd).strip().lower()

        # Block other voice commands while music plays (except wakeup) so the music is never interrupted.
        if is_music_playing() and cmd != "wakeup":
            self.add_event("command", "ignored during music: %s" % cmd)
            return ""

        # The visible UI button uses this quiet mode instead of the normal
        # sleep/wake flow.  It shares the hard silent boundary, but the only
        # wake source is the same button and waking never speaks.
        if cmd == "quiet_toggle":
            entering = not is_assistant_sleeping()
            if entering:
                self.motion_trigger("STOP", "quiet mode requested")
            set_manual_quiet_mode(entering)
            for robot in [ROBOT] + list(USER_ROBOTS.values()):
                if robot:
                    robot.add_event("quiet", "button: %s" % ("on" if entering else "off"))
            return ""

        if cmd in ("sleep", "voice_off"):
            self.motion_trigger("STOP", "sleep requested")
            with _sleep_lock:
                global _manual_quiet_mode
                _manual_quiet_mode = False
                try:
                    if os.path.exists(QUIET_STATE_FILE):
                        os.remove(QUIET_STATE_FILE)
                except Exception:
                    pass
            changed = set_assistant_sleeping(True)
            for robot in [ROBOT] + list(USER_ROBOTS.values()):
                if robot:
                    robot.add_event("sleep", f"entered by {source}" if changed else "already sleeping")
            return ""

        if cmd == "wakeup":
            now = time.time()
            # Button-controlled quiet mode intentionally ignores the offline
            # wake word; only quiet_toggle can restore interaction.
            if is_manual_quiet_mode():
                return ""
            # A sleeping companion can only be woken by the offline UART module;
            # direct web/API commands are deliberately ignored.
            if is_assistant_sleeping() and source != "voice":
                return ""
            if now - self.last_wakeup_time < 3:
                return None
            self.last_wakeup_time = now
            was_sleeping = is_assistant_sleeping()
            set_assistant_sleeping(False)
            greeting = "我醒啦。现在可以继续和我说话了。"
            targets = []
            seen = set()
            for robot in [ROBOT] + list(USER_ROBOTS.values()):
                if robot and id(robot) not in seen:
                    seen.add(id(robot))
                    targets.append(robot)
            for robot in targets:
                with robot.lock:
                    robot.voice_wakeup_time = now
                    robot.voice_wakeup_acked = False
                    robot.return_prompt = True
                    robot.last_voice_cmd = "wakeup"
                robot.add_event("voice", "wakeup: 语音唤醒成功")
                robot.motion_trigger("GREET", "voice wakeup")
                robot.add_chat("assistant", greeting, card_type="proactive")
            if was_sleeping:
                threading.Thread(target=self.call_tts, args=(greeting,), daemon=True).start()
            return greeting

        # Sleeping is a hard interaction boundary. Only the UART wake word is
        # accepted; web chat, other voice commands and proactive actions stay mute.
        if is_assistant_sleeping():
            return ""

        self.touch_interaction(source=source)

        if cmd in ("study", "start study", "focus"):
            return self.start_study(source=source, goal=payload.get("goal", None))
        if cmd in ("stop", "study end", "end study"):
            return self.stop_study()
        if cmd in ("status", "time"):
            reply = self.show_status_text()
            self.add_chat("assistant", reply)
            return reply
        if cmd in ("clear", "clear history"):
            with self.lock:
                self.chat_history = []
                self.last_report = None
            return "对话历史已经清空啦。"
        if cmd == "happy":
            # set_mood already does ask_ai + add_chat internally; return directly here, so that two
            # duplicate replies do not make begin(replace=True) cut off the TTS first sentence.
            return self.set_mood("开心", source=source)
        if cmd == "continue":
            with self.lock:
                self.return_prompt = False
            reply = "好，我们继续刚才的节奏。我在旁边陪你，不催，但也不让你孤军奋战。"
            self.add_chat("assistant", reply)
            return reply
        if cmd == "restart":
            with self.lock:
                self.return_prompt = False
            if self.study_start:
                self.stop_study()
            return self.start_study(source=source, goal=payload.get("goal", "重新开始专注"))
        if cmd == "rest":
            with self.lock:
                self.return_prompt = False
            reply = "可以，先休息一小会儿。喝口水、活动一下肩颈，回来我们再接上。"
            self.add_chat("assistant", reply)
            return reply
        # —— additional voice commands (offline voice module SU-03T) ——
        if cmd == "voice_on":
            # turn on voice interaction mode
            with self.lock:
                self.voice_wakeup_time = time.time()
                self.voice_wakeup_acked = False
            self.add_event("voice", "voice_on: 打开语音交互")
            reply = "语音交互已开启，我在听。有什么我可以帮你的吗？"
            self.add_chat("assistant", reply, card_type="proactive")
            return reply
        if cmd == "chat":
            # start chat mode
            self.add_event("voice", "chat: 开始聊天")
            reply = self.ask_ai("用户通过语音说想聊天。请开始一段轻松对话。", scene="chat", max_tokens=80)
            self.add_chat("assistant", reply)
            return reply
        if cmd == "stop_chat":
            # stop the chat
            self.add_event("voice", "stop_chat: 停止聊天")
            reply = "好，聊天结束啦。我在这里随时陪你。"
            self.add_chat("assistant", reply)
            return reply
        if cmd == "ack_wakeup":
            # frontend acknowledges the voice wakeup
            with self.lock:
                self.voice_wakeup_acked = True
            return "ok"
        if cmd == "tts_mode":
            # Exact mapping: drizzle=Piper, stream=Doubao, surge=custom voice.
            mode = str(payload.get("mode", "drizzle")).strip().lower()
            if mode not in ("drizzle", "stream", "surge"):
                return "不支持的模式: " + mode
            ok, msg = set_tts_backend(mode)
            if ok:
                self.add_event("tts", "声音切换为 %s" % mode)
                return "TTS 已切换为 %s (%s)" % (mode, msg)
            else:
                self.add_event("tts", "切换失败: %s" % msg)
                return "切换失败: %s" % msg

        reply = "收到命令：" + cmd
        self.add_chat("assistant", reply)
        return reply

    def _build_tool_system_prompt(self, scene="chat"):
        """Build the same persona/context base used by normal AI replies.

        The tool agent may add tool-use rules, but it must not switch to the
        old hard-coded persona or lose the active account's persona/sensors.
        """
        with self.lock:
            mood = self.current_mood
            goal = self.study_goal
        nickname = self.memory.get("nickname", "同学")
        assistant_name = self.get_assistant_name()
        prompt = (
            f"你的名字叫{assistant_name}。你的主人是{nickname}。"
            f"你是{nickname}的温柔简短中文桌面陪伴机器人。"
            f"你必须自称‘{assistant_name}’或‘我’，称呼用户为‘{nickname}’。"
            "回复1-2句中文，口语化，不要Markdown。"
            "句子要短（每句20字以内），多用逗号自然分隔成短句，避免长难句。"
            "用户难过/焦虑/疲惫时先安慰，再给一个小建议。"
            "回复中不要称呼用户名字，直接说内容；不要暴露模型身份。"
            f"心情：{mood}。目标：{goal}。"
        )
        try:
            from src.core.runtime_services import get_runtime_services
            rt = get_runtime_services()
            if rt.persona_service:
                pp = rt.persona_service.build_system_prompt(
                    "default",
                    {"mood": mood, "goal": goal, "role_name": assistant_name},
                )
                if pp:
                    prompt = pp
        except Exception:
            pass
        try:
            from src.persona.user_config import build_user_persona_prompt
            prompt += "\n" + build_user_persona_prompt(
                self.get_persona_config(), seed=uuid.uuid4().hex[:8])
        except Exception:
            pass
        try:
            from src.sensors.ai_context import build_sensor_context
            prompt += "\n" + build_sensor_context()
        except Exception:
            pass
        return prompt

    def handle_chat(self, text, source="web"):
        text = str(text).strip()
        if not text or is_assistant_sleeping():
            return ""

        # ── music playback command: block every other voice interaction while it plays ──
        if _is_music_request(text):
            if not _start_music_playback(self):
                # Already playing: reply through normal TTS to lift the mute gate (the music thread holds it).
                reply = "音乐还在播放中呢，等它放完我们再聊天吧。"
                self.add_chat("assistant", reply)
                self.call_tts(reply)
                return reply
            reply = "好呀，给你放一首音乐，放松一下吧。"
            self.add_chat("assistant", reply)
            return reply
        if is_music_playing():
            # Ignore other requests during music: text hint only; the half-duplex mute gate is held by the
            # music thread and no TTS is done here, so nothing competes with the playing music for the speaker.
            self.add_chat("assistant", "正在放音乐，等它放完我们再聊哦。")
            return "正在放音乐，等它放完我们再聊哦。"

        self.touch_interaction(source=source)

        # Mic gate: mute from the moment the user's message is being processed
        # until the reply's TTS has fully played (plus echo-settle). The
        # streaming coordinator or call_tts unmutes after the LAST WAV file of
        # the reply finishes; the safety check below handles the no-audio case.
        if _global_asr_worker:
            _global_asr_worker.set_system_speaking(True)

        # Save a user memo
        note_keywords = ("记住", "记着", "帮我记", "记下来", "记住哦", "别忘了", "提醒我")
        if any(kw in text for kw in note_keywords):
            notes = self.memory.setdefault("notes", [])
            is_new_note = text not in notes
            if is_new_note:
                notes.append(text)
            self.memory["notes"] = self.memory["notes"][-20:]
            self.save_memory()
            if is_new_note:
                try:
                    from src.core.runtime_services import get_runtime_services
                    rt = get_runtime_services()
                    if rt.memory_service:
                        rt.memory_service.remember(self.username or "default", text)
                except Exception:
                    pass
            self.add_event("memory", f"已记录备注: {text[:40]}...")

        inferred = self.infer_mood_from_text(text)
        if inferred:
            with self.lock:
                self.current_mood = inferred
                self.current_strategy = MOOD_STRATEGY[inferred]
            self.add_event("mood", f"inferred from chat: {inferred}")

        self.add_chat("user", text)

        # Explicit spoken trigger for the competition demo.  This is an
        # intentional user request, so it remains available even when the
        # autonomous proactive-speaking switch is off; the switch only gates
        # unsolicited perception/timer actions.
        proactive_voice_phrases = (
            "主动跟我聊", "主动和我聊", "主动聊聊", "主动问我",
            "开始主动交互", "开启主动交互", "来点主动交互",
            "主动找我聊", "你来主动聊", "你主动说话", "主动关心我",
        )
        if any(phrase in text for phrase in proactive_voice_phrases):
            self.add_event("proactive", "voice trigger: explicit user request")
            reply = self.ask_ai(
                "用户明确要求你主动开启一段互动。请直接问一个轻松、具体的问题，"
                "例如询问他此刻在做什么或今天最想完成什么，不要解释触发机制。",
                scene="proactive_voice", max_tokens=50)
            self.add_chat("assistant", reply, card_type="proactive")
            return reply

        # ── Sprint: Function Calling for tool-intent messages ──
        # Tool calling (multi-round DeepSeek + tool execution) is moved entirely to a background thread, so
        # that up to 3 synchronous rounds on the request thread / ASR poll thread cannot freeze the frontend
        # or drop speech. When the reply is ready it is stored via add_chat + streaming TTS and shown by the
        # frontend's 900ms status poll; the request thread returns immediately.
        tool_keywords = ("提醒", "闹钟", "几分钟后", "小时后", "明天", "记住", "记着",
                         "别忘了", "帮我记", "设备状态",
                         "天气", "多少度", "温度", "下雨", "下雪", "刮风", "几度")
        has_tool_intent = any(kw in text for kw in tool_keywords)
        if has_tool_intent:
            threading.Thread(
                target=self._run_tool_intent_worker, args=(text,),
                daemon=True, name="ToolIntent").start()
            return ""

        # ── non-tool intent: reminder gate → streaming ask_ai ──
        from src.core.reminder_gate import looks_like_reminder
        reminder = (self.schedule.parse_time_with_ai(text, self.ask_ai)
                    if looks_like_reminder(text) else None)
        if reminder:
            ts = self.schedule.format_trigger_time(reminder["trigger_time"])
            self.add_event("schedule", f"已添加日程: {reminder['content']} ({ts})")
        reply = self.ask_ai(text, scene="chat", max_tokens=80)
        self.add_chat("assistant", reply)
        # Safety: if the reply produced no audio at all, unmute now so the mic
        # is never left stuck muted. Otherwise the coordinator's "inactive"
        # transition unmutes after the last WAV finishes.
        if _global_asr_worker:
            try:
                _coord, _ = _get_stream_tts()
                if not _coord.is_active():
                    _global_asr_worker.set_system_speaking(False)
            except Exception:
                _global_asr_worker.set_system_speaking(False)
        return reply

    def _run_tool_intent_worker(self, text):
        """Run a tool intent in the background: tool call / reminder parsing / ask_ai → store + stream TTS.

        Fully isolated from handle_chat's main path (the request thread returns immediately) and surfaced
        by the frontend's status polling. The finally block guarantees an explicit unmute when no TTS was
        queued, so the mic is never muted forever.
        """
        _worker_started = time.perf_counter()
        try:
            fc_reply = None
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if rt.tool_service and getattr(rt, "tool_calling_coordinator", None) is None:
                    from src.tools.tool_calling_coordinator import ToolCallingCoordinator
                    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
                    rt.tool_calling_coordinator = ToolCallingCoordinator(
                        deepseek_client=_get_deepseek_client(),
                        api_key=api_key,
                        tool_service=rt.tool_service,
                        model=DEEPSEEK_MODEL,
                        max_loops=2,
                    )
                if getattr(rt, "tool_calling_coordinator", None):
                    sp = self._build_tool_system_prompt(scene="chat") + (
                        "涉及天气/温度/下雨/最高温/最低温的问题，必须优先调用 get_weather 工具获取实时数据，"
                        "不得自行编造天气信息。"
                        "未成功获取天气数据时，如实告知用户暂时无法查询，不得编造。")
                    history = [{"role": m["role"], "content": m["text"]}
                               for m in self.chat_history[-12:]]
                    fc_reply, fc_events = rt.tool_calling_coordinator.run(
                        user_text=text,
                        system_prompt=sp,
                        messages=history,
                        enabled_tools=["set_reminder", "remember_user_fact",
                                       "get_device_status", "get_weather"],
                        timeout_s=TOOL_BUDGET_S,
                    )
                    for evt in fc_events:
                        status = "OK" if evt.success else "FAIL"
                        detail = evt.result_text or evt.error_code or ""
                        self.add_event("tool", f"{evt.tool_name}: {status} {detail}"[:120])
                        print(f"[TOOL] {evt.tool_name} success={evt.success} "
                              f"elapsed={evt.elapsed_ms:.0f}ms "
                              f"args={evt.arguments}", flush=True)
            except Exception as _tc_err:
                import traceback as _tb
                print("[TOOL] ToolCallingCoordinator failed:", _tc_err, flush=True)
                _tb.print_exc()
                fc_reply = None

            tool_reply_needs_tts = bool(fc_reply)
            reply = fc_reply.strip() if fc_reply else None
            if not reply:
                # Only spend a second LLM request when the local reminder gate
                # detects an actual reminder intent. Normal chat stays one-call.
                try:
                    from src.core.reminder_gate import looks_like_reminder
                    reminder = (self.schedule.parse_time_with_ai(text, self.ask_ai)
                                if looks_like_reminder(text) else None)
                    if reminder:
                        ts = self.schedule.format_trigger_time(reminder["trigger_time"])
                        self.add_event("schedule", f"已添加日程: {reminder['content']} ({ts})")
                except Exception:
                    pass
                reply = self.ask_ai(text, scene="chat", max_tokens=80)

            if not reply:
                return
            tool_latency_meta = None
            if tool_reply_needs_tts:
                tool_latency_meta = {
                    "trace_id": uuid.uuid4().hex[:8],
                    "ai_ms": round((time.perf_counter() - _worker_started) * 1000, 1),
                    "tts_ms": None,
                    "tts_pending": True,
                    "tts_ok": None,
                    "tts_backend": TTS_BACKEND,
                }
            self.add_chat("assistant", reply, latency_meta=tool_latency_meta)
            if tool_reply_needs_tts:
                self._queue_streaming_reply_tts(reply, latency_meta=tool_latency_meta)
        finally:
            # The background path has no synchronous return to wait on; if no TTS was queued in the end we
            # must unmute explicitly (the handle_chat entry point muted), otherwise the mic stays locked.
            try:
                coordinator, _ = _get_stream_tts()
                if _global_asr_worker and not coordinator.is_active():
                    _global_asr_worker.set_system_speaking(False)
            except Exception:
                if _global_asr_worker:
                    try:
                        _global_asr_worker.set_system_speaking(False)
                    except Exception:
                        pass

    # ── DEPRECATED since Phase1 ──────────────────────────────────
    # VisionWorker (global singleton, _ensure_global_vision) is the
    # sole owner of /dev/video2.  This legacy frame-diff loop must
    # NOT be started — it would race VisionWorker for the camera.
    # ─────────────────────────────────────────────────────────────
    def vision_loop(self):
        import warnings as _w
        _w.warn("vision_loop is DEPRECATED — use global VisionWorker", DeprecationWarning, stacklevel=2)
        self.cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)

        if not self.cap.isOpened():
            self.camera_ok = False
            self.add_event("camera", f"open failed: {CAMERA_DEVICE}")
            return

        self.camera_ok = True
        self.add_event("camera", f"started: {CAMERA_DEVICE}")

        ret, prev = self.cap.read()
        if not ret:
            self.add_event("camera", "first frame failed")
            return

        time.sleep(1)

        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.3)
                continue

            diff = cv2.absdiff(prev, frame)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            _, th = cv2.threshold(gray, 30, 255, cv2.THRESH_BINARY)
            motion = cv2.countNonZero(th)
            now = time.time()

            if motion > VISION_MOTION_THRESHOLD:
                self.last_seen = now
                changed = False
                with self.lock:
                    if self.visual_state == "AWAY":
                        self.visual_state = "PRESENT"
                        changed = True

                if changed:
                    self.add_event("vision", "user returned")
                    if self.study_start:
                        with self.lock:
                            self.return_prompt = True
                        reply = self.ask_ai("我刚回到桌前，还在专注模式中。", scene="user_return", max_tokens=50)
                    else:
                        reply = self.ask_ai("我刚回到桌前。", scene="user_return", max_tokens=50)
                    self.add_chat("assistant", reply)

            if now - self.last_seen > LEAVE_TIMEOUT_SECONDS:
                changed = False
                with self.lock:
                    if self.visual_state == "PRESENT":
                        self.visual_state = "AWAY"
                        self.last_left_time = now
                        changed = True
                        self.daily["leave_events"] += 1
                        if self.study_start:
                            self.leave_count_in_session += 1

                if changed:
                    self.add_event("vision", "user left")
                    reply = self.ask_ai("我离开了桌前。", scene="user_leave", max_tokens=50)
                    self.add_chat("assistant", reply)

            prev = frame
            time.sleep(0.3)

    def voice_loop(self):
        rx_buffer = bytearray()
        retry_delay = 1
        while self.running:
            if self.voice_fd is None:
                try:
                    self.voice_fd = open_uart(VOICE_PORT, VOICE_BAUD)
                    rx_buffer.clear()
                    retry_delay = 1
                    with self.lock:
                        self.voice_ok = True
                    self.add_event("voice", f"UART ready: {VOICE_PORT} {VOICE_BAUD}")
                except Exception as exc:
                    with self.lock:
                        self.voice_ok = False
                    self.add_event("voice_error", f"UART open failed; retry in {retry_delay}s: {exc}")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 15)
                    continue
            try:
                rlist, _, _ = select.select([self.voice_fd], [], [], 0.5)
                if self.voice_fd in rlist:
                    data = os.read(self.voice_fd, 1024)
                    if not data:
                        raise OSError("UART returned EOF")
                    rx_buffer.extend(data)
                    for hex_str, cmd in extract_voice_commands(rx_buffer):
                        with self.lock:
                            self.last_voice_cmd = cmd
                        self.set_pipeline("voice_input", "已识别")
                        self.add_event("voice", f"RX {hex_str} -> {cmd}")
                        if cmd == "unknown":
                            self.motion_trigger("SHAKE", "unknown SU-03T frame")
                        if cmd == "wakeup" or (cmd != "unknown" and not is_assistant_sleeping()):
                            self.handle_command(cmd, source="voice")
            except Exception as exc:
                self.add_event("voice_error", f"UART disconnected: {exc}")
                try:
                    os.close(self.voice_fd)
                except Exception:
                    pass
                self.voice_fd = None
                with self.lock:
                    self.voice_ok = False
                time.sleep(1)

    def proactive_loop(self):
        while self.running:
            if is_assistant_sleeping():
                time.sleep(2)
                continue
            # No proactive announcements at all while music is playing.
            if is_music_playing():
                time.sleep(2)
                continue
            now = time.time()
            # One perception consumer remains on the default robot, but its
            # output must follow the currently active web/account robot.
            interaction_robot = self
            runtime_services = None
            try:
                from src.core.runtime_services import get_runtime_services
                runtime_services = get_runtime_services()
                interaction_robot = getattr(runtime_services, "_active_robot", None) or self
            except Exception:
                pass
            proactive_enabled = True
            try:
                service = getattr(runtime_services, "proactive_service", None)
                enabled_value = getattr(service, "enabled", True) if service else True
                proactive_enabled = bool(
                    enabled_value() if callable(enabled_value) else enabled_value)
            except Exception:
                proactive_enabled = True
            with interaction_robot.lock:
                study_running = interaction_robot.study_start is not None
                study_elapsed = int(now - interaction_robot.study_start) if interaction_robot.study_start else 0
                no_interaction_elapsed = int(now - interaction_robot.last_interaction_time)

            # VisionWorker person-presence (sticky hysteresis; replaces the raw
            # per-frame face_detected which flips on a slight head turn).
            visual_present = get_global_vision_state().get(
                "presence", get_global_vision_state().get("face_detected", False))

            # ── Sprint: ProactiveInteractionAdapter ──
            speaking = (
                _global_asr_worker is not None
                and getattr(_global_asr_worker, "_system_speaking", False)
            )
            try:
                from src.core.runtime_services import get_runtime_services
                rt = runtime_services or get_runtime_services()
                if getattr(rt, "proactive_adapter", None) is None:
                    from src.services.proactive_adapter import ProactiveInteractionAdapter, NoOpProactiveAdapter
                    if rt.proactive_service:
                        rt.proactive_adapter = ProactiveInteractionAdapter(rt.proactive_service)
                    else:
                        rt.proactive_adapter = NoOpProactiveAdapter()
                if rt.proactive_adapter:
                    rt.proactive_adapter.tick(
                        get_global_vision_state(),
                        interaction_robot.schedule.get_active(),
                        is_assistant_sleeping(), speaking, False)
                    decision = rt.proactive_adapter.poll_decision()
                    if decision:
                        reason_family = str(decision.reason).split(":", 1)[0]
                        if reason_family == "greeting":
                            reply = interaction_robot.ask_ai("用户刚来到桌前。", scene="proactive_greeting", max_tokens=50)
                            interaction_robot.add_chat("assistant", reply, card_type="proactive")
                        elif reason_family == "emotion_care":
                            reply = interaction_robot.ask_ai("用户看起来心情不太好。", scene="proactive_emotion", max_tokens=60)
                            interaction_robot.add_chat("assistant", reply, card_type="proactive")
                        elif reason_family in ("positive_expression", "presence_away", "presence_return") and not speaking:
                            content = decision.suggested_prompt
                            if content:
                                interaction_robot.add_event("proactive", content)
                                interaction_robot.add_chat("assistant", content, card_type="proactive")
                                interaction_robot.call_tts(content)
                        elif reason_family in ("reminder", "reminder_due") and not speaking:
                            content = decision.suggested_prompt or "日程到了"
                            interaction_robot.add_chat("assistant", content, card_type="proactive")
                            interaction_robot.call_tts(content)
                        elif reason_family == "silence_care" and not speaking:
                            reply = interaction_robot.ask_ai(
                                "用户已经安静了一段时间，请温柔地问问是否需要帮助。",
                                scene="proactive_low", max_tokens=50)
                            interaction_robot.add_chat("assistant", reply, card_type="proactive")
                        rt.proactive_adapter.record_executed(decision)
            except Exception:
                pass

            # Sensor data is device-wide.  Only the default robot consumes the
            # sensor stream, but its reply is written to the active account.
            if (proactive_enabled and self.username is None and not speaking
                    and now - self.last_sensor_announcement_check_time
                    >= SENSOR_ANNOUNCE_INTERVAL_SECONDS):
                self.last_sensor_announcement_check_time = now
                try:
                    from src.sensors.ai_context import poll_sensor_announcement
                    sensor_prompt = poll_sensor_announcement()
                    if sensor_prompt:
                        reply = interaction_robot.ask_ai(
                            sensor_prompt,
                            scene="sensor_report",
                            max_tokens=60,
                        )
                        interaction_robot.add_chat("assistant", reply, card_type="proactive")
                        with interaction_robot.lock:
                            interaction_robot.daily["proactive_reminders"] += 1
                        interaction_robot.add_event("sensor_announcement", reply)
                except Exception as exc:
                    interaction_robot.add_event("sensor_announcement_error", str(exc)[:120])

            # ── Legacy: sitting-too-long reminder + low-interaction care ──
            if proactive_enabled and visual_present:
                if study_running and study_elapsed >= SIT_REMINDER_SECONDS and now - interaction_robot.last_sit_reminder_time >= PROACTIVE_COOLDOWN_SECONDS:
                    interaction_robot.last_sit_reminder_time = now
                    with interaction_robot.lock:
                        interaction_robot.daily["proactive_reminders"] += 1
                    interaction_robot.add_event("proactive", "sit reminder")
                    reply = interaction_robot.ask_ai(
                        f"我已经连续在桌前学习了{format_time(study_elapsed)}。",
                        scene="proactive_sit",
                        max_tokens=50
                    )
                    interaction_robot.add_chat("assistant", reply, card_type="proactive")

                if study_running and no_interaction_elapsed >= LOW_INTERACTION_SECONDS and now - interaction_robot.last_low_interaction_time >= PROACTIVE_COOLDOWN_SECONDS:
                    interaction_robot.last_low_interaction_time = now
                    with interaction_robot.lock:
                        interaction_robot.daily["proactive_reminders"] += 1
                    interaction_robot.add_event("proactive", "low interaction care")
                    reply = interaction_robot.ask_ai(
                        f"我已经安静了{format_time(no_interaction_elapsed)}，你想确认我是在专注还是卡住了。",
                        scene="proactive_low",
                        max_tokens=50
                    )
                    interaction_robot.add_chat("assistant", reply, card_type="proactive")
            time.sleep(2)

    def study_timer_loop(self):
        while self.running:
            if is_assistant_sleeping():
                time.sleep(1)
                continue
            if self.study_start:
                elapsed = int(time.time() - self.study_start)
                current_minute = elapsed // 60
                if current_minute > 0 and current_minute != self.last_study_notice_minute:
                    self.last_study_notice_minute = current_minute
                    self.add_event("study", f"elapsed {format_time(elapsed)}")
                    if current_minute % 25 == 0:
                        reply = self.ask_ai(
                            f"我已经学习了{format_time(elapsed)}，请提醒我休息一下。",
                            scene="study_status",
                            max_tokens=60
                        )
                        self.add_chat("assistant", reply)
            time.sleep(1)

    def get_status(self):
        # Monitoring must remain available even if an operation thread is
        # stalled while holding the robot mutation lock.  These are shallow,
        # eventually-consistent snapshots of Python-owned values.
        if self.study_start:
            elapsed = int(time.time() - self.study_start)
            study_running = True
            study_time = format_time(elapsed)
            study_seconds = elapsed
        else:
            study_running = False
            study_time = "未开启"
            study_seconds = 0

        today_seconds = self.daily["study_seconds"] + study_seconds
        try:
            _c07a = self._motion_client().health()
            _c07a["ready"] = bool(_c07a.get("enabled") and _c07a.get("connected"))
        except Exception:
            _c07a = {"enabled": False, "connected": False, "ready": False}

        _vision_state = get_global_vision_state()
        _asr_state = get_global_asr_state()
        _tts_state = get_global_tts_state()

        return {
            "username": self.username,
            "assistant_name": self.get_assistant_name(),
            "c07a": _c07a,
            "demo_mode": DEMO_MODE,
            "voice_ok": ROBOT.voice_ok if ROBOT is not None else self.voice_ok,
            "assistant_sleeping": is_assistant_sleeping(),
            "quiet_mode": is_manual_quiet_mode(),
            "music_playing": is_music_playing(),
            "sleep_changed_at": _assistant_sleep_changed_at,
            "ai_ok": _global_deepseek_ok(),
            "camera_ok": bool(_global_vision_worker is not None and _global_vision_worker._running),
            "visual_state": (
                "PRESENT" if _vision_state.get(
                    "presence",
                    _vision_state.get("face_detected", False))
                else "AWAY"
            ),
            "vision": _vision_state,
            "asr": _asr_state,
            "tts": _tts_state,
            # HDMI expression-screen compatibility fields (templates/display.html relies on top-level fields)
            "current_emotion": (_vision_state.get("emotion") or "neutral").lower(),
            "face_detected": bool(_vision_state.get("face_detected", False)),
            "listening": _asr_state.get("state") == "listening",
            "tts_audio_ts": os.path.getmtime("/tmp/nuanyu_tts_output.wav") if os.path.exists("/tmp/nuanyu_tts_output.wav") else 0,
            "study_running": study_running,
            "study_goal": self.study_goal,
            "study_time": study_time,
            "study_seconds": study_seconds,
            "leave_count_in_session": self.leave_count_in_session,
            "voice_count_in_session": self.voice_count_in_session,
            "interaction_count_in_session": self.interaction_count_in_session,
            "return_prompt": self.return_prompt,
            "last_voice_cmd": ROBOT.last_voice_cmd if ROBOT is not None else self.last_voice_cmd,
            "voice_wakeup_time": self.voice_wakeup_time,
            "voice_wakeup_acked": self.voice_wakeup_acked,
            "last_ai_reply": self.last_ai_reply,
            "last_ai_latency_ms": int(self.last_ai_latency_ms),
            "tts_latency": _tts_latency_summary(self._tts_latency_log),
            "current_mood": self.current_mood,
            "current_strategy": self.current_strategy,
            "pipeline": dict(self.pipeline),
            "tts_engine": _tts_state,
            "daily": {
                "study_time": format_time(today_seconds),
                "voice_interactions": self.daily["voice_interactions"],
                "web_interactions": self.daily["web_interactions"],
                "leave_events": self.daily["leave_events"],
                "proactive_reminders": self.daily["proactive_reminders"],
            },
            "last_report": self.last_report,
            "chat_history": list(self.chat_history[-35:]),
            "events": list(self.events[-30:]),
            "schedule": self.schedule.get_active(),
            "memory": {
                "nickname": self.memory.get("nickname", "同学"),
                "notes": self.memory.get("notes", []),
                "recent_sessions": self.memory.get("recent_sessions", []),
                "favorite_goals": self.memory.get("favorite_goals", []),
                "recent_moods": self.memory.get("recent_moods", []),
            }
        }

    def start(self):
        # Only the default robot (no username) starts the camera and the voice serial port
        # Per-user instances must not seize hardware resources, or multiple instances fighting over the
        # serial port make replies land on a random account
        # When the watchdog is running, voice_loop is not started (the watchdog owns the port and forwards commands)
        _watchdog_mode = os.environ.get("NUANYU_WATCHDOG_MODE", "0") == "1"
        if self.username is None:
            self.motion.start()
            import atexit
            atexit.register(self.motion.close)
            if not _watchdog_mode:
                threading.Thread(target=self.voice_loop, daemon=True, name="VoiceUART").start()
        # VisionWorker is global — start once for all users
        _ensure_global_vision()
        # ASR Worker — board mic + Whisper Tiny CPU
        if self.username is None:
            _ensure_global_asr()
            set_assistant_sleeping(is_assistant_sleeping())
        # TTS Manager — Drizzle + Surge
        if self.username is None:
            _ensure_global_tts()
        threading.Thread(target=self.study_timer_loop, daemon=True).start()
        # Shared perception must have exactly one proactive consumer. Starting
        # this loop for every pre-created user caused four simultaneous LLM calls.
        if self.username is None:
            threading.Thread(
                target=self.proactive_loop, daemon=True,
                name="ProactiveDefault").start()
            threading.Thread(
                target=self.c07a_poll_loop, daemon=True,
                name="C07APoll").start()
        threading.Thread(target=self.schedule_loop, daemon=True).start()

    def c07a_poll_loop(self):
        """Poll C07A independently of the slower proactive rules."""
        while self.running:
            try:
                self.motion.poll()
            except Exception as exc:
                self.add_event("c07a_error", str(exc)[:120])
            time.sleep(0.05)

    def _start_vision(self):
        """Start VisionWorker for FER (only for default robot instance)."""
        try:
            self.vision_worker = VisionWorker()
            self.vision_worker.start()
            print("[VISION] VisionWorker started OK")
        except Exception as e:
            print(f"[VISION] Failed to start VisionWorker: {e}")
            self.vision_worker = None

    def _stop_vision(self):
        if self.vision_worker:
            self.vision_worker.stop()
            self.vision_worker = None

    def _vision_poll_loop(self):
        """Poll VisionWorker state at ~10Hz for smooth UI updates."""
        while self.running and self.vision_worker:
            try:
                state = self.vision_worker.get_latest_state()
                if state:
                    self._vision_state = state
            except Exception:
                pass
            time.sleep(0.1)

    def schedule_loop(self):
        """Check schedule reminders once every 30 seconds"""
        while self.running:
            time.sleep(30)
            if is_assistant_sleeping():
                continue
            # Do not announce schedules while music is playing, so the music is not interrupted.
            if is_music_playing():
                continue
            try:
                due = self.schedule.check_due()
                for r in due:
                    ts = time.strftime("%H:%M", time.localtime(r["trigger_time"]))
                    msg = f"⏰ 日程提醒：{r['content']}（设定时间：{ts}）"
                    self.add_chat("assistant", msg, card_type="proactive")
                    self.call_tts(msg)
                    self.add_event("schedule", f"triggered: {r['content']}")
            except Exception:
                pass


# ======================== HTML pages (including the login system) ========================

with open(os.path.join(PROJECT_ROOT, "templates", "login.html"), "r", encoding="utf-8") as _f:
    LOGIN_PAGE = _f.read()
with open(os.path.join(PROJECT_ROOT, "templates", "main.html"), "r", encoding="utf-8") as _f:
    HTML_PAGE = _f.read()
# HDMI expression-screen page — used by the 5-inch external display
_display_path = os.path.join(PROJECT_ROOT, "templates", "display.html")
if os.path.exists(_display_path):
    with open(_display_path, "r", encoding="utf-8") as _f:
        DISPLAY_PAGE = _f.read()
else:
    DISPLAY_PAGE = "<html><body><h1>display.html not found</h1></body></html>"

STATIC_ROOT = os.path.realpath(os.path.join(PROJECT_ROOT, "static"))
ASSET_ROOT = os.path.realpath(os.path.join(PROJECT_ROOT, "assets"))

class NuanyuHandler(BaseHTTPRequestHandler):
    def _make_cookie_str(self, sid=None):
        """Build the Set-Cookie string. sid=None deletes the cookie."""
        if sid:
            return "nuanyu_session=%s; Path=/; HttpOnly; Max-Age=%d; SameSite=Lax" % (sid, SESSION_TIMEOUT)
        else:
            return "nuanyu_session=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax"

    def _send_json(self, obj, status=200, set_cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html, status=200, set_cookie=None):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, request_path):
        """Serve versioned UI assets without changing any /api contract."""
        relative_path = request_path[len("/static/"):].replace("/", os.sep)
        file_path = os.path.realpath(os.path.join(STATIC_ROOT, relative_path))
        if not file_path.startswith(STATIC_ROOT + os.sep) or not os.path.isfile(file_path):
            self._send_json({"error": "not found"}, status=404)
            return
        with open(file_path, "rb") as static_file:
            data = static_file.read()
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_asset(self, request_path):
        """Serve hand-drawn character SVG assets without changing API routes."""
        relative_path = request_path[len("/assets/"):].replace("/", os.sep)
        file_path = os.path.realpath(os.path.join(ASSET_ROOT, relative_path))
        if not file_path.startswith(ASSET_ROOT + os.sep) or not os.path.isfile(file_path):
            self._send_json({"error": "not found"}, status=404)
            return
        with open(file_path, "rb") as asset_file:
            data = asset_file.read()
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    _MAX_BODY = 1 << 20  # 1MB: a LAN client must not be able to exhaust board memory with a huge Content-Length

    def _read_json(self):
        length = safe_int(self.headers.get("Content-Length", "0"), 0)
        if length <= 0:
            return {}
        if length > self._MAX_BODY:
            print("[HTTP] oversized body rejected: %d bytes" % length, flush=True)
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        # A non-object JSON body (e.g. [1,2]) would make every data.get() raise AttributeError; guard uniformly
        return obj if isinstance(obj, dict) else {}

    def _get_cookie_sid(self):
        """Extract the session id from the request cookies"""
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        sid_cookie = cookies.get("nuanyu_session")
        if sid_cookie:
            return sid_cookie.value.strip()
        return None

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path

        if path.startswith("/static/"):
            self._send_static(path)
            return

        if path.startswith("/assets/"):
            self._send_asset(path)
            return

        # login page
        if path == "/":
            sid = self._get_cookie_sid()
            username = get_session_user(sid)
            if username:
                # logged in → show the main page
                self._send_html(HTML_PAGE)
            else:
                # not logged in → show the login page
                self._send_html(LOGIN_PAGE)
            return

        # HDMI expression-screen page (for the 5-inch external display kiosk; no login needed)
        if path == "/display":
            self._send_html(DISPLAY_PAGE)
            return

        if path == "/api/status":
            robot, username = get_robot_for_request(self)
            status = robot.get_status()
            status["username"] = username or ""
            self._send_json(status)
            return

        if path == "/api/health":
            try:
                from src.core.runtime_services import get_runtime_services
                snap = get_runtime_services().health_snapshot()
                self._send_json({"ok": True, "health": snap})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        # Phase 2 GET APIs
        if path == "/api/persona":
            # Auth: require valid session (same as /api/status)
            robot_auth, _ = get_robot_for_request(self)
            if robot_auth is None:
                self._send_json({"ok": False, "error": "authentication required"}, status=401)
                return
            try:
                from src.persona.user_config import public_presets
                active = robot_auth.get_persona_config()
                role_name = robot_auth.get_assistant_name()
                active["name"] = role_name
                self._send_json({
                    "ok": True,
                    "presets": public_presets(),
                    "active": active,
                    "role_name": role_name,
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/memory/stats":
            # Auth: require valid session (same as /api/status)
            robot_auth, _ = get_robot_for_request(self)
            if robot_auth is None:
                self._send_json({"ok": False, "error": "authentication required"}, status=401)
                return
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if rt.memory_service:
                    self._send_json({"ok": True, "memory": rt.memory_service.health()})
                else: self._send_json({"ok": False, "error": "memory service not available"}, status=503)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/tools":
            # Auth: require valid session (same as /api/status)
            robot_auth, _ = get_robot_for_request(self)
            if robot_auth is None:
                self._send_json({"ok": False, "error": "authentication required"}, status=401)
                return
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if rt.tool_service:
                    if not rt.tool_service.list_tools():
                        try:
                            from src.services.tool_service import register_builtin_tools
                            import time as _t
                            def _rh(m,d,p='normal'):
                                r=rt.default_robot
                                if r: r.schedule.add(str(m),_t.time()+int(d),source='ai');return{'ok':True}
                                return{'ok':False}
                            def _fh(f,c='general'):
                                if rt.memory_service:
                                    uid=getattr(rt.default_robot,'username','default')or'default'
                                    rt.memory_service.save_memory(uid,'fact:'+c+':'+str(hash(f)%10000),f)
                                    return{'ok':True}
                                return{'ok':False}
                            register_builtin_tools(rt.tool_service,
                                weather_provider=_weather_provider,
                                device_status_provider=lambda:{'ok':True},
                                reminder_handler=_rh, fact_handler=_fh)
                        except Exception:
                            pass
                    self._send_json({"ok": True, "tools": rt.tool_service.get_tool_schemas()})
                else: self._send_json({"ok": False, "error": "tool service not available"}, status=503)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/cloud/status":
            # Auth: require valid session (same as /api/status)
            robot_auth, _ = get_robot_for_request(self)
            if robot_auth is None:
                self._send_json({"ok": False, "error": "authentication required"}, status=401)
                return
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if rt.cloud_sync_service:
                    h = rt.cloud_sync_service.health()
                    cmds = len(rt.cloud_sync_service.poll_commands())
                    self._send_json({"ok": True, "cloud": h, "downlink_commands": cmds})
                else: self._send_json({"ok": False, "error": "cloud service not available"}, status=503)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return


        if path == "/api/proactive/status":
            # Auth: require valid session (same as /api/status)
            robot_auth, _ = get_robot_for_request(self)
            if robot_auth is None:
                self._send_json({"ok": False, "error": "authentication required"}, status=401)
                return
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if getattr(rt, "proactive_adapter", None) is None:
                    from src.services.proactive_adapter import ProactiveInteractionAdapter, NoOpProactiveAdapter
                    rt.proactive_adapter = ProactiveInteractionAdapter(rt.proactive_service) if rt.proactive_service else NoOpProactiveAdapter()
                self._send_json({"ok": True, "proactive": rt.proactive_adapter.health()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return
        if path == "/api/tts_status":
            self._send_json({"ok": True, "tts": get_global_tts_state()})
            return

        if path == "/api/sensors":
            try:
                from src.sensors.sensor_state import get_sensor_state
                state = get_sensor_state()
                self._send_json({
                    "ok": True,
                    "sensors": state.snapshot(),
                    "health": state.health(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/weather":
            # Weather (for the mini-program / demo): real-time weather for the logged-in user's city (keyless Open-Meteo).
            try:
                city = "上海"
                try:
                    robot_for_weather, _ = get_robot_for_request(self)
                    mem = getattr(robot_for_weather, "memory", None) or {}
                    profile = mem.get("profile") or {}
                    city = profile.get("weather_city") or city
                except Exception:
                    pass
                result = _weather_provider(city)
                # Do not overwrite the provider's ok — on failure keep ok=False + error; never fake success.
                if result.get("ok"):
                    result["city"] = result.get("city") or city
                self._send_json(result)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/tts_audio":
            wav_path = "/tmp/nuanyu_tts_output.wav"
            if os.path.exists(wav_path):
                with open(wav_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json({"error": "no audio yet"}, status=404)
            return

        if path == "/api/users":
            # Return the user list (without password hashes)
            users = load_users()
            safe_users = {name: {"created": info.get("created", "")} for name, info in users.items()}
            self._send_json({"users": safe_users})
            return

        if path == "/api/whoami":
            sid = self._get_cookie_sid()
            username = get_session_user(sid)
            if username:
                self._send_json({"ok": True, "username": username})
            else:
                self._send_json({"ok": False, "username": None})
            return

        if path == "/api/logout":
            sid = self._get_cookie_sid()
            if sid:
                destroy_session(sid)
            self._send_json({"ok": True}, set_cookie=self._make_cookie_str(None))
            return

        # ── Surge preview audio (GET, with voice_id validation) ──
        if path.startswith("/api/surge/audio/"):
            voice_id = path.split("/")[-1]
            import re as _re
            if not voice_id or not _re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$', voice_id):
                self._send_json({"error": "invalid voice_id"}, status=400)
                return
            try:
                p = get_surge_provider()
                wav_bytes = p._store.get_preview_wav(voice_id)
                if wav_bytes:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.send_header("Cache-Control", "max-age=3600")
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                    return
            except Exception:
                pass
            self._send_json({"error": "preview not found"}, status=404)
            return

        # ── Stream / Doubao voice list (read-only, no auth needed) ──
        if path == "/api/stream/voices":
            try:
                voices = doubao_get_voices()
                cur_vt, cur_label = doubao_current_voice()
                self._send_json({
                    "ok": True,
                    "voices": voices,
                    "current": {"voice_type": cur_vt, "label": cur_label},
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()

        # login endpoint
        if path == "/api/login":
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", ""))
            if not username or not password:
                self._send_json({"ok": False, "error": "用户名和密码不能为空"}, status=400)
                return
            # ASR not ready: reject login while it is still loading (~30 seconds); if it was attempted but
            # failed (missing model/OOM), do not block text-chat login — voice degrades to unavailable
            # instead of the whole site going down.
            if ASR_ENABLED and not is_asr_ready() and not _asr_preload_done:
                self._send_json({
                    "ok": False,
                    "error": "语音识别引擎正在加载中，请稍候…（约30秒）",
                    "asr_loading": True,
                }, status=503)
                return
            if verify_user(username, password):
                sid = create_session(username)
                # Make sure the user's own robot instance exists
                logged_in_robot = get_robot_for_user(username)
                # Switch the single always-on ASR route immediately.  Without
                # this, the first spoken turn after login could still land in
                # the anonymous/default robot until a later status request.
                try:
                    from src.core.runtime_services import get_runtime_services
                    get_runtime_services().set_active_robot(logged_in_robot)
                except Exception:
                    pass
                self._send_json({"ok": True, "username": username},
                                set_cookie=self._make_cookie_str(sid))
            else:
                self._send_json({"ok": False, "error": "用户名或密码错误"}, status=401)
            return

        # The APIs below require login (without login they use the default ROBOT)
        robot, username = get_robot_for_request(self)

        if path == "/api/mic_control":
            worker = _global_asr_worker
            if worker is None:
                try:
                    from src.core.runtime_services import get_asr_worker
                    worker = get_asr_worker()
                except Exception:
                    worker = None
            if worker is None:
                self._send_json({"ok": False, "error": "ASR worker not ready"}, status=503)
                return
            raw_muted = data.get("muted", None)
            if raw_muted is None:
                current = bool(worker.is_manual_muted()
                               if hasattr(worker, "is_manual_muted")
                               else getattr(worker, "_manual_muted", False))
                self._send_json({"ok": True, "manual_mic_muted": current})
                return
            if isinstance(raw_muted, str):
                muted = raw_muted.strip().lower() in ("1", "true", "yes", "on")
            else:
                muted = bool(raw_muted)
            worker.set_manual_mute(muted)
            status = robot.get_status() if robot is not None else {}
            self._send_json({"ok": True, "manual_mic_muted": muted, "status": status})
            return

        if path == "/api/proactive/config":
            if robot is None:
                self._send_json({"ok": False, "error": "robot not ready"}, status=503)
                return
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if getattr(rt, "proactive_adapter", None) is None:
                    from src.services.proactive_adapter import ProactiveInteractionAdapter, NoOpProactiveAdapter
                    rt.proactive_adapter = (ProactiveInteractionAdapter(rt.proactive_service)
                                            if rt.proactive_service else NoOpProactiveAdapter())
                service = rt.proactive_service
                if "enabled" in data and service is not None and hasattr(service, "set_enabled"):
                    service.set_enabled(bool(data.get("enabled")))
                if "level" in data and service is not None and hasattr(service, "set_proactivity_level"):
                    service.set_proactivity_level(int(data.get("level")))
                self._send_json({"ok": True, "proactive": rt.proactive_adapter.health()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=400)
            return

        if path == "/api/proactive/demo":
            if robot is None:
                self._send_json({"ok": False, "error": "robot not ready"}, status=503)
                return
            try:
                reply = robot.ask_ai("用户刚来到桌前。", scene="proactive_greeting", max_tokens=50)
                robot.add_chat("assistant", reply, card_type="proactive")
                robot.add_event("proactive", "manual demo greeting")
                self._send_json({"ok": True, "reply": reply, "status": robot.get_status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/tts_provider":
            backend = data.get("backend", "").strip()
            if backend:
                ok, msg = set_tts_backend(backend)
                self._send_json({"ok": ok, "message": msg, "current": TTS_BACKEND})
            else:
                self._send_json({
                    "current": TTS_BACKEND,
                    "available": {
                        "drizzle": bool(_get_drizzle_backend().loaded),
                        "stream": bool(
                            _doubao_available and get_doubao_provider().loaded
                        ),
                        "surge": True,  # always allowed (falls back to drizzle if offline)
                        "surge_online": bool(_surge_available),
                    }
                })
            return

        # ── Surge / ZipVoice voice pack management ──

        if path == "/api/surge/voices":
            voices = []
            active_voice = None
            try:
                p = get_surge_provider()
                voices = p.list_voices()
                active_voice = p.get_active_voice()
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
                return
            self._send_json({
                "ok": True,
                "voices": voices,
                "active_voice_id": active_voice.get("voice_id") if active_voice else None,
            })
            return

        if path.startswith("/api/surge/voices/") and path.endswith("/activate"):
            voice_id = path.split("/")[-2]
            try:
                p = get_surge_provider()
                meta = p.activate_voice(voice_id)
                self._send_json({"ok": True, "voice": meta})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=404)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        if path.startswith("/api/surge/voices/") and path.endswith("/preview"):
            voice_id = path.split("/")[-2]
            try:
                p = get_surge_provider()
                wav_bytes = p._store.get_preview_wav(voice_id)
                if wav_bytes:
                    # A desktop WebView would play wav_b64 on the PC. Route
                    # preview audio through the same board-only path as
                    # normal Surge speech instead.
                    from src.tts.surge_lite_provider import _play_wav_bytes

                    def _play_board_preview():
                        ok, _, _, _ = _play_wav_bytes(wav_bytes)
                        print("[SurgeLite] preview target=board ok=%s" % ok,
                              flush=True)

                    threading.Thread(
                        target=_play_board_preview,
                        daemon=True,
                        name="SurgeBoardPreview",
                    ).start()
                    self._send_json({"ok": True, "output": "board"})
                else:
                    self._send_json({"ok": False, "error": "preview not available"}, status=404)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        if path.startswith("/api/surge/voices/") and path.endswith("/delete"):
            voice_id = path.split("/")[-2]
            try:
                p = get_surge_provider()
                p.delete_voice(voice_id)
                self._send_json({"ok": True})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=404)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        if path == "/api/surge/upload":
            name = str(data.get("name", "")).strip()
            audio_b64 = str(data.get("audio_b64", "")).strip()
            ref_text = str(data.get("reference_text", "")).strip()
            original_filename = str(data.get("filename", "")).strip()

            if not name:
                self._send_json({"ok": False, "error": "语音包名称不能为空"}, status=400)
                return
            if not audio_b64:
                self._send_json({"ok": False, "error": "参考音频不能为空"}, status=400)
                return
            if not ref_text:
                self._send_json({"ok": False, "error": "参考文本不能为空"}, status=400)
                return

            try:
                p = get_surge_provider()
                voice_id, meta = p.create_voice(name, audio_b64, ref_text, original_filename)
                self._send_json({"ok": True, "voice_id": voice_id, "voice": meta})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        if path == "/api/surge/health":
            try:
                p = get_surge_provider()
                self._send_json({"ok": True, "health": p.health()})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        # ── Stream / Doubao voice selection ──

        if path == "/api/stream/voice":
            vt = str(data.get("voice_type", "")).strip()
            if not vt:
                self._send_json({"ok": False, "error": "missing voice_type"}, status=400)
                return
            ok, msg = doubao_set_voice(vt)
            if ok:
                cur_vt, cur_label = doubao_current_voice()
                self._send_json({
                    "ok": True,
                    "message": "voice switched to %s" % msg,
                    "current": {"voice_type": cur_vt, "label": cur_label},
                })
            else:
                self._send_json({"ok": False, "error": msg}, status=400)
            return

        if path == "/api/stream/preview":
            if is_assistant_sleeping():
                self._send_json({"ok": False, "sleeping": True})
                return
            try:
                cur_vt, cur_label = doubao_current_voice()
                queued = doubao_preview()
                self._send_json({
                    "ok": queued,
                    "message": "preview queued" if queued else "preview failed to queue",
                    "voice": {"voice_type": cur_vt, "label": cur_label},
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)[:200]}, status=500)
            return

        if path == "/api/shutdown":
            # Login is required: a single curl from an unauthenticated LAN client could kill the whole
            # process, which on a demo floor amounts to a remote shutdown.
            if not username:
                self._send_json(
                    {"ok": False, "error": "需要登录后才能关机"},
                    status=401,
                )
                return
            self._send_json({"ok": True})
            # Graceful shutdown via RuntimeServices (idempotent).
            # systemd watchdog will re-launch the process afterward.
            def _do_shutdown():
                time.sleep(0.3)
                try:
                    from src.core.runtime_services import get_runtime_services
                    get_runtime_services().stop()
                    print("[SHUTDOWN] RuntimeServices stopped OK", flush=True)
                except Exception as _sexc:
                    print("[SHUTDOWN] RuntimeServices stop failed: %s" % str(_sexc)[:200], flush=True)
                import os as _os; _os._exit(0)
            threading.Thread(target=_do_shutdown, daemon=True, name="Shutdown").start()
            return

        if path == "/api/chat":
            _api_started = time.perf_counter()
            print("[LATENCY:API] received epoch=%.6f" % time.time(), flush=True)
            reply = robot.handle_chat(data.get("text", ""))
            print("[LATENCY:API] handle_done elapsed_ms=%.1f" %
                  ((time.perf_counter() - _api_started) * 1000), flush=True)
            self._send_json({"reply": reply, "status": robot.get_status()})
            print("[LATENCY:API] response_sent elapsed_ms=%.1f" %
                  ((time.perf_counter() - _api_started) * 1000), flush=True)
            return

        # Phase 2 POST APIs
        if path == "/api/persona/update":
            try:
                robot_auth, _ = get_robot_for_request(self)
                if robot_auth is None:
                    self._send_json(
                        {"ok": False, "error": "authentication required"},
                        status=401,
                    )
                    return
                if "name" in data:
                    role_name = robot_auth.set_assistant_name(data.get("name"))
                else:
                    role_name = robot_auth.get_assistant_name()
                config_fields = {
                    key: data[key] for key in (
                        "persona_id", "personality",
                        "language_style", "catchphrases",
                    ) if key in data
                }
                has_custom_fields = any(
                    key in data for key in (
                        "personality", "language_style", "catchphrases",
                    )
                )
                persona = robot_auth.update_persona_config(
                    config_fields,
                    apply_preset=("persona_id" in data and not has_custom_fields),
                )
                persona["name"] = role_name
                self._send_json({
                    "ok": True,
                    "persona": persona,
                    "role_name": role_name,
                })
            except Exception as exc:
                status = 400 if isinstance(exc, ValueError) else 500
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=status)
            return

        if path == "/api/cloud/command/ack":
            try:
                from src.core.runtime_services import get_runtime_services
                rt = get_runtime_services()
                if rt.cloud_sync_service:
                    ok = rt.cloud_sync_service.acknowledge_command(str(data.get("command_id", "")))
                    self._send_json({"ok": ok})
                else: self._send_json({"ok": False}, status=503)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, status=500)
            return

        if path == "/api/command":
            reply = robot.handle_command(data.get("command", ""), source="web", payload=data)
            self._send_json({"reply": reply, "status": robot.get_status()})
            return

        if path == "/api/mood":
            reply = robot.set_mood(data.get("mood", "一般"), source="web")
            self._send_json({"reply": reply, "status": robot.get_status()})
            return

        if path == "/api/tts":
            text = str(data.get("text", "")).strip()
            if is_assistant_sleeping():
                self._send_json({"ok": False, "sleeping": True, "status": robot.get_status()})
                return
            if not text:
                self._send_json({"ok": False, "error": "empty text", "status": robot.get_status()}, status=400)
                return
            robot.call_tts(text)
            self._send_json({"ok": True, "tts": get_global_tts_state(), "status": robot.get_status()})
            return

        if path == "/api/asr":
            if is_assistant_sleeping():
                self._send_json({"text": "", "status": "sleeping"})
                return
            import base64 as b64
            audio_b64 = data.get("audio", "")
            if not audio_b64:
                self._send_json({"text": "", "status": "error", "error": "no audio data"})
                return
            try:
                audio_bytes = b64.b64decode(audio_b64)
                boundary = "----NuanyuASR"
                body_lines = []
                body_lines.append("--" + boundary)
                body_lines.append('Content-Disposition: form-data; name="file"; filename="audio.wav"')
                body_lines.append("Content-Type: audio/wav")
                body_lines.append("")
                body = "\r\n".join(body_lines).encode("utf-8") + b"\r\n"
                body += audio_bytes + b"\r\n"
                body += ("--" + boundary + "--\r\n").encode("utf-8")
                req = urllib.request.Request(
                    ASR_SERVER_URL,
                    data=body,
                    headers={"Content-Type": "multipart/form-data; boundary=" + boundary}
                )
                r = urllib.request.urlopen(req, timeout=ASR_TIMEOUT_SECONDS, context=_SSL_CTX)
                result = json.loads(r.read().decode("utf-8"))
                text = result.get("text", "").strip()
                robot.set_pipeline("voice_input", "已识别: " + (text[:20] + "..." if len(text) > 20 else text))
                self._send_json({"text": text, "status": "ok"})
            except Exception as e:
                self._send_json({"text": "", "status": "error", "error": str(e)})
            return

        self._send_json({"error": "not found"}, status=404)


def main():
    global ROBOT
    print("=" * 55)
    print("  Nuanyu — multi-user proactive emotional companion 🎤")
    print("  AI: DeepSeek (%s)" % DEEPSEEK_MODEL)
    print("  Web URL on PC: http://127.0.0.1:%d" % WEB_PORT)
    print("=" * 55)
    print("")

    # ★ TTS preload — wait for TTS to be ready before starting the server
    print("[STARTUP] Preloading TTS engine...")
    preload_tts()
    tts_status = get_global_tts_state()
    print(
        "[STARTUP] TTS status: mode=%s backend=%s state=%s"
        % (TTS_BACKEND, tts_status.get("actual_backend"), tts_status.get("state"))
    )

    # ── RuntimeServices: single owner for all shared services ──
    # Creation order: config → services → robot → register → poll
    from src.core.runtime_services import get_runtime_services
    rt = get_runtime_services()
    rt.load_config()
    print("[STARTUP] RuntimeServices config loaded")

    # Step 3: Create default robot (before ASR so consumer has a target)
    print("[STARTUP] Creating default robot instance...")
    ROBOT = NuanyuCore(username=None)
    rt.register_default_robot(ROBOT)

    # Step 2: Initialize shared services (ASR, Vision, DeepSeek, TTS factory)
    rt.initialize()
    print("[STARTUP] RuntimeServices initialized: %s"
          % " → ".join(rt._startup_seq))

    # ★ ASR preload — do not start the service until the model is ready
    print("[STARTUP] Preloading ASR engine...")
    preload_asr()

    # ★ start all of the robot's background threads (voice/vision/proactive/study/schedule)
    ROBOT.start()
    # Materialize and register the actual TTS coordinator so runtime health
    # reflects the service that owns the live speech queue.
    rt.tts_coordinator, _ = _get_stream_tts()

    # Step 7: Start polling threads (ASR consumer, Vision state poll)
    rt.start_polling()
    # Deliver the ASR results buffered during preload
    rt.flush_pending_asr()

    # user list
    users = load_users()
    print("[STARTUP] Registered users: %s" % (", ".join(users.keys()) if users else "none"))
    # Restore persisted sessions (login survives web restarts, so the active robot is not reset to the default)
    _load_sessions()
    print("[STARTUP] Restored sessions: %d" % len(ACTIVE_SESSIONS))
    print("")

    # Pre-create a robot instance for each registered user
    for username in users.keys():
        print("[STARTUP] Pre-creating robot for user: %s" % username)
        rt.user_robots[username] = get_robot_for_user(username)

    print("")
    print("Please run on Windows first:")
    print("  adb reverse tcp:5002 tcp:5002   (ASR service)")
    print("  adb forward tcp:5004 tcp:5004     (Web service)")
    print("DEMO_MODE =", DEMO_MODE)
    print("DeepSeek max_tokens: chat=80, proactive=50, mood=60, study=60-80")
    print("")

    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), NuanyuHandler)
    print("Server started at 0.0.0.0:%d" % WEB_PORT)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        with open(os.path.join(NUANYU_ROOT, "crash.log"), "w") as f:
            traceback.print_exc(file=f)
        print(f"FATAL CRASH: {e}", flush=True)
        traceback.print_exc()
        os._exit(1)
