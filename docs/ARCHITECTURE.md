# Architecture

How the Nuanyu board runtime is put together: why one process owns the device, how
services are registered and shut down, what the supervisor restarts on, and which
engineering rules are non-negotiable.

Paths in this document are relative to the repository root, which is also the deployment
root: `deploy/`, `app/` and `config/` sit side by side. Set `NUANYU_ROOT` to deploy the
runtime elsewhere — the reference board installs the tree under `/userdata_fibo`.

---

## 1. The one-owner principle

Every peripheral this runtime touches is a resource that tolerates **exactly one
owner**. That constraint, not a preference for monoliths, is what shapes the whole
design.

| Resource | Sole owner | Why a second owner breaks it |
|---|---|---|
| Voice UART `/dev/ttyHS1` | the web process (`app/nuanyu_web.py`, `voice_loop`) | The offline wake-word module emits a raw 3-byte frame stream. A byte stream has no addressing: whichever reader wins a `read()` consumes the frame, so two readers steal frames from each other and the wake word turns into a coin flip. |
| Board speaker (ALSA) | `app/fibo_tts.py` | The headphone route is *global* mixer state. Two processes toggling it can close the route while the other is mid-playback, which produces silence even though `aplay` exits 0. The playback serialisation lock is also process-local, so a second process would interleave writes to the same PCM. |
| Camera `/dev/video2` | `app/src/vision/vision_worker.py` | A V4L2 capture device is exclusive: the second opener either fails or gets a corrupt, flapping stream. This was a real defect — a legacy frame-difference loop and `VisionWorker` both opened the camera. The legacy loop is now marked deprecated and is never started. |
| USB-CDC sensor node | `app/src/sensors/sensor_reader.py` | libusb claims the CDC data interface exclusively. |
| Board state as a whole | `deploy/start_nuanyu_runtime.sh` (supervisor) | Two supervisors would each restart "their" process. |

Two supporting decisions follow from this:

- **The server is `http.server`, not a web framework.** The runtime needs to open the
  UART, configure ALSA and hold the camera in the same process that serves HTTP;
  a framework brings a worker model of its own for no benefit here.
- **Exactly one supervisor.** There is one systemd unit (`deploy/nuanyu-runtime.service`)
  and one script. A second watchdog is explicitly forbidden — the recovery logic is
  already inside the supervisor loop, and two restarters produce restart storms.

---

## 2. The startup guard: two file locks

### 2.1 Process lock

`app/nuanyu_web.py` takes a non-blocking exclusive lock **before** importing or
initialising anything heavyweight:

```python
_INSTANCE_LOCK_FD = None
if __name__ == "__main__":
    import fcntl
    _INSTANCE_LOCK_FD = open("/run/nuanyu_web_5004.lock", "w")
    try:
        fcntl.flock(_INSTANCE_LOCK_FD.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[STARTUP] another Nuanyu web instance already owns port 5004", flush=True)
        raise SystemExit(73)
    _INSTANCE_LOCK_FD.write(str(os.getpid()))
```

Details that matter:

- The lock is taken only under `if __name__ == "__main__"`. Test modules import the
  module without becoming a second owner.
- The lock is taken *before* the heavy imports, so a rejected instance exits in
  milliseconds instead of loading models and then dying.
- The listening socket sets `SO_REUSEADDR` (clean restart after `TIME_WAIT`) but
  deliberately **never** `SO_REUSEPORT`. With `SO_REUSEPORT` the kernel load-balances
  connections across duplicate processes, so the AI state, sensor state and session
  cookies would appear to randomly flap between two half-initialised instances.
  `tests/test_single_instance_contract.py` asserts both properties.

### 2.2 Supervisor lock

The supervisor takes its own lock on `/run/nuanyu_runtime.lock` and exits with code
`75` if it is already held:

```sh
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] runtime already owned" >> "$LOG"
    exit 75
fi
```

So the two guards are independent: one supervisor owns the restart loop, one web
process owns the hardware. As a third layer the supervisor also `pkill`s stale
`python3 …/nuanyu_web.py` and `python3 …/l610_service.py` processes before starting a
fresh child.

---

## 3. The `RuntimeServices` registry

`app/src/core/runtime_services.py` holds the process-wide service graph. Its contract
is stated in the module docstring: every long-lived background worker is created,
owned and stopped by `RuntimeServices`, and **no other module may create a second
instance**.

Owned slots:

