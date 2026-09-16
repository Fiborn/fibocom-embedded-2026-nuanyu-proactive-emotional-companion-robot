# Known issues and caveats

Things that will bite you, in rough order of how much they will bite you. Each entry says
what is wrong, how to tell whether it has bitten you, and what to do.

---

## 1. The deployed runtime and this repository can drift apart

**The shipped launcher has been verified to match this repository — but a stale copy of it
exists on disk, and launching that one silently downgrades the board.**

The board does not run this repository directly. In the original deployment a Windows
desktop launcher (`暖语.exe`, built from `desktop/` in the internal tree) connects over
ADB, **pushes its own bundled payload over the board's files**, and restarts the service.

That means there are two independent copies of the code:

| Copy | Where it comes from |
|---|---|
| This repository | The board's files as they were captured (`adb pull`) |
| The desktop EXE's payload | Whatever the source tree looked like **when the EXE was built** |

If the EXE is older than the board, launching it **silently downgrades the board**. Nothing
reports this — the sync succeeds, the service restarts, and the runtime is now running
different code than you think.

### Verification status of the known artifacts

The reference deployment was checked by extracting the EXE's embedded payload and comparing
all **30 push destinations** against the board, by content hash:

| Artifact | Built | Verdict |
|---|---|---|
| `D:\Users\35190\Desktop\暖语.exe` (sha256 `9397d39f…49838e`) | 2026-08-11 00:21 +0800 | **Matches the board — 30/30 destinations byte-identical.** Launching it is a no-op. |
| `dist/暖语.exe` in the upstream repo | 2026-08-10 22:26 +0800 | **Stale. Would downgrade two files** — see below. |

> **The trap.** The upstream repository force-tracks `dist/暖语.exe` even though `dist/` is in
> `.gitignore`, so it travels with every clone and looks exactly like the deliverable. It
> predates the last commit and, if run, reverts `xiaopei_web_v3.py` (losing the latency
> final-write fix) and `src/connectivity/c07a_motion.py` (losing the late-ACK guard that
> stops STOP cascades). **Check which EXE you are running before you run it.**

### This repository is ahead of the board

The published code carries two deliberate behavioural fixes that are **not** on the board and
**not** in any EXE:

- removal of `SENSOR_SIM_FALLBACK` and the radar-forcing path (`app/src/sensors/`), and
- removal of the fabricated TTS latency in `_display_tts_ms()`.

If you apply those to the board by hand, **a later EXE launch will silently revert them**,
because the EXE pushes its own copies of those files. Re-syncing and re-fixing is the
workaround; removing the duplicate copy of the code is the fix.

### How to tell which revision the board is on

Compare a file the EXE also ships. `nuanyu_web.py` (formerly `xiaopei_web_v3.py`) is the
best one, because it changes most often:

```bash
adb shell sha256sum /userdata_fibo/companion_demo/workspace/web_app/xiaopei_web_v3.py
sha256sum app/nuanyu_web.py        # compare the value, not the filename
```

If you still have the EXE, extract its payload without running it and compare the same way —
never infer the revision from the EXE's file timestamp (see §2).

### Why timestamps will mislead you

An EXE's mtime is when the **build finished**, not when the file contents were captured, and
nothing in the file records which revision it bundles. Two builds an hour apart can carry
wildly different code, and a build can be named or dated in a way that suggests the opposite.

This is not hypothetical. During verification the shipped launcher's timestamp sat only
~44 seconds after the last commit, which looked too tight to be a real build and suggested it
might predate that commit. **That inference was wrong** — extracting the payload showed the
file matched exactly. Meanwhile a *differently named* copy of the launcher, only two hours
older, turned out to be the one that would have downgraded the board. The timestamps pointed
at the wrong suspect in both directions.

**Compare content hashes, never timestamps.**

### What to do about it

- Treat the board as the runtime truth and this repository as a snapshot of it, and re-verify
  with a hash comparison after any EXE-driven sync.
- If you rebuild the desktop launcher, rebuild it from a tree you have just committed, and
  confirm the board's hashes afterwards.
- Better: stop having two copies. Have the launcher push *this* repository's `app/` directory
  rather than a payload frozen into its own bundle.

---

## 2. The board's clock is wrong, and it stays wrong

At the time of capture the board reported `2026-08-11` while the real date was over a month
later. It has no working time source, so **every file timestamp on the board is relative to a
clock that has stopped**.

The board also runs **UTC**, while a typical developer machine is on local time. So a naive
comparison of board mtimes against your own is wrong twice over — by the timezone offset
*and* by however long the board's clock has been dead. (During verification, a batch of board
files stamped `Aug 11 02:01` were actually pushed at `10:01` local time, and the clock had
already been stopped for weeks.)

Consequences:

- File mtimes on the board cannot be compared with timestamps on your PC without first
  establishing both the timezone offset and the drift.
- Anything that keys off wall-clock time — token expiry, TLS certificate validation, log
  correlation, `Last-Modified` headers — behaves unexpectedly.
- Log lines from the board carry misleading dates.

**Do not use board timestamps as evidence of anything.** Use content hashes. If you need
correct time, sync it over ADB (`adb shell date` with a value from the host, or run an NTP
client once networking is up).

---

## 3. There is no way to create an account from the UI

