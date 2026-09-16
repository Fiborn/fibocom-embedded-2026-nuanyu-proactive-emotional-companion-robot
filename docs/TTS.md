# Text-to-speech

The robot speaks through one of three interchangeable TTS backends. They differ in where
the model runs and what it is good at, but they all share the same streaming pipeline, the
same playback path on the board, and the same latency metric.

This document covers the backend split, the sentence-segmentation and streaming design,
how first-audio latency is measured, why synthesis concurrency is pinned to one, the bugs
that were fixed to get there, and the failure modes that look like success.

Source files: `app/nuanyu_web.py` (routing and lifecycle), `app/src/tts/*` (backends and
pipeline), `app/fibo_tts.py` (board speaker routing), `tools/zipvoice_server_v2.py` (host
side of the `surge` backend).

## 1. The three backends

`NUANYU_TTS_BACKEND` selects the engine at startup; the default is `drizzle`.

| Value | Model | Runs on | Needs a network | Character |
|---|---|---|---|---|
| `drizzle` | Matcha-TTS (`matcha-icefall-zh-baker`), Piper as a fallback voice | Board | No | Default. Fully offline, low latency |
| `stream` | Doubao cloud TTS (`seed-tts-2.0`) | Cloud | Yes | Highest voice quality, a curated voice list |
| `surge` | ZipVoice (zero-shot voice cloning) | Host PC | Yes (ADB tunnel) | Clones a voice from a short reference sample |

### `drizzle` — on-board, the default

`app/src/tts/drizzle_backend.py`. Synthesis runs in a **separate process**
(`app/src/tts/matcha_worker.py`) over a stdin/stdout JSON-line protocol. This is
deliberate: the `sherpa_onnx` wheel bundles its own ONNX Runtime, which is ABI-incompatible
with the one inside the vendor `fiboaisdk`, and loading both in one process crashes. Do not
inline the worker.

Inside the backend there are two threads: a synthesis thread and a playback thread. The
playback thread plays segment *n* while the synthesis thread synthesises segment *n+1*, so
there is no gap between sentences.

Shorter texts (≤ 6 characters) are cached, so a fixed phrase such as the opening line costs
no synthesis time on the second and later uses.

`app/fibo_tts.py` also contains an older Fibocom on-board DSP synthesis engine
(`FIBO_TTS_ENGINE=fibo`, with espeak-ng as a last resort). Nothing in the current default
path calls it; it is reachable only through `fibo_tts.py`'s own API. The DSP timeout
sentinel it writes is still watched by the supervisor — see §7.3.

### `stream` — Doubao cloud

`app/src/tts/doubao_provider.py`. Voices are an explicit allowlist enforced in the backend;
each entry is a Doubao voice id plus its display name. The API key and resource id come from
the environment (`config/nuanyu.env`, not committed). Requests go through the host-side
cloud proxy over an ADB reverse tunnel rather than directly to the internet.

Unlike the other two backends, `stream` is submitted as **whole paragraphs** rather than
sentence by sentence — the coordinator logs `strategy=whole-paragraph submit` for it.

### `surge` — ZipVoice on the host PC

`app/src/tts/surge_lite_provider.py` plus `tools/zipvoice_server_v2.py`. The host server
holds the voice packs and does the synthesis; the board posts text to it over HTTP (a
persistent `urllib3` connection pool with keep-alive) and gets base64 WAV back.

This is the only backend with user-cloned voices: an uploaded reference clip of 0.5–30 s is
normalised, stored as a voice pack, and activated on the server. A pack is displayed in the
frontend under whatever name the user gave it when uploading. Voice packs persist on the board
(`data/voice_packs/`), but the **server** loses its in-memory copy whenever the host process
restarts — which is why selecting `surge` always re-checks the server and re-activates the
selected voice instead of trusting a cached "available" flag.

On the host server:

- Diffusion steps default to 3. Two steps occasionally produced slurred or invented words;
  four was noticeably slower; three sits close to four in quality for about 0.6 s less
  latency.