| Slot | Created by | Notes |
|---|---|---|
| `asr_worker` | `_create_asr_worker()` | skipped when `ASR_ENABLED=false` |
| `vision_worker` | `_create_vision_worker()` | sole camera owner |
| `deepseek_client` | `_create_deepseek_client()` | persistent HTTPS client, warmed up once |
| `tts_coordinator` | registered from `main()` | created lazily by the TTS backend |
| `sensor_reader` | `_create_sensor_reader()` | ESP32-S3, self-polling |
| `memory_service`, `persona_service`, `tool_service`, `proactive_service`, `cloud_sync_service` | `_load_phase2_services()` | real implementation with NoOp fallback |
| `default_robot`, `user_robots` | `register_default_robot()`, `ensure_user_robot()` | one `NuanyuCore` per account |

Creation order is enforced (see the docstring and `initialize()`):

1. load config
2. create shared services (ASR, Vision, DeepSeek; the TTS factory is registered)
3. create and register the default robot
4. load Phase-2 services (and wire the tool handlers)
5. start Vision and ASR
6. start the polling threads (`start_polling()`)
7. deliver any buffered ASR results

Shutdown is the reverse order.

### 3.1 Health

`health_snapshot()` returns a structured report — per service: `name`, `running`,
`ready`, `extra` — plus `initialized`, `stopping`, `startup_seq` and
`user_robot_count`. It is served by `GET /api/health`. "Running" and "ready" are
deliberately separate: a worker can exist while its model has not loaded.

### 3.2 Robot routing

- The default robot (`username=None`) is the only instance that starts hardware:
  `start()` gates `voice_loop`, the global ASR worker, the global TTS manager, the
  proactive loop and the C07A poll loop behind `if self.username is None`.
- Per-user robots exist so each account has its own chat history, memory and persona.
- `set_active_robot()` records which robot the authenticated web UI is showing, and
  the always-on ASR consumer (`_poll_asr_loop`) delivers recognised speech there, so a
  spoken turn and the web chat page share one `chat_history` and one latency pipeline.
  Only requests carrying a valid session change the active robot — anonymous probes
  (the mini-program, health checks) do not steal the voice route.
- If no robot is ready (early startup), recognised text is buffered in `_asr_pending`
  (max 8, oldest dropped and logged) and replayed by `flush_pending_asr()`. This
  replaces the old behaviour where ASR results were silently dropped while the robot
  was still `None`.

---

## 4. Ports and adapters

The Phase-2 services are split into abstract ports and concrete adapters.

```
app/src/ports/          abstract interfaces + data types (abc.ABC)
  memory.py             MemoryService
  persona.py            PersonaService, Persona, VoiceProfile
  tools.py              ToolService, ToolDefinition
  proactive.py          ProactiveService, ProactiveDecision
  cloud.py              CloudSyncService, SyncRecord, CloudCommand

app/src/services/       adapters
  memory_service.py     SqliteMemoryService | NoOpMemoryService
  persona_service.py    JsonFilePersonaService | NoOpPersonaService
  proactive_service.py  ProactiveServiceImpl | NoOpProactiveService
  tool_service.py       tool registry + builtin tools
  cloud_sync_service.py OfflineQueueCloudService | NoOpCloudSyncService
app/src/memory/         SqliteMemoryStore (WAL, keyword search, legacy JSON import)
app/src/persona/        per-user persona JSON config
app/src/domain/         shared event and model types (RuntimeEvent, ToolResult, …)
```

Two properties make the split useful rather than decorative:

- **NoOp fallback at load time.** `_load_phase2_services()` tries the real class and,
  on *any* exception, falls back to the NoOp implementation of the same port and logs
  `noop_fallback`. A broken SQLite file or a missing persona directory degrades one
  feature instead of breaking the voice pipeline. `health_snapshot()` marks a service
  `ready: false` when its backend reports `noop`, so a downgrade is visible.
- **The tool-calling loop has no dependency on the app.** `app/src/tools/tool_calling_coordinator.py`
  takes the DeepSeek client and the `ToolService` as constructor arguments and imports
  nothing from `nuanyu_web` or `RuntimeServices`. It can be exercised on its own, and a
  tool failure degrades to a plain reply.

Storage behind the adapters is per-user: conversation history and long-term memories in
SQLite (`app/data/nuanyu_memory.db`), persona configuration as JSON under
`app/data/personas/`, and legacy `app/memories/{username}.json` files are imported
idempotently.

---

## 5. Service lifecycle

