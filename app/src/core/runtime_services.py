#!/usr/bin/env python3
"""Runtime service container — Phase 1 stabilisation v2.

Every long-lived background worker (ASR, Vision, TTS coordinator,
DeepSeek client) is created, owned and stopped by RuntimeServices.
No other module may create a second instance of these services.

Creation order (enforced):
  1. load config
  2. create shared services (ASR, Vision, TTS, DeepSeek)
  3. create default robot
  4. register default robot
  5. start Vision
  6. start ASR
  7. start result polling
  8. start HTTP server

Stop order:  reverse of creation.

Wiring in main():
    rt = get_runtime_services()
    rt.initialize()          # steps 1-6
    default_robot = rt.default_robot
    default_robot.start()    # background threads (voice, proactive, …)
    rt.start_polling()       # ASR result consumer + ASR poll thread
    rt.start_http_server()   # or caller starts HTTPServer directly
"""

import collections
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

# The application's own directory (this file lives at app/src/core/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════
#  Lightweight structured logger  (no third-party deps)
# ═══════════════════════════════════════════════════════════════════

def _runtime_log(trace_id: str, service: str, event: str,
                 state: str = "", elapsed_ms: float = 0.0,
                 error_type: str = "", detail: str = "") -> None:
    """Emit one structured log line.  Never logs secrets."""
    stamp = time.strftime("%H:%M:%S")
    parts = [stamp, "trace=%s" % trace_id, "svc=%s" % service,
             "evt=%s" % event]
    if state:
        parts.append("state=%s" % state)
    if elapsed_ms:
        parts.append("elapsed=%.1fms" % elapsed_ms)
    if error_type:
        parts.append("err=%s" % error_type)
    if detail:
        parts.append("detail=%s" % detail[:240])
    print(" ".join(parts), flush=True)
    sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════════
#  RuntimeServices
# ═══════════════════════════════════════════════════════════════════