- The `directml` provider is force-downgraded to `cpu` even if a launcher explicitly asks
  for it. On this hardware class `directml` is both slower and less compatible.
- Synthesis is serialised behind a single inference lock, because the underlying
  `OfflineTts` object is not safely re-entrant.

### Selecting and switching

The backend can also be switched at runtime through the settings page. A switch to an
unavailable engine is **rejected with an error** rather than silently substituted — the
point is that the UI never reports one engine while another one is speaking. A switch also
stops the outgoing backend and discards the coordinator, so the next reply rebuilds it
against the new engine.

There is one deliberate exception: if `surge` or `stream` is selected but has since become
unavailable, the speech path logs the fact and uses `drizzle`. This is a last-resort runtime
fallback, not a reported state change.

## 2. The streaming pipeline

A reply from the LLM arrives as a stream of token chunks and must start playing long before
the last token arrives. Two components do that:

- `IncrementalSentenceSegmenter` — turns an arbitrary chunk stream into natural, bounded
  speech units.
- `StreamingTTSCoordinator` — feeds those units to whichever backend is active, keeps them
  ordered, and tells the rest of the system when speech is in flight.

Both live in `app/src/tts/streaming_pipeline.py`.

### Segmentation

The segmenter buffers incoming text and cuts it at punctuation. Its rules:

| Setting | Env | Default in the code | Shipped value |
|---|---|---|---|
| `min_chars` | `TTS_STREAM_MIN_CHARS` | 6 | 6 (unset) |
| `soft_chars` | `TTS_STREAM_SOFT_CHARS` | 12 | **16** |
| `max_chars` | `TTS_STREAM_MAX_CHARS` | 42 | **36** |
| `first_soft_chars` | `TTS_STREAM_FIRST_SOFT_CHARS` | 8 | **6** |

The shipped `config/nuanyu.env.example` shortens all three: `TTS_STREAM_FIRST_SOFT_CHARS`
to `6`, `TTS_STREAM_SOFT_CHARS` to `16` and `TTS_STREAM_MAX_CHARS` to `36`. Shorter segments
start playback sooner at the cost of more synthesis requests. It leaves
`TTS_STREAM_MIN_CHARS` unset, matching the code default.

- Strong endings (`。！？!?；;` and newline) and weak endings (`,，、：:`) both cut, as soon as
  the buffered segment reaches `min_chars`. Cutting only on strong punctuation makes the
  speech stilted, so weak punctuation counts too.
- A fragment shorter than `min_chars` is held and merged into the next cut.
- **The first sentence is special.** It may cut at `2` characters (so a two-character
  nickname plus its comma is released immediately), and if no punctuation appears it is
  released at `first_soft_chars`. The reason is latency: the first segment starts synthesis
  earliest, and the first audio follows from it. After the first sentence the segmenter
  reverts to the normal thresholds, because a too-short later segment plays for less time
  than it takes to synthesise, which would open a gap.
- Text with no punctuation at all is still bounded by `max_chars`, preferring the last weak
  punctuation or space before the limit.
- Parenthesised asides are stripped **at buffer level**, not chunk level — the model often
  opens a reply with a stage direction like `（语气温和）` (*in a gentle tone*), and a chunk
  boundary can fall inside the brackets. Closed bracket pairs of 1–15 characters are
  removed; an unclosed pair is kept until more text arrives.

### Delivery

`StreamingTTSCoordinator` wraps a `speak_async(text, callback)` function and adds:

- **Commit ahead.** A segment is submitted as soon as it is cut. The coordinator does *not*
  wait for the previous segment to finish playing, so the backend's synthesis engine works
  on segment *n+1* during playback of segment *n*, and the inter-sentence gap is zero.
- **Ordering is delegated.** The provider assigns a sequence number and its playback thread
  drains results in sequence order. The coordinator only tracks how many segments are
  outstanding.
