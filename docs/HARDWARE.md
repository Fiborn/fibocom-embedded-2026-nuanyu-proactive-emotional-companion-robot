# Hardware

What the runtime talks to, how each device is addressed, and which process owns it.

The guiding rule for everything below: **device names are stable, device numbers are
not.** Where a peripheral can be resolved by name, it is resolved by name at every
launch. Where it cannot, the interface is documented here so the assumption is visible.

Paths are relative to the deployment root, which is the repository root unless
`NUANYU_ROOT` says otherwise; the reference board sets it to `/userdata_fibo`.

---

## Board and OS

| Item | Value |
|---|---|
| Board | Fibocom SC171V3 |
| SoC | Qualcomm QCS6490 (aarch64) |
| Accelerator | Hexagon NPU/DSP, used by the Fibocom ASR/TTS SDKs |
| OS | Ubuntu-based vendor image |
| Python | 3.8 (`/usr/local/lib/python3.8/dist-packages` is added to `sys.path`) |
| Vendor SDK | `fiboaisdk` (`api_aisdk_py`), plus the Fibocom TTS model directory |
| SDK licence | `/home/fibo/qcom_6490_license` — `key1.pem`, `key2.pem`, `key3.pem`, `license.bin` |
| Storage discipline | Only `/userdata_fibo` is written. The system partition is read-only for this project. |

If the licence directory or its files are missing, the vendor TTS engine does not
initialise; `app/fibo_tts.py` logs the missing path and the drizzle backend's Matcha
worker (which does not need the vendor SDK) still works.

Application state:

| Path | Contents |
|---|---|
| `/userdata_fibo/nuanyu_runtime.log` | supervisor + service log (rotated at 8 MiB) |
| `/userdata_fibo/nuanyu_web.pid` | PID of the supervised web process |
| `/userdata_fibo/crash.log` | thread crash tracebacks |
| `/userdata_fibo/l610.log` | 4G service log |
| `/userdata_fibo/display.log`, `display.pid` | HDMI renderer |
| `/userdata_fibo/.nuanyu_sleeping`, `.nuanyu_stop`, `.nuanyu_quiet` | interaction state markers (cleared at every start) |
| `/userdata_fibo/.nuanyu_tts_unhealthy` | DSP-timeout sentinel read by the supervisor |
| `/tts_output`, `/tmp/nuanyu_tts_output.wav`, `/tmp/drizzle_matcha`, `/tmp/drizzle_cache` | TTS scratch and the current WAV |

---

## Audio output — the board speaker

### Card resolution

The output is the Qualcomm analog headphone (HPH) path on the
`lahaina-yupikiot-snd-card`. **Card numbers move when USB devices are plugged or
unplugged** — on this board card 0 and card 1 can be USB capture devices, so the
Qualcomm `MultiMedia1` playback PCM may be card 1, 2 or something else. The supervisor
therefore resolves the card by name on every launch:

```sh
detected_hph_card="$(awk '/lahaina-yupikiot/{gsub(/[^0-9]/, "", $1); print $1; exit}' /proc/asound/cards 2>/dev/null)"
if [ -n "$detected_hph_card" ]; then
    export FIBO_TTS_HPH_CARD="$detected_hph_card"
else
    export FIBO_TTS_HPH_CARD="${FIBO_TTS_HPH_CARD:-2}"
fi
export FIBO_TTS_HPH_DEVICE="${FIBO_TTS_HPH_DEVICE:-0}"
```

`app/fibo_tts.py` defaults to card 2 / device 0 so the module is usable standalone, but
the environment variable always wins in production. Playback is a plain `aplay` to
`hw:<card>,0` with a 512-frame period.

### Mixer route

The headphone path is not open by default. It is opened by writing a sequence of
`amixer` controls (`amixer -q -c <card> cset numid=<n> <value>`) and closed by the
reverse sequence. The controls that matter:

| Control | Value when open | Effect |
|---|---|---|
| `numid=90,91` (AIF1_PB) | `AIF1_PB` | audio interface playback routing |
| `numid=112,115` | `RX0` / `RX1` | receiver path selection |
| `numid=107,108` | `CLSH_DSM_OUT` | class-H DSM outputs |
| `numid=137,138,244,245,269,270,520` | `on` | codec blocks and PA |
| `numid=243` | **`CLS_H_HIFI`** | class-H amplifier mode |
| `numid=5499` | `8192` (stream volume) | output level, `FIBO_TTS_HPH_STREAM_VOLUME` |

