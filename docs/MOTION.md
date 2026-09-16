# Motion — the C07A chassis controller

The robot body can move. A small external controller board, referred to throughout the
project as the **C07A**, drives the two wheels through a motor driver and exposes a
line-based UART protocol for a fixed vocabulary of short gestures.

Motion is **optional**. The runtime treats it as an accessory that may be absent: with the
shipped configuration the client is disabled, opens no port, and every call is a no-op. The
robot talks, listens and shows expressions exactly as before.

Source file: `app/src/connectivity/c07a_motion.py`. Configuration:
`config/c07a_motion.env.example`. Loaded by `deploy/start_nuanyu_runtime.sh`.

The controller **firmware** is not part of this repository. It is built and flashed
separately; see §8.

## 1. Safety first

These rules are not stylistic. A previous bench session produced smoke.

1. **Wheels off the ground** for any motor test — the chassis raised so the wheels spin free,
   the area cleared of hands, cables and loose objects, and a power switch within reach.
2. **Cut the 12 V immediately** on continuous rotation, unusual noise, violent shaking or
   smoke. Investigate wiring, shorts and the supply before blaming the program.
3. **12 V goes only to the motor driver's motor supply input (`VMOTOR`).** Never to the C07A
   GPIO, never to the debug probe, never to the main board's signal pins.
4. **The SWD debug probe is for flashing and debugging, not for power.** Do not power the
   C07A from it and from its own supply at the same time.
5. **Do not connect the motor driver board's 5 V / 3.3 V rails to the C07A** until their
   sources, voltage levels and power-up order have been verified. Feeding 5 V into a 3.3 V
   rail destroys the part. The C07A's own 3.3 V logic supply needs its source confirmed
   independently.
6. **Never rewire or flash while powered.** If a wheel turns the wrong way, change the
   firmware polarity macros (`MOTOR_A_FORWARD_IN1_HIGH` / `MOTOR_B_FORWARD_IN1_HIGH`) and
   reflash — do not swap wires to compensate.
7. **Verify one command at a time before enabling the full flow.** `PING` → `PONG`,
   `ACT:STOP` → `ACK`/`DONE`, `ACT:GREET` → `ACK`/`DONE`. Only when all three pass should
   automatic triggering be switched on. If a command gets no acknowledgement, stop and
   diagnose; do not retry blindly.
8. **Power-up order:** signal lines, then C07A logic supply, then the 12 V motor supply.
   Power-down is the reverse — 12 V first.
9. Record direction, amplitude, acknowledgement log and video for every test run.

## 2. Wiring

### 2.1 C07A → motor driver

A fixed pin assignment. The driver board in the reference build is a D153C / TB6612-class
dual H-bridge.

| C07A pin | Function | Connects to |
|---|---|---|
| PA8 | Left wheel PWM (PWMA) | Driver `PWMA` (3.3 V logic, software 1 kHz PWM) |
| PA9 | Right wheel PWM (PWMB) | Driver `PWMB` |
| PA12 | Left wheel AIN1 | Driver `AIN1` |
| PA25 | Left wheel AIN2 | Driver `AIN2` |
| PA26 | Right wheel BIN1 | Driver `BIN1` |
| PA27 | Right wheel BIN2 | Driver `BIN2` |
| PA24 | Driver standby (STBY) | Driver `STBY` — must be driven high; left floating, the motors never turn |
| GND | Common ground | Driver GND = C07A GND = main board GND = supply GND |

Which of PA12/PA25 is AIN1 versus AIN2 depends on how the harness is actually built.
Annotate and record it; do not infer it from wire colour.

### 2.2 Main board → C07A UART

| Main board | C07A |
|---|---|
| TX | RX |
| RX | TX |
| GND | GND |

TX and RX **must cross**, and the two sides **must share ground** — a missing common ground
is one of the most common reasons the controller never answers.