- **Bounded pending.** At most `TTS_STREAM_MAX_PENDING` (default 3) segments may be in
  flight. When the queue is full and the newest item belongs to the same session, the two
  are merged if the result stays within 64 characters; otherwise the submission is refused.
  This bounds memory when the LLM outruns the synthesiser.
- **Mic gating.** The coordinator reports "active" to the ASR worker, which keeps the
  microphone muted so the robot does not hear itself. It goes inactive only when every
  session is both finished and fully played out — not in the gap between two WAV files of
  the same reply, which used to let the speaker's voice be captured and fed back into the
  AI.
- **Two watchdogs.** Each segment arms a per-item timer (default 90 s) that releases it if
  the provider never calls back at all; and each finished session arms an **inactivity**
  grace timer (default 10 s, `TTS_SESSION_GRACE_SEC`) that is re-armed by every playback
  completion. The grace timer is deliberately idle-based rather than a fixed deadline from
  `finish()`, so a long but healthy reply is never unmuted early while it is still speaking.

The coordinator records the last 100 latency reports, which the status API exposes.

### Inside the `surge` provider

`surge` has the most machinery, because it is the backend whose latency behaviour is most
visible:

- A pool of `SURGE_MAX_CONCURRENT` synthesis threads consumes a bounded request queue
  (32 items). Enqueueing a request *is* starting synthesis.
- A single playback thread pops results by sequence number. Results are stored in a
  dictionary keyed by sequence, so an out-of-order completion is still played in order.
- A **first-sentence exclusive gate**: until segment 0 has finished synthesising, at most
  one later segment may run in parallel with it. Without the gate, full concurrency would
  compete for the same synthesis resources and slow down the very first audio — the one the
  user is waiting for. Once segment 0 completes, everything runs in parallel. The gate
  resets at the start of every reply.
- A short-phrase cache (`≤ 6` characters, at most 32 entries) keyed by
  `(voice_id, text)`. The key **must** include the voice, or switching voices would replay
  the previous voice's opening line. The cache is cleared on a voice switch.
- `speak_async` accepts a `delay_ms` argument, used only for the opening line — see §5.

## 3. First-audio latency

### Definition

For every segment, the provider timestamps the moment the text is enqueued
(`submitted_at`, taken in `speak_async`) and the moment playback begins. The reported
**first-audio latency** is their difference:

```
first_audio_ms = (time.perf_counter() - submitted_at) * 1000
```

It is measured at the point where audio starts, after the synthesis wait, the WAV parse,
the format conversion and the delay. Because the playback thread blocks on the result
condition variable while synthesis runs, this wait *is* the synthesis wait — the number is
not an estimate assembled from separate timers.

The per-segment log line breaks the same interval into its parts:

```
[SurgeLite] OK chars=12 | fetch=640ms decode=3ms parse=1ms convert=8ms play=2400ms |
            first_audio=1010ms wait=1010ms overhead=12ms | dur=2.4s rtf=0.27 err=
```

`fetch` is the HTTP round trip plus host synthesis; `parse`/`convert` are board-side WAV
handling; `play` is the `aplay` call; `overhead` is `parse + convert + base64 decode`.
The value is passed back through the callback as `{"first_audio_ms": …, "backend": …}`,
written to `/tmp/_last_tts_ms`, and rolled into the coordinator's latency log and the chat
latency record shown in the UI.

One honest caveat:

- For `drizzle` the reported value is `synthesis_ms + 40`, using a measured ~40 ms `aplay`
  start-up cost rather than a second timestamp. It is an approximation, and the code says
  so.

The value handed to the UI is the measurement, not a presentation of it: `_display_tts_ms()`
in `app/nuanyu_web.py` returns the real number unmodified, so the UI column and the log
agree.

## 4. Concurrency is pinned to 1

`SURGE_MAX_CONCURRENT` controls the size of the synthesis thread pool. Its default in the
code is `3`; **the shipping board configuration sets it to `1`**, and that is the value the
project validated.