`numid=243` is the one that must not be omitted. Left at `CLS_H_INVALID`, ALSA accepts
every frame, `aplay` exits 0 and the physical headphone pins stay completely silent —
this is the single most misleading failure mode in the audio path. Any mixer
troubleshooting should start there.

The route is opened **once** at startup (`hph_always_on_init()`) and left open, so the
first sentence of a reply pays no route-switching latency. The cost is that the analog
path stays live for the process lifetime; `hph_always_on_close()` closes it.

### Ownership rules

- All three TTS backends share this one playback path. A backend must never configure
  the mixer itself — that is how two conflicting configurations produce intermittent
  silence.
- Playback is serialised by a process-local lock with a 5 s acquire timeout; if the
  lock is busy the clip is skipped rather than queued behind a wedged `aplay`.
- `AUDIO_OUTPUT_MODE` selects `auto` (board first, host fallback only after board
  playback fails), `board` (board only) or `pc`. The board-first release runs `auto`;
  the stricter formal policy is `board`. The host bridge on port 5015 is an emergency
  path and normal business code must not call it.
- **A successful `aplay` is not evidence of sound.** Verify with a speaker connected.

Environment variables: `FIBO_TTS_HPH_CARD`, `FIBO_TTS_HPH_DEVICE`,
`FIBO_TTS_HPH_STREAM_VOLUME`, `AUDIO_OUTPUT_MODE`, `PC_SPEAKER_URL`,
`FIBO_TTS_PC_ONLY`, `SURGE_PC_SPEAKER`.

---

## Audio input — the USB microphone

| Item | Value |
|---|---|
| Default device | `plughw:CARD=Device,DEV=0` (`ASR_MIC_DEVICE`) |
| Mixer card | `Device` (`ASR_MIC_CARD`) |
| Format | 16 kHz, mono, S16_LE, read in 30 ms frames (640 bytes) |
| Owner | `app/src/asr/asr_worker.py` |

### Gain restore on hot-plug

A USB capture device can come back from a replug with its capture volume at 0%. That
failure is silent in the worst way: the ASR worker keeps reporting `listening` and
`stats.total` climbs, but nothing is ever recognised. The worker therefore re-applies
gain from its capture loop, at most once per 15 s:

```python
subprocess.run(
    ["amixer", "-q", "-c", MIC_CARD, "sset", "Mic", "100%", "cap"],
    timeout=3, check=False, ...)
```

This is what makes unplug/replug recover without restarting the service. Note that the
published supervisor script does **not** perform this restore itself, and no AGC
control is set anywhere in this repository, despite both being described in the
internal maintenance notes — see §9.

### Source selection

`ASR_MIC_SOURCE` chooses where PCM comes from:

| Value | Source | Requirement |
|---|---|---|
| `board` | `arecord` on `ASR_MIC_DEVICE` | USB mic attached |
| `remote` | `GET /mic` on the host PC | `adb reverse tcp:5002 tcp:5002` and `tools/pc_mic_server.py` running |
| `auto` (**default**) | board first; falls back to `remote` if `arecord` fails to start | one of the two |

The remote stream is 16 kHz / 16-bit / mono raw PCM, chunked, with an 8 s socket
timeout (`ASR_REMOTE_MIC_URL`, `ASR_REMOTE_MIC_TIMEOUT`). The board runs its own VAD
over it, exactly as over the local mic; the host side does no processing. The reported
microphone source (`board` / `remote` / `none`) is part of the ASR health payload, so
"the mic is working" can always be traced to a concrete source.

### VAD characteristics

| Parameter | Default | Env |
|---|---|---|
| Speech threshold (RMS) | 180 | `ASR_SILENCE_THRESHOLD` |
| Silence tail | 300 ms | `ASR_SILENCE_TAIL_MS` |
| Pre-roll kept before onset | 300 ms | — (fixed) |
| Onset confirmation | 90 ms | `ASR_ONSET_CONFIRM_MS` |
| Minimum utterance | 400 ms | — (fixed) |
| Minimum audio handed to the SDK | 2000 ms (zero-padded) | `ASR_SDK_MIN_AUDIO_MS` |
| Max utterance / max listen | 8 s / 30 s | `ASR_MAX_UTTERANCE_SEC`, `ASR_MAX_LISTEN_SEC` |
| Mic settle after playback | 1000 ms | `ASR_MIC_SETTLE_MS` |