Both ends must be **3.3 V TTL**. If either side is RS-232, 5 V or 1.8 V, add a level shifter
first. The C07A's input high threshold is roughly `0.7 × VDD ≈ 2.31 V`, so a 1.8 V TX line
will never be read as a high level.

### 2.3 Why the port is left unset

`config/c07a_motion.env.example` ships with:

```ini
C07A_MOTION_PORT=
```

and a comment saying not to fill it in until the main-board/C07A UART voltage, pins and
common ground have been verified.

This is not an oversight. The main board exposes several UART-capable `ttyHS*` nodes, and
which one is physically routed to the connector and configured as a UART by the pin
multiplexer can only be established from the board schematic and a measurement — not from
the device node list. Two of them are already spoken for:

| Node | Owner |
|---|---|
| `ttyHS1` | SU-03T voice module. **Never use this one for the C07A.** See §2.5 |
| `ttyHS6` | L610 4G module UART fallback |
| `ttyHS7` | A candidate only. It was used by an early bench probe script; it has **not** been confirmed as the C07A port |

With `C07A_MOTION_PORT` empty the client refuses to open anything and logs
`C07A_MOTION_PORT is empty`. That is the intended state until the hardware is verified.

How to identify the real port:

1. Find which `ttyHS*` the schematic routes to the connector and pin-muxes as a UART.
2. Loop the port back (short TX to RX) and confirm echo. This only proves the main-board
   side works, not that anything is attached.
3. Measure the TX idle level with a meter or scope. It should sit high at ~3.3 V. A steady
   0 V means the pin is not muxed or not brought out at all.
4. Confirm `stty` can set the baud rate on the node without error.

### 2.4 Power

- The C07A's 3.3 V logic supply source must be confirmed separately.
- 12 V goes to the driver's motor input only.
- The driver board's own 5 V / 3.3 V rails stay disconnected from the C07A until their
  source, level and sequencing are known.
- Three or four point common ground (supply, driver, C07A, main board).

### 2.5 Why the SU-03T voice port must never be used

`ttyHS1` carries the SU-03T voice-command module at 9600 baud. In the runtime it is opened
by the voice loop (or, in watchdog mode, by the watchdog process that owns the serial port
and forwards commands). It is a different device, at a different baud rate, with a different
framing convention.

Pointing the C07A client at it would mean two writers on one serial port, with binary voice
frames being parsed as motion replies and motion commands being fed to the voice module.
The runtime comment on the C07A client says it directly: *C07A is a distinct optional UART;
it never reuses `VOICE_PORT`.* The two must stay separate.

## 3. Protocol

| Property | Value |
|---|---|
| Baud | 115200 (overridable with `C07A_MOTION_BAUD`) |
| Framing | 8 data bits, no parity, 1 stop bit |
| Encoding | ASCII, one frame per line, `\n` terminated |
| Flow control | None |

Commands (main board → controller):

| Frame | Meaning |
|---|---|
| `PING` | Liveness probe |
| `ACT:<ACTION>` | Start a named action |

Replies (controller → main board):

| Frame | Meaning |
|---|---|
| `PONG` | Answer to `PING` |
| `ACK:<ACTION>` | Action accepted and started |
| `DONE:<ACTION>` | Action finished; the wheels have stopped |
| `ERR:<REASON>` | Rejected or aborted |

The client understands these error reasons: it logs any `ERR:` line and clears the pending
action, leaving the action vocabulary and firmware behaviour as the only authority for what
each reason means.

The client applies three rules to the receive stream:

- **Stale replies are ignored, not fatal.** A `STOP` can be sent while an earlier action's
  `ACK`/`DONE` is still in flight, and `STOP` is deliberately fire-and-forget on failure
  paths. An `ACK:`/`DONE:` for `STOP`, or one arriving when nothing is pending, is logged
  and dropped. Treating it as an error used to trigger an emergency stop that produced
  another reply, and so on — a stop feedback loop.