The reason is that the host-side synthesis service is serial: it holds a single inference
lock, and parallel requests simply queue inside it. Extra concurrency therefore buys no
throughput at all, while still costing the board extra threads and extra HTTP connections
that compete with ASR and the rest of the runtime.

Serial is sufficient because one segment synthesises in roughly 0.8 s while it plays for
2–3 s — synthesis is comfortably faster than real time, so the pipeline still stays ahead
and sentences remain seamless with a single lane.

`TTS_STREAM_MAX_PENDING` (default 3) is a different knob: it is the coordinator's batching
window, unrelated to synthesis concurrency, and stays at 3.

## 5. Bugs that were fixed

### 5.1 The `BoundedSemaphore` double-release crash

**Symptom:** the robot stopped mid-reply. It would speak one or two sentences and then fall
silent; the wait counter climbed (observed up to 19 s) and never recovered.

**Root cause:** the first-sentence exclusive gate was a
`threading.BoundedSemaphore(1)`. Two paths released it. Segment 0 released unconditionally
when it finished; meanwhile a later segment had already acquired the semaphore and released
it after synthesising. If segment 0's release landed in between, the semaphore's value went
from 0 to 1 to 2, and the second release raised
`ValueError: Semaphore released too many times`. That exception killed the synthesis thread
outright. Segments already pulled from the queue were never written to the result buffer and
never called back, so the playback thread waited forever on a missing sequence number, every
later segment backed up behind it, and the reply simply stopped.

The crash log accumulated about 30 instances of this before it was diagnosed.

**Fix:** the semaphore was replaced by a `Condition` plus a counter (`_late_holders`), which
cannot double-release because the counter is only ever decremented by a worker that
incremented it. In addition:

- `_synth_worker` and `_synthesize_item` now catch every exception, record the failed
  sequence into the result buffer, and keep the thread alive. No exception may drop a
  segment.
- The playback thread has a **stall guard** (`SURGE_PLAY_STALL_TIMEOUT`, default 60 s): if a
  sequence's result never arrives, that sequence is skipped instead of freezing the reply
  forever.
- `speak_async` gained the `delay_ms` argument, and the queue tuple became
  `(seq, text, callback, submitted_at, delay_ms)`.

**Do not revert any of this.** Specifically: do not reintroduce a `BoundedSemaphore`, do not
remove the stall guard, and do not remove the exception fallback in `_synthesize_item`.

### 5.2 A shared callback closure

A related bug in the coordinator: the completion callback was defined inside the dispatch
loop and closed over the loop's `session_id`/`done`/`result` variables. Since the loop does
not wait for the provider, the first segment's callback would be invoked with the *later*
loop's bindings, so every subsequent segment was misfiled as a duplicate callback — the
provider logged that only one callback was ever received. The fix is a `_make_callback()`
factory that captures each segment's state in its own closure.

### 5.3 The opening-phrase delay

Every reply used to begin with the user's nickname followed by a comma — one fixed string per
user, e.g. `<nickname>，`. Because it is fixed and short, it hit the short-phrase cache and played
almost instantly, so the reported first-audio latency was a meaningless ~2 ms. Worse, the
first real sentence then had to wait for the model's first tokens, so the reply sounded
disjointed.

The opening line is now submitted with `delay_ms=TTS_OPENING_DELAY_MS` (default 1000). Once
the audio is ready, the playback thread waits out the remainder of that delay, measured from
the enqueue time, before playing. During that window the LLM is producing its first sentence
and the provider is synthesising it, so the next segment follows the opening line seamlessly.

The side effect is the honest one: the reported first-audio latency now includes the delay,
so it reflects how long the user actually waited instead of a cache hit. The delay applies
only to the `surge` backend.

### 5.4 The speaker-routing rule

All three backends must play through the same board path — `fibo_tts._play_wav()` or an
equivalent that routes through the same mixer configuration. There must not be a second
component configuring the HPH mixer, and the frontend must not play normal conversational
speech with `new Audio()`, because that comes out of the *host* PC's speakers instead of the
robot. (`new Audio()` is only acceptable as a browser-fallback for the last synthesized WAV,
never as the primary path.)