The 300 ms pre-roll and the 2 s padding exist so the first phoneme of a wake word is
never clipped and so the recognition SDK is not handed an utterance shorter than its
minimum. The noise floor is calibrated from the median of the first ~10 frames rather
than a single frame, because a single quiet first frame made USB-mic noise look like
speech and produced a run of false transcriptions.

---

## Camera

| Item | Value |
|---|---|
| Board camera | `/dev/video2`, V4L2 |
| Capture format | 640×480 colour, buffer size 1, three warm-up frames discarded |
| Owner | `app/src/vision/vision_worker.py` (`VisionWorker`) |
| Fallback | `GET http://127.0.0.1:5016/camera.jpg` (`CAMERA_FALLBACK_URL`) |

`VisionWorker` is the **only** component that opens the camera. The legacy
frame-difference loop that used to open it as well is deprecated and is never started;
two openers produce an unstable vision state.

The fallback is a host-side JPEG: the board fetches one image per frame with a 2 s
timeout and a 2 MiB size cap, decodes it with OpenCV, and tags the frame source as
`pc_fallback`. It requires `adb reverse tcp:5016 tcp:5016` and a JPEG server on the
host. Reads are deliberately connection-closed per request, because a host camera
bridge is simpler to keep stateless than to keep alive.

Source reporting: `_camera_source` is one of `board`, `pc_fallback` or `none`, and
`/api/status` exposes `camera_ok` derived from the worker actually running.
**Camera failure is non-fatal** — the AI, TTS and ASR pipelines start and run normally
with no camera at all; the UI reports the degraded state instead.

On-board processing: face detection (Haar cascade, every 5th frame, downscaled to
320 px) and expression recognition (64×64 greyscale ONNX model, 7 classes, Chinese
labels), smoothed over time to stop the displayed expression flickering. The worker
also maintains a sticky person-presence flag with hysteresis (4 s hold, 8 s exit), so a
brief head turn does not immediately flip the robot to "away".

Configurable via `CAMERA_DEVICE`, `CAMERA_FALLBACK_URL`, `VISION_ENABLED`,
`FER_ENABLED`.

---

## Sensor node — ESP32-S3 over USB CDC

An ESP32-S3 node reports ambient conditions and mmWave radar output over USB CDC.

| Item | Value |
|---|---|
| Transport | USB CDC/JTAG serial via libusb |
| ESP32-S3 USB IDs | VID `0x303A`, PID `0x1001`, bulk IN endpoint `0x81` |
| Alternate (older node) | CH343 bridge, VID `0x1A86`, PID `0x55D3`, endpoint `0x82` |
| Baud | 115200 (`SENSOR_BAUD`) |
| Supervisor | `app/src/sensors/sensor_reader.py` → subprocess `app/src/sensors/sc171_usb_receiver.py` |
| State | `app/src/sensors/sensor_state.py` |

Design points:

- **The USB I/O runs in a subprocess.** A libusb fault would otherwise be able to take
  down the whole runtime; isolating it means the worst case is a dead receiver that the
  watchdog restarts. The parent only parses JSON Lines from the child's stdout.
- **Supervised with exponential backoff**: a clean exit restarts immediately, a failure
  restarts after 2 s doubling up to 60 s (`SENSOR_INITIAL_BACKOFF`,
  `SENSOR_MAX_BACKOFF`). Each receiver invocation is run for up to 24 h
  (`SENSOR_RUN_SECONDS`).
- **Malformed input is skipped, never fatal.** Lines are accepted as plain JSON or with
  the receiver's `[RX] ` diagnostic prefix; a line without all required keys is dropped
  and counted.

Payload — the node must send every one of these top-level keys or the line is rejected:

```json
{
  "light": 320,
  "air_ppb": 88,
  "temperature_c": 26.5,
  "humidity_rh": 55.0,
  "radar": {
    "online": true, "presence": true, "motion": 0,
    "heart_bpm": 72, "breath_bpm": 16, "risk_level": 1,
    "valid_frames": 1200, "bad_headers": 0, "bad_payloads": 0
  }
}
```