- **An unexpected reply is fatal.** An `ACK:`/`DONE:` naming an action that is *not* the
  pending one, or a line that is neither a known reply nor `PONG`, raises an error and
  triggers an emergency stop.
- **An oversized line is fatal.** If more than 256 bytes accumulate without a newline, the
  client assumes the stream has desynchronised, stops the motors and clears the buffer.

Exactly one frame is written per command, with no padding or sync bytes: the firmware is
line-framed and already discards empty lines, and synthetic sync bytes have been observed to
produce delayed parser diagnostics on some driver revisions.

## 4. Action vocabulary

`MotionAction` in `app/src/connectivity/c07a_motion.py`:

| Action | Intent | Typical duration |
|---|---|---|
| `STOP` | Stop the motors immediately | — |
| `GREET` | Greeting wiggle on wake-up | 600 ms |
| `NOD` | Acknowledge what the user said | 430 ms |
| `SHAKE` | Could not understand the input | 320 ms |
| `THINK` | Working on a request | 340 ms |
| `HAPPY` | Positive mood | not specified in the sources |
| `COMFORT` | Soothing response | not specified in the sources |
| `REMIND` | A scheduled reminder is due | 420 ms |
| `GOODBYE` | The user is leaving | not specified in the sources |
| `ALERT` | Reserved | not wired to any event |
| `OFFLINE` | Reserved | not wired to any event |

The durations above are the firmware-side design values recorded in the integration guide;
they are not verifiable from this repository. The same guide specifies a default 25 % PWM
duty, segments no longer than 350 ms and a total action no longer than 3 s, and caps duty at
35 % for bench testing. **Treat these as design intent, not as a contract this client
enforces** — the client's only notion of an action ending is the `DONE` frame.

### Choosing a gesture for a spoken reply

`speech_motion_action(scene, mood)` maps an interaction *scene* to one gesture rather than
trying to classify individual sentences, so a long reply produces one predictable movement
instead of a series of them:

| Scene | Action |
|---|---|
| `wakeup`, `proactive_greeting` | `GREET` |
| `happy` | `HAPPY` |
| `proactive_emotion` | `COMFORT` |
| `proactive_sit`, `proactive_low`, `reminder`, `sensor_report` | `REMIND` |
| `user_leave` | `GOODBYE` |
| `mood` with a positive mood string (`开心`, *happy*; `高兴`, *glad*) | `HAPPY` |
| `mood` with a negative mood string (`焦虑` *anxious*, `疲惫` *tired*, `崩溃` *overwhelmed*, `难过` *sad*) | `COMFORT` |
| anything else | `NOD` |

The mood strings are the Chinese labels the FER pipeline produces, so the comparison is made
against those literals.

## 5. Timeouts, heartbeat and error handling

| Setting | Default | Meaning |
|---|---|---|
| `MOTION_FEEDBACK_ENABLED` | `false` | Master switch. Alias: `C07A_MOTION_ENABLED` |
| `C07A_MOTION_MOCK` | `true` | Bench mode without hardware — see §7 |
| `C07A_MOTION_PORT` | *(empty)* | UART device node. Empty means "do not open anything" |
| `C07A_MOTION_BAUD` | `115200` | Baud rate. Only 9600 and 115200 are accepted |
| `C07A_ACK_TIMEOUT_S` | `0.8` | Time allowed for `ACK:<ACTION>` |
| `C07A_DONE_TIMEOUT_S` | `4.0` | Time allowed for `DONE:<ACTION>` after acknowledgement |
| `C07A_HEARTBEAT_S` | `0.5` | Interval between `PING` frames |

Behaviour:

- **One action at a time.** `trigger()` refuses a new action while one is pending, logging
  `reject busy`. The controller is not asked to preempt.
- **Queueing behind the current action.** `trigger_after_stop()` is the variant used for
  speech gestures: if an action is already running, the new one is stored as a successor and
  started when the current `DONE` arrives. Only one successor is retained — the newest wins.
  This exists because not every firmware revision accepts a `STOP` that preempts an active
  `THINK`, and forcing one could come back as an error.