Routing is controlled by `AUDIO_OUTPUT_MODE`:

| Value | Behaviour |
|---|---|
| `board` | Board speaker only. The shipping configuration for a normal deployment |
| `pc` | Host PC speakers only |
| `auto` | Board first; if board playback fails, retry on the host through the desktop bridge |

The PC bridge exists as an emergency path for a board with a broken audio jack, and it is
the reason `auto` is not the right production setting: it will quietly move the robot's
voice to the host. `app/src/tts/audio_router.py` implements the fallback, and the board
supervisor pins `SURGE_PC_SPEAKER=0` and `FIBO_TTS_PC_ONLY=0`.

## 6. Verifying audio

Do **not** accept "the process is running" or "the API returned 200" as evidence that the
robot speaks. A TTS check has to end in a human listening to the board speaker.

```bash
# Which backend and voice is active
curl -s http://127.0.0.1:5004/api/status | grep -o '"backend":"[^"]*"'

# Host side of the surge backend: expect provider=cpu and num_steps=3
curl -s http://127.0.0.1:5018/status

# Latency and crash markers in the runtime log
grep -E 'SurgeLite|Coord|CRASH' /userdata_fibo/nuanyu_runtime.log | tail
```

What a healthy run looks like:

- One `[SurgeLite] … first_audio=…` line per segment, with `fetch` staying under a second
  and not growing across a reply (growth means the queue is backing up).
- No `[CRASH]` lines and no `Semaphore released too many times`.
- Audible speech from the board speaker, with the opening line followed seamlessly by the
  first sentence.

## 7. Operational failure modes

These are the failure modes that present as success at the API level. All three have cost
real debugging time; none of them is detected by an exit code.

### 7.1 `aplay` returns 0 but there is no sound

`aplay` exiting 0 only proves that ALSA accepted the PCM frames. It does not prove that the
codec is driving the physical output pins.

The concrete case on this board: the codec's `RX HPH Mode` must be set to `CLS_H_HIFI`. If
it is left at `CLS_H_INVALID`, every frame is consumed and the headphone pins stay silent —
identical logs, no audio. `app/fibo_tts.py` therefore sets `CLS_H_HIFI` (mixer control
`numid=243`) as part of opening the HPH route, and resets it to `CLS_H_INVALID` when the
route is closed. The route is opened once at startup (`hph_always_on_init()`) and kept open
so every backend shares it and no per-utterance mixer toggle adds latency.

If a port produces "Playback OK" and no sound, check, in order: the physical jack and any
amplifier power; the `RX HPH Mode` value; the actual ALSA card the frames went to.

### 7.2 Dynamic ALSA card numbering

The board does not have a fixed playback card number. USB devices plugged or unplugged at
runtime shift the enumeration, so a hard-coded `hw:1,0` or `hw:2,0` is correct only until
the next reboot. `deploy/start_nuanyu_runtime.sh` resolves the card by **name** on every
launch, scanning `/proc/asound/cards` for `lahaina-yupikiot` and exporting the result as
`FIBO_TTS_HPH_CARD`. The device index stays 0 (`FIBO_TTS_HPH_DEVICE`).

The same class of problem applies to the microphone: the capture device is addressed by
stable name (`plughw:CARD=Device,DEV=0`, overridable with `ASR_MIC_DEVICE`), and its capture
gain is re-applied from the ASR capture loop at most once per 15 s, because a USB
re-enumeration can reset it to 0%. The supervisor does **not** restore it at start.

You can check what the board currently sees with `cat /proc/asound/cards`.

### 7.3 The DSP timeout sentinel

The vendor DSP synthesis call is blocking and cannot be cancelled safely. `app/fibo_tts.py`
runs it on a worker thread behind a bounded wait (`FIBO_TTS_SYNTH_TIMEOUT`, default 12 s).
If the wait expires:

1. the SDK is put into a **quarantined** state — every later call fails fast with
   "Fibocom DSP is quarantined", because the abandoned vendor call is still holding DSP
   state and cannot be safely re-entered;
2. `/userdata_fibo/.nuanyu_tts_unhealthy` is written with the error text.

The board supervisor polls that file every 3 s and, when it appears, logs
`Fibocom TTS DSP timed out, restarting cleanly` and restarts the web process. That is the
only way to get a clean DSP state back, which is why the sentinel exists instead of a retry.

Note the scope: the sentinel is written by the Fibocom DSP synthesis path, which the current
default (`drizzle` → Matcha) backend does not call. The supervisor watches it regardless of
which backend is active.

### 7.4 Microphone gating and the fallback source

The coordinator's active flag drives the microphone mute. Two independent guards keep a
stuck session from muting the microphone forever: the per-item watchdog and the session
grace timer described in §2. If the microphone seems permanently deaf after a reply, look
for `[Coord] session force-finish (grace)` in the log — its absence means the mute was
never released.

The capture source itself is selected by `ASR_MIC_SOURCE`:

| Value | Source |
|---|---|
| `board` | Board USB microphone (`arecord`) |
| `remote` | Host PC microphone, streamed as raw PCM over an ADB reverse tunnel by `tools/pc_mic_server.py` |
| `auto` | Default. Board first; fall back to the host microphone if the board device cannot be opened |

The fallback exists for boards whose external microphone has failed. It is a workaround, not
a preferred configuration — a real deployment should have a working board microphone, and
the status API reports which source is actually in use (`microphone: board` or
`remote`) so the difference is visible rather than inferred.

## 8. Configuration reference

All of these are read from the environment, normally via `config/nuanyu.env` on the
board (not committed — copy `config/nuanyu.env.example` to `config/nuanyu.env` and fill it
in).

| Variable | Default | Meaning |
|---|---|---|
| `NUANYU_TTS_BACKEND` | `drizzle` | Active backend: `drizzle` / `stream` / `surge` |
| `TTS_OPENING_DELAY_MS` | `1000` | Delay before the opening line plays (`surge` only) |
| `TTS_STREAM_MAX_PENDING` | `3` | Coordinator in-flight segment limit |
| `TTS_STREAM_MIN_CHARS` | `6` | Segmenter: minimum segment length |
| `TTS_STREAM_SOFT_CHARS` | `12` in code, **`16` in the shipped config** | Segmenter: soft cut threshold after the first sentence |
| `TTS_STREAM_MAX_CHARS` | `42` in code, **`36` in the shipped config** | Segmenter: hard segment limit |
| `TTS_STREAM_FIRST_SOFT_CHARS` | `8` in code, **`6` on the board** | Segmenter: soft cut threshold for the first sentence |
| `TTS_SESSION_GRACE_SEC` | `10` | Idle time after which a stalled session releases the mic |
| `SURGE_MAX_CONCURRENT` | `3` in code, **`1` on the board** | Synthesis thread pool size |
| `SURGE_PLAY_STALL_TIMEOUT` | `60` | Seconds to wait for a missing sequence before skipping it |
| `ZIPVOICE_SERVER_URL` | `http://127.0.0.1:5018` (set by the board supervisor; the provider's own fallback is `5017`) | Host-side ZipVoice endpoint |
| `ZIPVOICE_NUM_STEPS` | `3` | Host-side diffusion steps |
| `ZIPVOICE_PROVIDER` | `cpu` | Host-side ONNX Runtime provider (`directml` is forced back to `cpu`) |
| `AUDIO_OUTPUT_MODE` | `auto` (`board` in production) | Speaker routing |
| `FIBO_TTS_HPH_CARD` | `2`, overridden at launch by name lookup | ALSA playback card |
| `FIBO_TTS_SYNTH_TIMEOUT` | `12` | Bounded wait for the Fibocom DSP synthesis call |
| `ASR_MIC_SOURCE` | `auto` | Capture source: `board` / `remote` / `auto` |