### 5.1 Startup

`main()` in `app/nuanyu_web.py` runs the sequence below; the printed
`[STARTUP] RuntimeServices initialized: …` line echoes the actual order.

```
preload_tts()                     TTS model loaded before the server accepts traffic
rt.load_config()
ROBOT = NuanyuCore(username=None)  default robot created BEFORE ASR starts
rt.register_default_robot(ROBOT)
rt.initialize()                    ASR + Vision + DeepSeek + sensor + Phase-2
preload_asr()                      ASR model load (tens of seconds)
ROBOT.start()                      voice / proactive / C07A / study / schedule threads
rt.tts_coordinator = _get_stream_tts()
rt.start_polling()                 ASR consumer + Vision state poll
rt.flush_pending_asr()             deliver anything buffered during preload
load_users() -> restore persisted sessions -> pre-create a robot per account
ThreadingHTTPServer(...).serve_forever()   port 5004
```

The ordering constraint is that the default robot must exist **before** the ASR
consumer starts. That is the fix for the old "recognised but silently dropped" bug.

> Note: the step list in the `RuntimeServices` module docstring still describes the
> original Phase-1 wiring (initialize before the robot is created). The live order is
> the one in `main()` above; where the two disagree, `main()` wins.

### 5.2 Shutdown

`POST /api/shutdown` requires a valid session (otherwise any LAN client could kill the
process) and then runs `RuntimeServices.stop()` on a background thread. `stop()` is
idempotent — the second and later calls return immediately.

Reverse order, with the reason each step is where it is:

1. `stop_event.set()` — every worker loop watches this.
2. Join the polling threads (3 s timeout).
3. `vision_worker.stop()` — **releases `/dev/video2`**.
4. `asr_worker.stop()` — closes the capture stream.
5. `sensor_reader.stop()` — kills the libusb receiver subprocess.
6. `tts_coordinator.close()` — drains the speech queue.
7. `deepseek_client.close()` — closes the persistent HTTPS connection.
8. Close the Phase-2 services.
9. `robot.running = False` for every robot, then join remaining threads (2 s).

systemd then restarts the service (`Restart=always`). The supervisor also runs
`launch_display.sh stop` on its own `INT`/`TERM`/`HUP` trap, so the HDMI renderer does
not outlive a stopped runtime.

---

## 6. Thread model

The process is multi-threaded, not multi-process, and every `Thread` is a daemon so a
wedged worker can never block interpreter exit.

| Thread | Created by | Job |
|---|---|---|
| main | `main()` | `serve_forever()` |
| one per HTTP request | `ThreadingMixIn` (`daemon_threads = True`) | request handling |
| `ASRWorker` | `app/src/asr/asr_worker.py` | mic capture → VAD → transcription loop |
| `ASRPoll` | `RuntimeServices.start_polling()` | consume transcripts, route to a robot (50 ms poll) |
| `VisionWorker` | `app/src/vision/vision_worker.py` | camera capture, face detection, FER, smoothing |
| `GVisionPoll` | `RuntimeServices.start_polling()` | publish the latest vision state (100 ms poll) |
| `StreamingTTS` | `app/src/tts/streaming_pipeline.py` | ordered speech-queue consumer |
| `DrizzleSynth` / `DrizzlePlay` | `app/src/tts/drizzle_backend.py` | synthesis / playback pipeline |
| `DoubaoQ` | `app/src/tts/doubao_provider.py` | cloud TTS queue |
| `SurgeLiteQ`, `SurgeLiteSynth<n>` | `app/src/tts/surge_lite_provider.py` | host-ZipVoice queue + synth workers |
| `FiboTTSSDK` | `app/fibo_tts.py` | the *quarantinable* vendor DSP call (see §7) |
| `VoiceUART` | `NuanyuCore.start()` | reads the SU-03T frame stream |
| `ProactiveDefault`, `C07APoll` | `NuanyuCore.start()` (default robot only) | proactive rules, motion polling |
| `study_timer_loop`, `schedule_loop` | `NuanyuCore.start()` | focus timer, reminders (one per robot) |
| `MusicPlayback` | `nuanyu_web.py` | long-form audio playback |
| `SensorWatchdog` | `app/src/sensors/sensor_reader.py` | supervises the receiver subprocess with backoff |
| L610 | `app/src/connectivity/l610_service.py` | **separate OS process** started by the supervisor |

Conventions:

- **Thread crashes do not kill the process.** `threading.excepthook` appends a
  traceback to `/userdata_fibo/crash.log` and returns; the process stays up.
- **Half duplex.** ASR is muted while TTS plays (`set_system_speaking` / the mic gate),
  so the speaker's own voice is never transcribed and fed back to the LLM. The mute is
  held for `ASR_MIC_SETTLE_MS` (default 1000 ms) after the *last* WAV of a reply.
- **One lock per singleton** (`_asr_lock`, `_vision_lock`, `_deepseek_client_lock`,
  `_drizzle_backend_lock`, `_tts_backend_lock`, `_sleep_lock`, `SESSION_LOCK`, …), plus
  per-instance locks on the robot. Lock ordering is kept one-way: `c07a._lock` →
  `web.lock`, so motion triggers are always issued **outside** `with self.lock:`.
- **Nothing blocking on the status path.** `/api/status` is polled continuously by the
  supervisor and the UI, so it must never block on the DeepSeek lock or the TTS
  condition — a stalled status path is what the supervisor's probe failures measure.
- The supervisor's 120-thread ceiling (§7) is the backstop for a leak in any of these
  loops.

---

## 7. The supervisor

`deploy/start_nuanyu_runtime.sh` is the single supervisor. It sources the environment,
resolves the ALSA card, then loops forever:

```
start child  →  wait for readiness (port 5004 listening, ≤ 90 s, 2 s steps)
             →  start the HDMI display, asynchronously
             →  poll every 3 s, killing the child on any of four triggers
             →  restart after 3 s (15 s once a crash loop is detected)
```

### 7.1 Restart triggers

| # | Trigger | Measurement | Threshold | Why |
|---|---|---|---|---|
| 1 | RSS ceiling | `VmRSS` from `/proc/<pid>/status` | > 5 242 880 KB (≈ 5 GiB) | A normally loaded process sits near 3.7 GB. A slow leak or an SDK anomaly past 5 GB would drag down the whole board, so the runtime is restarted before the system OOMs. |
| 2 | `/api/status` probe failures | `curl -fsS --max-time 3 http://127.0.0.1:5004/api/status`, every 3 s | 3 consecutive failures | A wedged request lock used to leave the listening socket alive while every request piled up forever, so "port open" was not a liveness signal. Probing the real status path is. A single success resets the counter. |
| 3 | Thread count | count of subdirectories under `/proc/<pid>/task` | > 120 | Last-resort guard against a thread leak reaching resource exhaustion. |
| 4 | TTS DSP timeout sentinel | existence of `/userdata_fibo/.nuanyu_tts_unhealthy` | present | The vendor DSP call cannot be cancelled safely. When `SpeechSynthesisSync` exceeds `FIBO_TTS_SYNTH_TIMEOUT` (default 12 s) the synthesis thread is quarantined, the SDK is marked failed so later calls fast-fail, and this sentinel is written. Only a clean process restart clears the quarantined SDK, so the supervisor performs one. |

### 7.2 Crash-loop cooldown

The child is restarted unconditionally, so a genuinely broken build cannot be
throttled by "give up": instead a `failures` counter is incremented on every exit and
reset to `0` the moment the child reaches readiness.

```sh
failures=$((failures + 1))
if [ "$failures" -ge 3 ]; then
    log "crash loop detected; cooling down 15s"
    sleep 15
else
    sleep 3
fi
```

So a process that starts successfully and later dies is restarted in 3 s, repeated
startup failures back off to 15 s, and any successful startup returns to the fast path.

### 7.3 Other supervisor duties

- Rotate `/userdata_fibo/nuanyu_runtime.log` to `.log.1` once it exceeds 8 MiB.
- `chmod 666 /dev/ttyHS1` and `/dev/video*` (the board image does not guarantee the
  runtime group these devices).
- Clear leftover state markers on every start — `.nuanyu_stop`, `.nuanyu_sleeping`,
  `.nuanyu_quiet`, `.nuanyu_tts_unhealthy`. Otherwise a process that died while the
  assistant was "asleep" would come back asleep and `handle_chat` would return empty,
  so the chat history would look broken.
- Write the child PID to `/userdata_fibo/nuanyu_web.pid` for external checks.
- Start the L610 4G service as its own process when the module exists.
- On `INT`/`TERM`/`HUP`, stop the children (SIGTERM, 1 s grace, then SIGKILL), remove
  the PID file and stop the display.

The unit wraps the script in `/bin/sh` on purpose, because files on the `/userdata`
filesystem can lose their executable bit across a re-sync:

```ini
ExecStart=/bin/sh /userdata_fibo/deploy/start_nuanyu_runtime.sh
Restart=always
RestartSec=3
TimeoutStopSec=12
KillMode=control-group
```

---

## 8. HTTP route surface

One handler class serves everything. Pages are server-rendered templates; the front end
is plain HTML/CSS/JS with no build step. **State reaches the UI by polling, not by
push**: the main page calls `GET /api/status` every 900 ms (4000 ms while the tab is
hidden) and `GET /api/sensors` every 2 s; the HDMI renderer polls `/api/status` at
400 ms. `POST /api/chat` is the only request that blocks for the length of an AI turn.

### Pages and assets

| Method | Path | Notes |
|---|---|---|
| GET | `/` | login page or main page, depending on the session cookie |
| GET | `/display` | the HDMI face page (no login; it is a kiosk surface) |
| GET | `/static/*` | UI CSS/JS |
| GET | `/assets/*` | hand-drawn character SVG assets |

### Session and accounts

| Method | Path | Notes |
|---|---|---|
| POST | `/api/login` | returns `503` while the ASR model is still loading |
| GET | `/api/logout` | |
| GET | `/api/whoami` | |
| GET | `/api/users` | user list, never hashes |

### Status and health

| Method | Path | Notes |
|---|---|---|
| GET | `/api/status` | the full state document; polled by UI and supervisor |
| GET | `/api/health` | `RuntimeServices.health_snapshot()` |
| GET | `/api/tts_status` | live TTS state (queue depth, last error) |

### Conversation and device control

| Method | Path | Notes |
|---|---|---|
| POST | `/api/chat` | text turn: memory context → DeepSeek stream → segmented TTS |
| POST | `/api/command` | sleep / wake / quiet / study and the other voice-equivalent commands |
| POST | `/api/mood` | self-reported mood, feeds persona context |
| POST | `/api/tts` | speak a given string |
| POST | `/api/asr` | transcribe an uploaded clip |
| POST | `/api/mic_control` | manual mic mute (get or set) |
| POST | `/api/shutdown` | session required; graceful stop, systemd restarts |

### TTS backends and voices

| Method | Path | Notes |
|---|---|---|
| POST | `/api/tts_provider` | switch between `drizzle`, `stream`, `surge` |
| GET | `/api/tts_audio` | most recent TTS WAV (`/tmp/nuanyu_tts_output.wav`) |
| GET/POST | `/api/stream/voices`, `/api/stream/voice`, `/api/stream/preview` | Doubao voice selection and preview |
| GET | `/api/surge/audio/{id}` | a voice-pack WAV |
| POST | `/api/surge/voices`, `/api/surge/voices/{id}/(activate|preview|delete)`, `/api/surge/upload`, `/api/surge/health` | voice-pack management (requires the host ZipVoice server) |

### Perception and sensing

| Method | Path | Notes |
|---|---|---|
| GET | `/api/sensors` | latest sensor snapshot + health |
| GET | `/api/weather` | keyless Open-Meteo lookup; returns `ok: false` rather than fabricating |

### Phase-2 service inspection

| Method | Path | Notes |
|---|---|---|
| GET | `/api/persona`, `/api/memory/stats`, `/api/tools`, `/api/proactive/status`, `/api/cloud/status` | read-only service state |
| POST | `/api/persona/update`, `/api/proactive/config`, `/api/proactive/demo`, `/api/cloud/command/ack` | configuration and downlink acknowledgement |

The `cloud`, `proactive` and `tools` endpoints require a valid session and return `401`
otherwise. Surge endpoints report a degraded (not fake-OK) status when the host server
is unreachable.

---

## 9. Engineering rules

These are the load-bearing lessons from this project. Each one cost a debugging
session; none of them is stylistic.

### Never hardcode ALSA card numbers

USB devices are enumerated at boot and re-enumerated on replug, so card indices are not
stable. On this board card 0 and card 1 can be USB capture devices, which means a
hardcoded `hw:1,0` may one day point at a microphone. The supervisor therefore resolves
the Qualcomm playback card **by name** on every launch and exports the result:

```sh
detected_hph_card="$(awk '/lahaina-yupikiot/{gsub(/[^0-9]/, "", $1); print $1; exit}' /proc/asound/cards 2>/dev/null)"
if [ -n "$detected_hph_card" ]; then
    export FIBO_TTS_HPH_CARD="$detected_hph_card"
else
    export FIBO_TTS_HPH_CARD="${FIBO_TTS_HPH_CARD:-2}"
fi
```

