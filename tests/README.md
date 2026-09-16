# Tests

Test suite for the Nuanyu on-device companion runtime (`app/`).

## Running the suite

From the repository root:

```bash
python -m pytest tests -q
```

If `pytest` is not installed, `unittest` works too:

```bash
python -m unittest discover -s tests -t tests -q
```

Every file in this directory also bootstraps `app/` onto `sys.path` itself, so a
single file can be run directly and a single test can be selected:

```bash
python tests/test_streaming_pipeline.py
python -m pytest tests/test_proactive_service.py -q -k cooldown
```

### Requirements

Only **`numpy`** is needed. `cv2` and `onnxruntime` are installed into
`sys.modules` as mocks by the vision tests, and every other board dependency
(the Fibocom SDK, the ALSAs, the camera, the sensor node) is either faked or
never imported. Installing `app/requirements.txt` is the supported setup and
gives the vision tests the real libraries, but it is not required to run the
suite.

## What the suite covers

| Area | Files |
|---|---|
| Conversation memory (SQLite + NoOp) | `test_memory_service.py`, `test_ai_memory_context.py` |
| Proactive engine and adapter | `test_proactive_service.py`, `test_proactive_adapter.py`, `test_sprint_contracts.py` |
| Tools and function calling | `test_tool_service.py`, `test_tool_calling_coordinator.py` |
| Streaming TTS segmentation / sessions | `test_streaming_pipeline.py` |
| Vision: FER, smoothing, camera ownership | `test_vision_lifecycle.py`, `test_expression_smoother.py` |
| Runtime lifecycle and health | `test_runtime_services.py`, `test_phase2_interfaces.py`, `test_phase2_integration.py` |
| Sensors to LLM context | `test_sensor_ai_context.py` |
| HTTP API, startup script, TTS backend names | `test_runtime_contract.py`, `test_single_instance_contract.py` |
| UI contracts (HTML/CSS/JS strings) | `test_sleep_wake_contract.py`, `test_stream_voice_ui_contract.py`, `test_user_persona_config.py`, `test_role_name_contract.py` |

Two styles of test appear here, and it is worth knowing which is which:

* **Unit tests** import a module from `app/src/` and call it (memory, tools,
  proactive engine, segmentation, smoothing).
* **Contract tests** read a source file as *text* and assert on the shape of it
  (endpoints, startup ordering, UI element ids, persona/role-name rules). They
  exist because `app/nuanyu_web.py` imports `termios` and the Fibocom SDK and
  therefore cannot be imported on a development machine. Where a contract test
  needs behaviour rather than text, it extracts the single function or class out
  of the source with `ast` and executes it standalone — see
  `test_sleep_wake_contract.py`, `test_role_name_contract.py` and
  `test_proactive_adapter.py`.

### Chinese fixtures

Several tests use Chinese text (user utterances, memories, persona prompts,
sensor announcements). That is the product's real input language — the strings
are fixtures, not localisation gaps. Please leave them as they are.

## Board-only behaviour

**No test in this directory currently requires the board.** Everything that
would otherwise need hardware is mocked: the camera (`cv2`), the emotion model
(`onnxruntime`), the ESP32 sensor node, the ALSA devices and the vendor
`fiboaisdk`. This is deliberate — the suite is meant to be runnable on a laptop,
and it is the reason a CI job can run it.

If you add a test that genuinely cannot work off-board, skip it with a readable
reason rather than letting it fail:

```python
import os
import unittest


@unittest.skipUnless(os.path.exists("/userdata_fibo"),
                     "board-only: needs the on-board model/SDK tree")
def test_dsp_tts_latency(self):
    ...
```

Prefer a capability probe (`os.path.exists("/dev/video2")`,
`importlib.util.find_spec("fiboaisdk")`) over a hostname check, so the test also
skips on a development machine that happens to be Linux.

### Side effects of a test run

`RuntimeServices.initialize()` is exercised by a handful of tests
(`test_runtime_services.py`, `test_phase2_interfaces.py`,
`test_runtime_contract.py`). It is the real startup path, so a run also:

* creates the runtime data directory under `app/data/` (git-ignored), and
* starts the sensor reader, which spawns `sc171_usb_receiver.py` and retries it
  in the background. The repeated `[SENSOR] receiver exited code=1, restart
  in 8.0s` lines on stderr are expected without the ESP32 node attached, and the
  `svc=runtime evt=init_done` trace lines are the runtime's own startup log.

Neither affects the test results. If the noise is distracting, disable the
sensor in the config the test builds (`config["sensor_enabled"] = False`).

## Deliberately not covered

* **Board I/O itself** — capturing from `/dev/video2`, playing to the ALSA
  card, reading the UART motion controller, and the ESP32 sensor stream. The
  suite tests the logic around these devices, never the devices.
* **The vendor speech stack** — Fibocom ASR/TTS through `fiboaisdk`, the
  Hexagon DSP models, and the host-side ZipVoice/drizzle servers. Only their
  routing, fallback and segmentation logic is covered.
* **The real LLM** — every LLM call in the tests is a fake or a mock; prompt
  construction is covered, the model's answers are not.
* **`app/nuanyu_web.py` as a running process** — HTTP handlers, SSE streams,
  session cookies and the single-instance file lock are covered by contract
  tests over the source, not by starting a server.
* **End-to-end latency and audio quality** — these need the board and are
  measured by the scripts in `tools/`, not here.

### A note on the simulation-style tests

`test_asr_routing.py`, `test_streaming_fallback.py`, and the
`TestAsrRoutingLogic` / `TestCoordinatorLifecycle` / `TestShutdownIdempotent`
classes in `test_runtime_contract.py` re-implement the app's logic against local
fakes (`FakeRobot`, `FakeCoordinator`, local `deque`s) instead of calling it.
They pass, but they assert on the fake, so they cannot catch a regression in
`app/`. Treat them as executable documentation of the intended behaviour; the
real coverage for those paths is in `test_streaming_pipeline.py` and
`test_runtime_services.py`. Wiring them to the real symbols would be a welcome
follow-up.