A reading is treated as stale after 120 s (`DATA_STALE_AFTER_SECONDS`). `null` fields
are normalised to zero and marked *unavailable* rather than being reported as a real
zero. The radar object carries the mmWave module's own liveness, so "no one is in the
room" and "the radar is not reporting" stay distinguishable.

The sensor readings are also compiled into the LLM context
(`app/src/sensors/ai_context.py`): only fields the node actually reported are offered
to the model, with an explicit instruction not to invent the rest.

**Sensor absence is explicitly non-fatal** and must never trigger a restart.

> The offline node, and an online node whose mmWave radar is silent, are both reported
> truthfully as offline. An earlier revision fabricated plausible readings and forced
> radar presence on; that has been removed as fabricated health. See
> `docs/ARCHITECTURE.md` §9 and `tests/test_sensor_state.py`.

Configurable via `SENSOR_ENABLED`, `SENSOR_BAUD`, `SENSOR_RUN_SECONDS`.

---

## HDMI display

| Item | Value |
|---|---|
| Panel | 5" HDMI |
| Rendering | native Wayland client, `app/src/display/expression_display.py` (cairo) |
| Launcher | `app/src/display/launch_display.sh` |
| Compositor | the system Weston session (`init_display.service`), socket `/run/user/root/wayland-0` |
| Data source | `GET /api/status` on `127.0.0.1:5004`, polled at 400 ms |

Notes that explain why this is not a web page in a kiosk browser:

- The board runs **weston 8.0.0**, whose xdg-shell stable implementation is incomplete
  (resize is missing arguments). Firefox, GTK 3.24 and WebKit2GTK clients all crash on
  protocol errors against it. The renderer therefore speaks **`wl_shell`**, weston 8's
  native protocol, and draws directly with cairo.
- The board's `libwayland-client` is a stripped build without the generated protocol
  stubs, so the renderer marshals Wayland requests itself through
  `wl_proxy_marshal*` and works around several quirks of that build (no
  `wl_display_sync`, and one extra vararg consumed for object-id arguments).
- **One compositor only.** Weston uses the SDM backend and only one process may hold the
  SDM output, so the launcher never starts a second Weston — it attaches to the existing
  session. `/etc/xdg/weston/weston.ini` must have the HDMI output enabled
  (`mode=on` for `DP-1` or `DSI-1`).
- The launcher starts only when an output is actually connected (it reads
  `/sys/class/drm/card0-DP-1/status` and `card0-DSI-1/status` and requires exactly
  `connected`) and after port 5004 is up, then supervises the renderer by PID. Because
  it is a separate process, the face keeps animating independently of the web service;
  it is stopped by the supervisor's own trap when the runtime stops.

`GET /display` serves a lightweight HTML face page for the same panel and is usable as a
fallback surface (no login required).

---

## UARTs and their owners

| Device | Baud | Peripheral | Owner |
|---|---|---|---|
| `/dev/ttyHS1` | 9600 (`VOICE_BAUD`) | offline voice module (wake word + fixed commands) | the web process (`voice_loop`) |
| `/dev/ttyUSB0`…`/dev/ttyUSB6` | 115200 | L610 4G composite (AT) | the L610 service process |
| `/dev/ttyHS6` | 115200 | L610 physical UART fallback | the L610 service process |
| `<motion UART>` | 115200 | C07A chassis controller | the default robot only, and only when configured |

**Voice module (`/dev/ttyHS1`).** A hard-wired wake-word accessory sends a 3-byte frame
(`0xA5`, command, `0x5A`) for recognition events; the web process parses the stream,
maps frames to commands and ignores unknown ones. It is muted whenever the assistant is
asleep or in quiet mode, with the offline microphone wake fallback as a safety net so a
stalled accessory cannot trap the app asleep. The supervisor `chmod 666`s the device at
startup.

**L610 (`/dev/ttyUSB*` / `/dev/ttyHS6`).** The module enumerates as a USB composite
device with several virtual serial ports of which only one carries AT commands; the
service probes the candidates in order with a 1.5 s `AT` handshake and takes the first
that answers. The physical UART is the last fallback. See §"Optional 4G module".