- **Timeouts escalate to an emergency stop.** If `ACK` does not arrive within 0.8 s, or
  `DONE` within 4 s of the action starting, the client logs the failure and sends
  `ACT:STOP`. (`STOP` itself is never awaited on these paths.)
- **Heartbeat.** `PING` is sent every `C07A_HEARTBEAT_S` while connected. The companion
  firmware is documented as having its own link timeout of 1 s, after which it stops the
  motors by itself — that firmware-side backstop is the real protection if this process dies
  or the link breaks.
- **Reconnect backoff.** If opening the port fails, the client waits 5 s before trying again.
- **`close()` sends a final `STOP`**, but this is best-effort: the board supervisor stops
  Python with `SIGTERM`, which does not run `atexit` handlers. The firmware's link timeout is
  what actually guarantees the wheels stop.

### Status reporting

The `/api/status` response contains a `c07a` block — `enabled`, `mock`, `connected`,
`port`, `baud`, `pending`, `queued`, `last_error` and a rolling 40-entry `history` of
protocol lines — plus a derived `ready` field that is true only when motion is both enabled
and connected. A freshly booted, unconfigured system reports `ready: false`, which is
correct and not an error.

## 6. How motion is wired into the runtime

The event wiring lives in `app/nuanyu_web.py`. Every trigger is best-effort: it never blocks
a chat turn, and any exception is recorded as a `c07a_error` event.

| Event | Action | Notes |
|---|---|---|
| Voice wake-up | `GREET` | The only broadcast trigger — one per robot in the target set |
| Speech recognition succeeded | gesture for the scene | Gated on `source == "voice"`, so typing in the web UI does not move the robot |
| LLM request starts | `THINK` | |
| LLM response completes | `STOP` | Unless a speech gesture was already dispatched |
| Sleep / voice disabled | `STOP` | |
| Unrecognised voice-module frame | `SHAKE` | |
| Scheduled reminder due | `REMIND` | |
| Reading a spoken reply | `trigger_after_stop(scene)` | Queued behind whatever gesture is currently running |

Not wired: `ALERT` and `OFFLINE`.

Additional design constraints worth preserving:

- **One owner per physical port.** The C07A client is created per robot instance, but only
  the default (hardware-owning) robot instance is ever started and polled; every logged-in
  user's instance routes through that one client. Without this, a second instance would open
  the same device node a second time and actually send commands.
- **Polling is on its own timer.** `poll()` runs in a dedicated loop at a 50 ms interval,
  not inside the slower proactive-rules loop. This matters because the ACK/DONE timeouts and
  the heartbeat are all advanced from `poll()`; a slower loop would stretch the effective
  timeout by up to one tick.
- **The lock order is one-way.** Motion triggers are issued outside the web service's main
  state lock, so the C07A client lock is always taken before any web lock and never the
  other way round. Holding the web lock while triggering has deadlocked against the poll
  thread before.

## 7. Mock mode

`C07A_MOTION_MOCK=true` — the default — makes the whole path runnable on a bench with no
controller attached:

- `start()` marks the client connected without opening any port.
- `poll()` immediately synthesises `ACK:<ACTION>` and `DONE:<ACTION>` for the pending
  action, so timeouts never fire and queued successors advance.
- `_write()` drops every outgoing frame.

Everything above it behaves normally: triggers are accepted, health reports `connected`,
the status block looks live. This lets you exercise the event wiring, the queueing rules and
the UI without any hardware.

Mock mode is not the same as disabled. With `MOTION_FEEDBACK_ENABLED=false` (also the
default), `trigger()` returns `False` immediately and `start()` logs `disabled`; nothing
runs at all. A bench session normally wants `enabled=true` + `mock=true`, and a real
installation wants `enabled=true` + `mock=false` + a verified `C07A_MOTION_PORT`.

## 8. Firmware