The runtime only ever *reads* `app/memories/_users.json`; there is no registration endpoint
and no seeding step. A fresh install has zero accounts and the login page cannot get you in.

Seed the first account yourself — the README's "Creating the first account" section has a
copy-paste snippet. Passwords are stored as **unsalted SHA-256 hex digests**, so pick
something you do not use anywhere else, and see §5.

---

## 4. Optional hardware is genuinely optional, and fails loudly

- **L610 4G module.** Started by the supervisor, but with no module attached it logs
  `AT port not found` and exits. This is expected and non-fatal; nothing else depends on it.
  It needs `config/l610.env` (broker host, device ID, device secret) before it will do
  anything.
- **C07A motion controller.** Its UART port is deliberately left unset. The wiring
  (voltage, pins, common ground) was never verified on hardware, so the runtime refuses to
  guess. Motion stays inert until you set `C07A_MOTION_PORT`. See `docs/MOTION.md`.
- **ESP32-S3 sensor node.** Absence is non-fatal, and — deliberately — the runtime does
  **not** substitute fake readings. You will see it reported as offline. See `docs/HARDWARE.md`.

---

## 5. The security posture is lab-grade, not product-grade

Do not expose this to an untrusted network. Specifically:

| Issue | Detail |
|---|---|
| Binds all interfaces | `WEB_HOST = "0.0.0.0"` in `app/nuanyu_web.py`. Not loopback-only — the `adb forward` tunnel makes it *look* loopback-only on the host, but the board's own listener is reachable from the board's LAN. |
| No transport security | Plain HTTP on port 5004, no TLS anywhere. |
| Weak password storage | Unsalted SHA-256. No salt, no work factor. Replace with a memory-hard KDF (Argon2/bcrypt/scrypt) before any real deployment. |
| Development-grade sessions | Cookie sessions with no rotation or expiry policy worth the name. |
| No rate limiting | Nothing throttles login attempts. |
| Unauthenticated read routes | `/api/status`, `/api/sensors`, `/api/weather`, `/api/tts_status`, `/api/tts_audio` and `/display` all answer without a session. **`/api/users` enumerates account names and creation dates** to any caller — the most leaky of them. Audit the route table before exposing anything. |

None of this is fixed, because fixing it changes behaviour the original deployment depends
on. It is called out here so nobody deploys it believing it is production-ready.

---

## 6. Model weights are not included, and some cannot be

See `docs/MODELS.md` for the full inventory. The short version: Matcha-TTS, Piper and
ZipVoice are open and you can fetch or convert them; the Fibocom `fibotts` and `fiboasr`
DSP models are **vendor assets tied to the board's DSP** and cannot be redistributed. Without
them, ASR and the default on-board voice do not work.

---

## 7. Code of unclear provenance that was deliberately kept

- **`app/fibo_tts.py`** is derived from the Fibocom SDK's own sample module (byte-identical
  to the copy that ships in the vendor's `tts_client/` directory), with board-specific
  speaker routing added on top. It was kept rather than rewritten because it is the SDK's
  public API surface. If you are licensing this project, review that file separately.
- **`piper_local`** is imported inside `app/fibo_tts.py` behind `FIBO_PIPER_INPROC=1`. **That
  module does not exist** anywhere in this repository or on the reference board, so the
  branch cannot work. It was left in place rather than deleted because it is part of the
  vendor module's API surface — but do not select it.

---

## 8. Legacy oddities that are load-bearing

These look like bugs and are not:

- **Port 5016 does two jobs.** It is the host **camera** fallback in the live config, but
  `app/fibo_tts.py` still carries a legacy `FIBO_PIPER_SERVER_URL` default pointing at
  `5016/speak`. They must not be enabled at the same time.
- **Port 5002 is shared** by two different host services (`/mic` raw PCM and `/transcribe`).
  Only one can bind it at a time.
- **`SURGE_MAX_CONCURRENT` defaults to 3 in the provider but is set to 1 in the board
  config.** 1 is correct for the CPU host server; the code default is a leftover.
- **ZipVoice's own docstring says port 5017 while its code and the deployment use 5018.**
  5018 is what runs.

---

## 9. The test suite has soft spots

405 tests pass, and they run on any platform without the board. But be aware:

- Some tests assert against **local fakes** rather than the real modules (notably parts of
  `test_asr_routing.py`, `test_streaming_fallback.py`, and three classes in
  `test_runtime_contract.py`). They pass, and they would not catch a regression. They are
  listed in `tests/README.md`.
- `test_vision_lifecycle.py` mocks the ONNX session and the camera, so the real inference
  path is not covered.
- CI (`.github/workflows/tests.yml`) runs the suite on every push and pull request across
  Python 3.8/3.11/3.12, plus a byte-compile pass over `app/`, `tools/` and `tests/`.

---

## 10. Documentation was translated, and translation can be wrong

Comments, docstrings and docs were translated from Chinese to English during the
open-sourcing pass. The web UI, the LLM persona prompts and the Chinese speech-command
matchers stay Chinese on purpose — translating those would change what the robot says and
break its command grammar.

If a comment ever contradicts the code, **the code is right**. Several claims inherited from
the internal notes were already found to be wrong during translation and are recorded in
`docs/HARDWARE.md` under "Known documentation conflicts".