**C07A motion controller.** Motion feedback is driven over its own UART on a
separate port from the voice module — reusing the voice port is forbidden. The port is
deliberately unset by default, and the client is inert (`MOTION_FEEDBACK_ENABLED=false`,
`C07A_MOTION_MOCK=true`) until the wiring, voltage levels and common ground have been
verified on the bench and a single-command `PING`/`ACT:STOP`/`ACT:GREET` round trip
passes. Motion triggers are best-effort and never block a conversation. Only the default
robot instance is allowed to open the motion port, so pre-created per-user robots cannot
claim it as well.

The motion client is optional in the strongest sense: with no port configured, every
call is a no-op and nothing in the voice, vision or chat paths changes.

---

## Optional 4G module

| Item | Value |
|---|---|
| Module | L610 (LTE), AT-command driven |
| Host entity | Huawei Cloud IoTDA (device/property model) |
| Command set | `AT+HMCON` (connect), `AT+HMPUB` (publish), `+HMREC` URC (receive) |
| Service | `app/src/connectivity/l610_service.py` |
| Started by | the supervisor, as a separate process, if the module file exists |
| Report interval | 60 s |
| Reconnect delay | 10 s |

The module is **inactive unless the hardware is attached**. Endpoint identifiers — broker
host/port, device ID and the device secret — all come from the environment or
`config/l610.env` (`L610_BROKER_HOST`, `L610_BROKER_PORT`, `L610_DEVICE_ID`,
`L610_DEVICE_SECRET`); the repository ships only a placeholder template. When no
AT-responsive port is found the service logs a clear message and exits rather than
retrying forever; when the environment is not configured it refuses to start. Use
`config/l610.env.example` as the template and never commit the populated file.

Telemetry is a device-status payload (timestamp + online) published on a fixed service
identifier. The module is independent of the voice pipeline: losing 4G degrades nothing
that runs on the board.

---

## ADB port tunnels

The board is reached over USB with `adb`. Only `5004` is forwarded to the host; every
other port is a **reverse** tunnel, i.e. a host service that the board dials as
`127.0.0.1` on the board itself.

| Port | Direction | Host-side endpoint | Purpose |
|---|---|---|---|
| **5004** | `adb forward` | board `:5004` | the Nuanyu web service. The host browser and the desktop shell reach it as `http://127.0.0.1:5004`. |
| **5002** | `adb reverse` | `GET /mic` | host microphone bridge (`tools/pc_mic_server.py`). The board reads 16 kHz/16-bit/mono raw PCM from it when `ASR_MIC_SOURCE` is `remote` or when the board mic fails under `auto`. |
| **5002** | `adb reverse` | `POST /transcribe` | legacy host ASR endpoint. It shares port 5002 with the mic bridge but is a different service on a different path — only one of the two can hold the port on the host at a time. |
| **5015** | `adb reverse` | `POST /play` | emergency host-speaker bridge (`PC_SPEAKER_URL`). Reachable, but the board-first release must not use it for normal business speech. |
| **5016** | `adb reverse` | `GET /camera.jpg` | host camera JPEG fallback (`CAMERA_FALLBACK_URL`). One image per frame, non-fatal if absent. |
| **5018** | `adb reverse` | ZipVoice server (`GET /health`, `GET /status`, `POST /tts`, voice activate/preview) | the `surge` TTS backend. The host runs `tools/zipvoice_server_v2.py` (sherpa-onnx, CPU, 3 diffusion steps) — `ZIPVOICE_SERVER_URL`. |
| **5019** | `adb reverse` | cloud proxy | DeepSeek (`DEEPSEEK_BASE_URL`, `POST /v1/chat/completions`) and Doubao TTS (`DOUBAO_TTS_API_URL`, default path `/doubao/tts`). The reference deployment points both at the host proxy; the code falls back to the public `https://api.deepseek.com` when the variable is unset, so a board with its own internet path can call the cloud directly. |

Not an ADB tunnel: **port 5005** is a LAN gateway opened by the desktop shell on the
host so a phone running the mini-program can reach the board's `/api/*`. It is
implemented in the desktop launcher, not in this repository, and it forwards API routes
only. Port 5004 is a host loopback port created by `adb forward` and is not reachable
from another device.

Example manual setup:

```bash
adb forward  tcp:5004 tcp:5004     # board web service
adb reverse  tcp:5002 tcp:5002     # host mic bridge / legacy ASR
adb reverse  tcp:5015 tcp:5015     # emergency host speaker bridge
adb reverse  tcp:5016 tcp:5016     # host camera fallback
adb reverse  tcp:5018 tcp:5018     # host ZipVoice (surge)
adb reverse  tcp:5019 tcp:5019     # cloud proxy (DeepSeek + Doubao)
```

Changing any of these requires updating the desktop launcher, the board environment
variables, the health checks and this document in the same change.

---

## Environment variable reference

| Variable | Default | Used for |
|---|---|---|
| `FIBO_TTS_HPH_CARD` / `_DEVICE` | resolved by name / `0` | board playback device |
| `FIBO_TTS_HPH_STREAM_VOLUME` | `8192` | mixer `numid=5499` |
| `AUDIO_OUTPUT_MODE` | `auto` | `auto` / `board` / `pc` routing |
| `ASR_MIC_DEVICE` | `plughw:CARD=Device,DEV=0` | capture device |
| `ASR_MIC_CARD` | `Device` | card for the gain restore |
| `ASR_MIC_SOURCE` | `auto` | `auto` / `board` / `remote` |
| `ASR_REMOTE_MIC_URL` | `http://127.0.0.1:5002/mic` | host mic bridge |
| `CAMERA_DEVICE` | `/dev/video2` | board camera |
| `CAMERA_FALLBACK_URL` | `http://127.0.0.1:5016/camera.jpg` | host camera fallback |
| `SENSOR_ENABLED`, `SENSOR_BAUD`, `SENSOR_RUN_SECONDS` | `true`, `115200`, `86400` | sensor node |
| `VOICE_PORT`, `VOICE_BAUD` | `/dev/ttyHS1`, `9600` | voice module UART |
| `C07A_MOTION_PORT`, `C07A_MOTION_BAUD`, `C07A_MOTION_MOCK`, `MOTION_FEEDBACK_ENABLED` | empty, `115200`, `true`, `false` | motion controller (inert by default) |
| `L610_BROKER_HOST`, `L610_BROKER_PORT`, `L610_DEVICE_ID`, `L610_DEVICE_SECRET` | empty | 4G module (unconfigured means disabled) |
| `ZIPVOICE_SERVER_URL` | `http://127.0.0.1:5018` | host TTS for `surge` |
| `DEEPSEEK_BASE_URL` | host proxy / `https://api.deepseek.com` | LLM endpoint |
| `DOUBAO_TTS_API_URL` | host proxy `/doubao/tts` | cloud TTS |
| `NUANYU_TTS_BACKEND` | `drizzle` | `drizzle` / `stream` / `surge` |

Secrets (`DEEPSEEK_API_KEY`, `DOUBAO_TTS_*`, `L610_DEVICE_SECRET`) come from the
environment or a populated `config/*.env` file. The repository contains templates only;
never commit a populated file, and never write a secret into a log, screenshot or
document.

---

## Known documentation conflicts

| Statement in the internal notes | What the code and this repository actually do |
|---|---|
| The startup script restores USB capture gain to 100% and enables AGC on every launch | `deploy/start_nuanyu_runtime.sh` does neither. The ASR worker re-applies `Mic 100%` at most once per 15 s; no AGC control is set anywhere. |
| The firmware/board UART for the motion controller is `/dev/ttyHS7` | That port was a script's assumption and was never verified on hardware. The runtime does not default to it: the motion port stays empty until bench verification. |
| The board's formal audio policy is `AUDIO_OUTPUT_MODE=board` | The supervisor default is `auto` — board first, host fallback only after board playback fails. Use `board` for the strict behaviour. |
| `hw:2,0` is the speaker | `hw:2,0` is only the *fallback* when `lahaina-yupikiot` is not found in `/proc/asound/cards`. The number is not stable across USB replugs and must not be hardcoded. |
| Port 5016 serves a Piper TTS endpoint | 5016 is the host **camera** fallback. A legacy `FIBO_PIPER_SERVER_URL` default still points at `5016/speak` in `app/fibo_tts.py`; it is inert unless a Piper-style engine is selected, but the two must not be enabled together. |
| Sensor absence means the UI shows "offline" | True now, and it did not used to be: `SENSOR_SIM_FALLBACK` and a radar-forcing path fabricated readings. Both were removed — see §"Sensor node". |