The fallback exists only so a machine without the card can still start; it is not a
license to hardcode. Mixer operations use the same variable. See `docs/HARDWARE.md`.

### One camera owner

`VisionWorker` is the only component that opens `/dev/video2`. The legacy
frame-difference loop in `NuanyuCore` remains in the tree for reference, is decorated
as deprecated and **must not be started**; two openers produce an unstable vision state
and a flapping `visual_state`. `/api/status` derives `camera_ok` from
`VisionWorker._running`, not from a cached flag.

### Board-first audio routing

Normal conversation, all three TTS backends and voice previews must come out of the
board speaker.

- The board path is tried first; the host PC speaker (port 5015) is an emergency bridge
  only and must not be reachable from normal business code.
- All backends share one playback path — `fibo_tts._play_wav()` or the equivalent in
  `app/src/tts/audio_router.py`. Never let a backend configure the mixer itself: two
  conflicting mixer configurations is exactly the failure this rule prevents.
- A browser-side `new Audio()` must never play business speech; it would emit from the
  host PC's speakers.
- **`aplay` returning 0 only proves ALSA accepted the PCM.** It does not prove sound
  left the jack. Acceptance of audio is a human check with a speaker connected.

### Never fake a health status

- No "online" constants, no suppressed error paths, no fabricated latency numbers.
  Reported first-audio latency is measured from submission to playback.
- Hardware availability and service availability are reported separately, so "the AI
  is up but the camera is missing" is expressible.
- A failed component must degrade visibly. Sensor absence, camera failure and an
  unreachable host TTS server are all non-fatal, and each one is supposed to make the
  UI say so rather than silently pass.

> **Resolved.** An earlier revision of `app/src/sensors/sensor_state.py` violated the
> rule above: `SENSOR_SIM_FALLBACK` (default on) substituted plausible readings while
> the node was disconnected or stale, and `_radar_forced_on()` reported "radar online,
> person present" whenever the radar was silent. Both existed to keep a demo screen
> from ever showing "offline". They have been **removed** — the class now returns only
> what the hardware actually sent, and `health()["available"]` marks which fields were
> really received. See `tests/test_sensor_state.py`, which fails if either returns.

---

## 10. Notes on stale internal documentation

The repository's internal maintenance notes are Chinese and partly superseded. Where
they disagree with the code, the code is authoritative. Known conflicts:

| Stale statement | Current reality |
|---|---|
| The startup chain is `start_xiaopei_robust.sh` → `start_xiaopei.sh` → `xiaopei-watchdog.service` | Those scripts are gone. `deploy/start_nuanyu_runtime.sh` is invoked directly by `deploy/nuanyu-runtime.service`. Enabling the old watchdog unit is forbidden. |
| The web runtime is `xiaopei_web_v3.py` | `app/nuanyu_web.py`. |
| Only `tts_stream.env` is sourced at startup | There is no `tts_stream.env` in the tree. The supervisor sources `config/nuanyu.env` (template: `config/nuanyu.env.example`), plus `config/c07a_motion.env` when present. |
| A camera/audio failure should restart the runtime | Camera, sensor and host-service failures are non-fatal and never restart anything. |
| The ASR result consumer polls every 200 ms | The loop now sleeps 50 ms; a 200 ms poll only made ASR feel slower. |
| `deploy/start_nuanyu_runtime.sh` restores the USB capture gain and enables AGC on every start | The published script does neither. The ASR worker re-applies `Mic 100%` at most once per 15 s, which is what makes mic hot-plug recover without a restart. No AGC control is set anywhere in this repository. |
| The front end receives state over SSE (stated in the repository README) | It polls `/api/status`. There is no SSE or WebSocket endpoint in the runtime. |
| `AUDIO_OUTPUT_MODE=board` is the formal policy | The supervisor default is `auto`, which means board-first with a host fallback only after board playback fails. Both are board-first in practice; `board` is the stricter setting. |
| Ports 5016 and 5017 for the host services | The live defaults are 5018 (ZipVoice) and 5019 (cloud proxy); 5016 is the camera fallback and 5015 the emergency speaker bridge. `fibo_tts.py` still carries a legacy `FIBO_PIPER_SERVER_URL` default pointing at `5016/speak`, which would collide with the camera fallback — it is inert unless a Piper-style engine is explicitly selected. |