This repository contains the client only. Nothing here can flash, verify or rebuild the
controller firmware.

Field notes from the integration work, which are worth carrying forward:

- The controller is a TI **MSPM0G3507**. Flashing and verification are done over SWD with
  `pyocd`, using a DAPLink probe. Record the probe's actual UID from `pyocd list` — do not
  copy a UID from any document.
- Two firmware builds exist. Only the **UART build** implements the `ACT:`/`PING` text
  protocol described here. An earlier build has no protocol layer at all; flashing it
  produces a controller that accepts commands and silently ignores them. Confirm the build
  before flashing.
- `pyocd load` performs program-and-verify internally, and a verification failure is
  reported as an error, so a clean run is meaningful evidence. Check that the file starts
  with the `7F 45 4C 46` ELF magic (a `.out` extension proves nothing).
- **A firmware file present on the main board's filesystem does not mean the controller has
  been flashed.** That was a real confusion during integration: the archived image matched
  the authoritative release byte for byte, while the actual controller had never been
  programmed.
- Beware the diagnostic trap: **no `PONG` does not mean "not flashed."** Work through power,
  common ground, TX/RX orientation, pin muxing, voltage level and firmware build *first*.
  Reflashing a controller whose image is already correct fixes nothing and adds a variable.
- Do not try to rebuild the firmware from the source tree alone. The published firmware code
  is the core logic layer; the project scaffolding needed to produce a flashable image
  (startup code, system configuration, generated device configuration) is not part of it,
  and there is no complete toolchain project to build from.

## 9. Bench checklist

Do these in order. Each step is a gate for the next one.

1. **Baseline.** Service active, controller file present, `ttyHS*` nodes listed and recorded.
2. **Wiring.** Pin-by-pin against §2.1. Continuity-confirm the common ground. Measure the TX
   idle level (expect ~3.3 V). Confirm the C07A supply rail comes up.
3. **Firmware.** Verify or flash the UART build; confirm verification passed. Disconnect the
   probe and confirm the controller still powers up.
4. **Single commands**, at 115200 8N1, with the wheels raised and the 12 V switch in reach:
   `PING` → `PONG`; `ACT:STOP` → `ACK:STOP` then `DONE:STOP`; `ACT:GREET` → `ACK:GREET`
   then `DONE:GREET`, with a visible bounded wiggle that stops by itself. Any command
   without both `ACK` and `DONE`, or any wheel that keeps turning or makes an unusual noise
   → cut the 12 V and stop; do not retry.
   Diagnostic order when there is no reply: controller power → TX/RX crossed or loose →
   common ground → main-board pin mux and voltage level → firmware is not the UART build →
   wrong port or baud, or the port is in use by something else.
5. **Per-action test.** Send each action name individually and wait for `DONE` before the
   next. Check direction, amplitude and duration against §4. If a wheel runs backwards,
   change the firmware polarity macro and reflash — do not rewire.
6. **Software configuration.** Only now fill in `C07A_MOTION_PORT` in the board's
   `config/c07a_motion.env` (never `ttyHS1`) and set `C07A_MOTION_MOCK=false`.
7. **End-to-end.** With `MOTION_FEEDBACK_ENABLED=true`, verify each event in §6 fires, that
   the wheel direction matches the intent, that conversations are not delayed, and that the
   process stays up. Then run a negative pass: sleeping, quiet mode and web-typed chat must
   not move the robot.

## 10. Known unresolved items

Recorded as unresolved at the time of the integration work; none of them can be settled
without the hardware in front of you.

- The real UART node for the C07A. `ttyHS7` is an assumption from an early probe script and
  has never been confirmed.
- The source of the C07A's 3.3 V logic supply.
- Whether the motor driver board's 5 V / 3.3 V rails can share a rail with the C07A.
- Whether the controller's flash actually holds the UART build (needs a probe-side verify).
- The polarity macro values that produce the correct wheel directions on the real chassis.
- The firmware's actual baud rate and UART pin binding.