class RuntimeServices:
    """Process-wide service graph — single creator, single owner."""

    # ── Module-level singletons that old code still accesses directly ──
    # These are set by initialize() and kept for backwards compatibility.
    # New code should use get_runtime_services() and its accessors.

    def __init__(self):
        # ── Configuration ──
        self.config: Dict[str, Any] = {}
        self._initialized = False
        self._stopped = False

        # ── Unified stop event (all background threads / loops check this) ──
        self.stop_event = threading.Event()

        # ── Core services (Phase 1) ──
        self.asr_worker: Any = None
        self.vision_worker: Any = None
        self.tts_coordinator: Any = None
        self.deepseek_client: Any = None

        # ── Phase 2 service slots (NoOp by default) ──
        self.memory_service: Any = None
        self.persona_service: Any = None
        self.tool_service: Any = None
        self.proactive_service: Any = None
        self.cloud_sync_service: Any = None

        # ── Phase 2: ESP32-S3 sensor node ──
        self.sensor_reader: Any = None

        # ── Robot instances ──
        self.default_robot: Any = None
        self.user_robots: Dict[str, Any] = {}

        # ── Background threads owned by RuntimeServices ──
        self._threads: List[threading.Thread] = []
        self._poll_threads: List[threading.Thread] = []

        # ── Locks ──
        self._lock = threading.Lock()
        self._robot_lock = threading.Lock()

        # ── Vision state (module-level compatibility) ──
        self.vision_state: Dict[str, Any] = {
            "face_detected": False, "emotion": "unknown",
            "emotion_zh": "未识别", "confidence": 0.0,
            "face_source": "none", "fer_latency_ms": 0.0,
        }
        self._vision_lock = threading.Lock()

        # ── Tracing / startup seq ──
        self._startup_seq: List[str] = []

    # ═══════════════════════════════════════════════════════════════
    #  Configuration
    # ═══════════════════════════════════════════════════════════════

    def load_config(self) -> Dict[str, Any]:
        """Read configuration from env / files (idempotent)."""
        if self.config:
            return self.config
        self.config = {
            "asr_enabled": os.environ.get("ASR_ENABLED", "true").strip().lower() == "true",
            "asr_backend": os.environ.get("ASR_BACKEND", "whisper_tiny_cpu"),
            "vision_enabled": os.environ.get("VISION_ENABLED", "true").strip().lower() == "true",
            "fer_enabled": os.environ.get("FER_ENABLED", "true").strip().lower() == "true",
            "tts_backend": os.environ.get("NUANYU_TTS_BACKEND", "drizzle"),
            "web_host": "0.0.0.0",
            "web_port": int(os.environ.get("WEB_PORT", "5004")),
            "demo_mode": os.environ.get("DEMO_MODE", "true").strip().lower() == "true",
            "camera_device": os.environ.get("CAMERA_DEVICE", "/dev/video2"),
            "voice_port": os.environ.get("VOICE_PORT", "/dev/ttyHS1"),
            "sensor_enabled": os.environ.get("SENSOR_ENABLED", "true").strip().lower() == "true",
        }
        _runtime_log("-", "runtime", "config_loaded", state="ok")
        return self.config

    # ═══════════════════════════════════════════════════════════════
    #  Initialization  (steps 1–6 of creation order)
    # ═══════════════════════════════════════════════════════════════

    def initialize(self) -> None:
        """Create and wire every shared service (idempotent).

        Call **once** during main() before starting HTTP server.
        Subsequent calls are no-ops.
        """
        with self._lock:
            if self._initialized:
                return
            self._initialized = True

        _runtime_log("-", "runtime", "init_start")

        # ── Step 1: Config ──
        cfg = self.load_config()
        self._startup_seq.append("config")

        # ── Step 2: Create shared services ──
        self._create_asr_worker()
        self._startup_seq.append("asr_worker")
        self._create_vision_worker()
        self._startup_seq.append("vision_worker")
        self._create_deepseek_client()
        self._startup_seq.append("deepseek_client")
        # TTS coordinator is created lazily — register the factory.
        self._startup_seq.append("tts_factory")

        # ── Step 3: Create & register default robot ──
        self._startup_seq.append("robot_slot")

        # ── Step 3b: Sensor reader (ESP32-S3) ──
        self._create_sensor_reader()
        self._startup_seq.append("sensor_reader")

        # ── Step 4: Load Phase 2 services (NoOp by default) ──
        self._load_phase2_services()
        self._startup_seq.append("phase2_services")

        _runtime_log("-", "runtime", "init_done",
                     state="ready",
                     detail="seq=%s" % "→".join(self._startup_seq))

    # ═══════════════════════════════════════════════════════════════
    #  Service factories  (called exactly once by initialize)
    # ═══════════════════════════════════════════════════════════════

    def _create_asr_worker(self) -> None:
        if self.asr_worker is not None:
            return
        cfg = self.config
        if not cfg.get("asr_enabled", True):
            _runtime_log("-", "asr", "skipped", state="disabled")
            return
        try:
            # ── Lazy import: avoids Fibocom SDK on dev machines ──
            sys.path.insert(0, APP_DIR)
            from src.asr.asr_worker import ASRWorker
            t0 = time.perf_counter()
            self.asr_worker = ASRWorker()
            self.asr_worker.start()
            elapsed = (time.perf_counter() - t0) * 1000
            _runtime_log("-", "asr", "created",
                         state="running", elapsed_ms=elapsed)
        except Exception as exc:
            _runtime_log("-", "asr", "create_failed",
                         error_type=type(exc).__name__,
                         detail=str(exc)[:200])

    def _create_vision_worker(self) -> None:
        if self.vision_worker is not None:
            return
        cfg = self.config
        if not cfg.get("vision_enabled", True):
            _runtime_log("-", "vision", "skipped", state="disabled")
            return
        try:
            sys.path.insert(0, APP_DIR)
            from src.vision.vision_worker import VisionWorker
            t0 = time.perf_counter()
            self.vision_worker = VisionWorker()
            self.vision_worker.start()
            elapsed = (time.perf_counter() - t0) * 1000
            _runtime_log("-", "vision", "created",
                         state="running", elapsed_ms=elapsed)
        except Exception as exc:
            _runtime_log("-", "vision", "create_failed",
                         error_type=type(exc).__name__,
                         detail=str(exc)[:200])
            self.vision_worker = None  # ensure None on failure

    def _create_deepseek_client(self) -> None:
        if self.deepseek_client is not None:
            return
        try:
            sys.path.insert(0, APP_DIR)
            from src.core.deepseek_client import DeepSeekHTTPClient
            import ssl as _ssl
            import certifi
            ctx = _ssl.create_default_context(cafile=certifi.where())
            base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            self.deepseek_client = DeepSeekHTTPClient(base, ctx)
            _ = self.deepseek_client.warmup()
            _runtime_log("-", "deepseek", "created", state="warm")
        except Exception as exc:
            _runtime_log("-", "deepseek", "create_failed",
                         error_type=type(exc).__name__,
                         detail=str(exc)[:200])

    def _create_sensor_reader(self) -> None:
        if self.sensor_reader is not None:
            return
        cfg = self.config
        if not cfg.get("sensor_enabled", True):
            _runtime_log("-", "sensor", "skipped", state="disabled")
            return
        try:
            sys.path.insert(0, APP_DIR)
            from src.sensors.sensor_reader import SensorReader
            from src.sensors.sensor_state import get_sensor_state
            t0 = time.perf_counter()
            state = get_sensor_state()
            self.sensor_reader = SensorReader(state=state)
            started = self.sensor_reader.start()
            elapsed = (time.perf_counter() - t0) * 1000
            if started:
                _runtime_log("-", "sensor", "created",
                             state="running", elapsed_ms=elapsed)
            else:
                _runtime_log("-", "sensor", "created",
                             state="no_hardware", elapsed_ms=elapsed,
                             detail="ESP32-S3 not connected or receiver not deployed")
        except Exception as exc:
            _runtime_log("-", "sensor", "create_failed",
                         error_type=type(exc).__name__,
                         detail=str(exc)[:200])
            self.sensor_reader = None

    def _load_phase2_services(self) -> None:
        """Load real implementations with automatic fallback to NoOp.

        Order: try the real class first; if it raises ANY exception
        fall back to the corresponding NoOp so the voice pipeline
        never breaks.
        """
        for svc_name, noop_cls_name, real_cls_name, slot_name in [
            ("memory",  "NoOpMemoryService",  "SqliteMemoryService",       "memory_service"),
            ("persona", "NoOpPersonaService", "JsonFilePersonaService",    "persona_service"),
            ("tool",    "NoOpToolService",    "NoOpToolService",           "tool_service"),
            ("proactive","NoOpProactiveService","ProactiveServiceImpl",    "proactive_service"),
            ("cloud_sync","NoOpCloudSyncService","OfflineQueueCloudService","cloud_sync_service"),
        ]:
            svc = None
            backend = "noop_fallback"
            try:
                mod = __import__(
                    "src.services." + slot_name.replace("_service", "_service"),
                    fromlist=["*"])
                real_cls = getattr(mod, real_cls_name)
                svc = real_cls()
                backend = "real"
                _runtime_log("-", svc_name, "loaded",
                             state="real", detail=real_cls_name)
            except Exception as exc:
                _runtime_log("-", svc_name, "real_load_failed",
                             error_type=type(exc).__name__,
                             detail=str(exc)[:160])
                try:
                    mod = __import__(
                        "src.services." + slot_name.replace("_service", "_service"),
                        fromlist=["*"])
                    noop_cls = getattr(mod, noop_cls_name)
                    svc = noop_cls()
                    _runtime_log("-", svc_name, "loaded",
                                 state="noop_fallback", detail=noop_cls_name)
                except Exception as exc2:
                    _runtime_log("-", svc_name, "load_failed",
                                 error_type=type(exc2).__name__,
                                 detail=str(exc2)[:160])
            setattr(self, slot_name, svc)

        self._wire_tool_handlers()

    def _wire_tool_handlers(self) -> None:
        """Connect built-in tool handlers to real backend services."""
        if self.tool_service is None:
            return
        try:
            import time as _time
            from src.services.tool_service import register_builtin_tools

            def _remind(message, delay_seconds, priority="normal"):
                robot = self.default_robot
                if robot:
                    robot.schedule.add(str(message), _time.time() + int(delay_seconds), source="ai")
                    return {"ok": True}
                return {"ok": False}

            def _fact(fact, category="general"):
                if self.memory_service:
                    uid = getattr(self.default_robot, "username", "default") or "default"
                    self.memory_service.save_memory(uid, "user_fact:" + category + ":" + str(hash(fact) % 10000), fact)
                    return {"ok": True}
                return {"ok": False}

            def _status():
                return {"ok": True, "services": self.health_snapshot().get("services", [])}

            # Lazy-init weather provider (avoids imports on start)
            _weather = None
            def _get_weather(city: str):
                nonlocal _weather
                if _weather is None:
                    from src.weather import OpenMeteoWeatherProvider
                    _weather = OpenMeteoWeatherProvider()
                return _weather(city)

            register_builtin_tools(
                self.tool_service,
                weather_provider=_get_weather,
                device_status_provider=_status,
                reminder_handler=_remind,
                fact_handler=_fact,
            )
            _runtime_log("-", "tools", "handlers_wired", state="ok")
        except Exception as exc:
            _runtime_log("-", "tools", "handlers_wire_failed",
                         error_type=type(exc).__name__, detail=str(exc)[:160])


    # ═══════════════════════════════════════════════════════════════
    #  Robot management
    # ═══════════════════════════════════════════════════════════════

    def register_default_robot(self, robot) -> None:
        """Set the default robot (called by main() after NuanyuCore init)."""
        with self._lock:
            self.default_robot = robot
        _runtime_log("-", "robot", "default_registered",
                     state="ok" if robot else "none")

    def get_robot_for_user(self, username: Optional[str] = None) -> Optional[Any]:
        if not username:
            return self.default_robot
        with self._robot_lock:
            return self.user_robots.get(username)

    def ensure_user_robot(self, username: str, factory: Callable) -> Any:
        with self._robot_lock:
            if username not in self.user_robots:
                self.user_robots[username] = factory(username)
                _runtime_log("-", "robot", "user_created",
                             detail="user=%s" % username)
            return self.user_robots[username]

    def get_all_robots(self) -> List[Any]:
        seen = set()
        result = []
        if self.default_robot is not None:
            seen.add(id(self.default_robot))
            result.append(self.default_robot)
        with self._robot_lock:
            for robot in self.user_robots.values():
                if id(robot) not in seen:
                    seen.add(id(robot))
                    result.append(robot)
        return result

    # ═══════════════════════════════════════════════════════════════
    #  Polling threads  (started after default robot is ready)
    # ═══════════════════════════════════════════════════════════════

    def start_polling(self) -> None:
        """Start ASR result consumer and vision poll threads.

        Must be called after default_robot is registered AND started,
        so the ASR consumer has a valid target.
        """
        if self.asr_worker is not None:
            t = threading.Thread(target=self._poll_asr_loop,
                                 daemon=True, name="ASRPoll")
            t.start()
            self._poll_threads.append(t)
            _runtime_log("-", "asr", "polling_started")

        if self.vision_worker is not None:
            t = threading.Thread(target=self._poll_vision_loop,
                                 daemon=True, name="GVisionPoll")
            t.start()
            self._poll_threads.append(t)
            _runtime_log("-", "vision", "polling_started")

        # Sensor reader is self-polling (own daemon thread); just log.
        if self.sensor_reader is not None:
            _runtime_log("-", "sensor", "polling_started")

    # ── ASR result consumer ─────────────────────────────────────

    _ASR_PENDING_MAX = 8

    def __init_asr_pending(self):
        if not hasattr(self, '_asr_pending'):
            self._asr_pending = collections.deque(maxlen=self._ASR_PENDING_MAX)
            self._asr_pending_lock = threading.Lock()

    def _resolve_asr_target(self):
        # Always-on voice follows whichever robot the web UI is currently
        # showing (set_active_robot), so spoken replies land in the same
        # chat_history the frontend renders.  Fall back to the default
        # robot when no web session has been active.
        for robot in (getattr(self, "_active_robot", None), self.default_robot):
            if robot is not None and getattr(robot, "running", False):
                return robot
        return None

    def set_active_robot(self, robot):
        """Remember which robot an authenticated web request is using.

        The ASR consumer routes always-on voice to this robot so that the
        voice conversation and the web chat page share one chat_history,
        one memory and one latency pipeline.
        """
        self._active_robot = robot

    def _poll_asr_loop(self):
        self.__init_asr_pending()
        _runtime_log("-", "asr", "poll_loop_start")
        while not self.stop_event.is_set() and self.asr_worker is not None:
            try:
                result = self.asr_worker.get_result()
                if result and result.get("success") and result.get("text"):
                    text = result["text"].strip()
                    if not text:
                        continue
                    # UART wake-up is primary, but keep an offline microphone
                    # fallback so a stalled SU-03T cannot trap the app asleep.
                    wake_text = "".join(
                        ch for ch in text.lower()
                        if ch not in " ，。！？,.!?、\t\r\n"
                    )
                    wake_phrases = (
                        "你好小陪", "你好小裴", "你好小培",
                        "你好小暖", "你好暖语",
                    )
                    if any(phrase in wake_text for phrase in wake_phrases):
                        target = self._resolve_asr_target()
                        if target is not None:
                            _runtime_log("-", "asr", "wake_fallback",
                                         detail="text=%.40s" % text)
                            target.add_event(
                                "voice",
                                "offline ASR wake fallback: %s" % text,
                            )
                            target.handle_command("wakeup", source="voice")
                        continue
                    # Sleep check — delegated to robot
                    target = self._resolve_asr_target()
                    if target is not None:
                        if getattr(target, '_system_sleeping', False):
                            continue
                        _runtime_log("-", "asr", "delivered",
                                     detail="text=%.40s" % text)
                        target.add_event("asr", "recognized: %s" % text)
                        # Route through the same full chat pipeline as the web
                        # chat page: notes/memory keywords, mood inference,
                        # tool calling, reminder gate, symbol cleanup, latency
                        # record and TTS are all applied inside handle_chat.
                        reply = target.handle_chat(text, source="voice")
                        if reply:
                            _runtime_log("-", "asr", "chat_replied",
                                         detail="text=%.40s" % reply)
                    else:
                        with self._asr_pending_lock:
                            if len(self._asr_pending) >= self._ASR_PENDING_MAX:
                                dropped = self._asr_pending.popleft()
                                _runtime_log("-", "asr", "queue_drop",
                                             detail="dropped=%.40s" % dropped)
                            self._asr_pending.append(text)
                            _runtime_log("-", "asr", "buffered",
                                         detail="q=%d/%d" % (
                                             len(self._asr_pending),
                                             self._ASR_PENDING_MAX))
            except Exception:
                pass
            # Results are already complete at this point.  A 200 ms poll made
            # ASR feel slower for no benefit; 50 ms is still negligible CPU.
            time.sleep(0.05)
        _runtime_log("-", "asr", "poll_loop_stop")

    def flush_pending_asr(self) -> None:
        """Deliver buffered ASR results (called after robot is ready)."""
        target = self._resolve_asr_target()
        if target is None:
            return
        self.__init_asr_pending()
        with self._asr_pending_lock:
            while self._asr_pending:
                text = self._asr_pending.popleft()
                try:
                    _runtime_log("-", "asr", "flush_deliver",
                                 detail="text=%.40s" % text)
                    target.add_event("asr", "pending: %s" % text)
                    reply = target.handle_chat(text, source="voice")
                    if reply:
                        _runtime_log("-", "asr", "chat_replied",
                                     detail="text=%.40s" % reply)
                except Exception:
                    pass

    # ── Vision state poller ─────────────────────────────────────

    def _poll_vision_loop(self):
        _runtime_log("-", "vision", "poll_loop_start")
        while not self.stop_event.is_set() and self.vision_worker is not None:
            try:
                state = self.vision_worker.get_latest_state()
                if state:
                    with self._vision_lock:
                        self.vision_state = state
            except Exception:
                pass
            time.sleep(0.1)
        _runtime_log("-", "vision", "poll_loop_stop")

    def get_vision_state(self) -> Dict[str, Any]:
        with self._vision_lock:
            return dict(self.vision_state)

    # ═══════════════════════════════════════════════════════════════
    #  Stop  (reverse creation order, idempotent)
    # ═══════════════════════════════════════════════════════════════

    def stop(self) -> None:
        """Idempotent graceful shutdown.  Safe to call multiple times."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True

        _runtime_log("-", "runtime", "stop_start")
        self.stop_event.set()

        # 1. Stop polling threads
        for t in self._poll_threads:
            try:
                t.join(timeout=3.0)
            except Exception:
                pass
        self._poll_threads.clear()

        # 2. Stop vision (releases /dev/video2)
        if self.vision_worker:
            try:
                self.vision_worker.stop()
                _runtime_log("-", "vision", "stopped")
            except Exception as exc:
                _runtime_log("-", "vision", "stop_error",
                             error_type=type(exc).__name__)

        # 3. Stop ASR
        if self.asr_worker:
            try:
                self.asr_worker.stop()
                _runtime_log("-", "asr", "stopped")
            except Exception as exc:
                _runtime_log("-", "asr", "stop_error",
                             error_type=type(exc).__name__)

        # 3b. Stop sensor reader
        if self.sensor_reader:
            try:
                self.sensor_reader.stop()
                _runtime_log("-", "sensor", "stopped")
            except Exception as exc:
                _runtime_log("-", "sensor", "stop_error",
                             error_type=type(exc).__name__)

        # 4. Stop TTS coordinator
        if self.tts_coordinator:
            try:
                self.tts_coordinator.close()
                _runtime_log("-", "tts", "stopped")
            except Exception as exc:
                _runtime_log("-", "tts", "stop_error",
                             error_type=type(exc).__name__)

        # 5. Close DeepSeek
        if self.deepseek_client:
            try:
                self.deepseek_client.close()
                _runtime_log("-", "deepseek", "stopped")
            except Exception as exc:
                _runtime_log("-", "deepseek", "stop_error",
                             error_type=type(exc).__name__)

        # 5b. Close Phase 2 services
        for slot_name in ("memory_service", "persona_service",
                          "tool_service", "proactive_service",
                          "cloud_sync_service"):
            svc = getattr(self, slot_name, None)
            if svc is not None:
                try:
                    svc.close()
                    _runtime_log("-", slot_name, "stopped")
                except Exception as exc:
                    _runtime_log("-", slot_name, "stop_error",
                                 error_type=type(exc).__name__)

        # 6. Stop all robots
        for robot in self.get_all_robots():
            try:
                robot.running = False
            except Exception:
                pass

        # 7. Stop remaining background threads
        for t in self._threads:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._threads.clear()

        _runtime_log("-", "runtime", "stop_done")

    def is_stopping(self) -> bool:
        return self._stopped or self.stop_event.is_set()

    # ═══════════════════════════════════════════════════════════════
    #  Health
    # ═══════════════════════════════════════════════════════════════

    def health_snapshot(self) -> Dict[str, Any]:
        services = []
        for name, worker, check in [
            ("asr", self.asr_worker, lambda w: w.health().get("loaded", False)),
            ("vision", self.vision_worker, lambda w: w.health().get("running", False)),
            ("tts", self.tts_coordinator, lambda w: True),
            ("deepseek", self.deepseek_client, lambda w: True),
            ("default_robot", self.default_robot,
             lambda r: getattr(r, "running", False)),
        ]:
            detail = {}
            running = worker is not None
            ready = False
            if running:
                try:
                    ready = check(worker)
                except Exception:
                    pass
            services.append({
                "name": name, "running": running, "ready": ready,
                "extra": detail,
            })
        # Sensor reader
        if self.sensor_reader is not None:
            try:
                sh = self.sensor_reader.health()
                services.append({
                    "name": "sensor", "running": True,
                    "ready": sh.get("connected", False),
                    "extra": sh,
                })
            except Exception:
                services.append({
                    "name": "sensor", "running": False, "ready": False,
                    "extra": {},
                })
        else:
            services.append({
                "name": "sensor", "running": False, "ready": False,
                "extra": {"connected": False},
            })

        # Phase 2 services
        for name, slot in [
            ("memory", self.memory_service),
            ("persona", self.persona_service),
            ("tools", self.tool_service),
            ("proactive", self.proactive_service),
            ("cloud_sync", self.cloud_sync_service),
        ]:
            detail = {}
            running = slot is not None
            ready = False
            if running:
                try:
                    detail = slot.health()
                    ready = detail.get("backend", "") != "noop"
                except Exception:
                    pass
            services.append({
                "name": name, "running": running, "ready": ready,
                "extra": detail,
            })
        return {
            "initialized": self._initialized,
            "stopping": self._stopped,
            "startup_seq": list(self._startup_seq),
            "services": services,
            "user_robot_count": len(self.user_robots),
        }


# ═══════════════════════════════════════════════════════════════════
#  Singleton & compatibility accessors
# ═══════════════════════════════════════════════════════════════════

_runtime: Optional[RuntimeServices] = None
_runtime_lock = threading.Lock()


def get_runtime_services() -> RuntimeServices:
    """Return (or lazily create) the process-wide RuntimeServices singleton.

    Production: called early by main().
    Tests: inject via set_runtime_services().
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = RuntimeServices()
        return _runtime


def set_runtime_services(instance: RuntimeServices) -> None:
    """Inject a custom RuntimeServices (used by tests and main())."""
    global _runtime
    with _runtime_lock:
        _runtime = instance


# ── Backwards-compatibility accessors ──
# These let old code access services without changing every call site.
# New code should import get_runtime_services() and call the methods
# directly.

def get_default_robot() -> Optional[Any]:
    rt = get_runtime_services()
    return rt.default_robot


def get_asr_worker() -> Optional[Any]:
    return get_runtime_services().asr_worker


def get_vision_worker() -> Optional[Any]:
    return get_runtime_services().vision_worker


def get_vision_state() -> Dict[str, Any]:
    return get_runtime_services().get_vision_state()


def get_sensor_reader() -> Optional[Any]:
    return get_runtime_services().sensor_reader


def is_runtime_stopping() -> bool:
    return get_runtime_services().is_stopping()
