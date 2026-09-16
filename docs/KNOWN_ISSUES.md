# Known issues and caveats

Things that will bite you, in rough order of how much they will bite you. Each entry says
what is wrong, how to tell whether it has bitten you, and what to do.

---

## 1. The deployed runtime and this repository can drift apart

**This is the most likely source of "but it works on my machine" confusion.**

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

The EXE's mtime is when the **build finished**, not when the payload was captured. A
PyInstaller onefile build of ~98 MB runs for several minutes, so a payload can predate an
EXE's timestamp by more than the gap between the last commit and the build. In the original
tree the gap was ~44 seconds — far too short for a build, which is itself the tell that the
build started *before* the last commit landed.

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

Consequences:

- File mtimes on the board cannot be compared with timestamps on your PC without first
  establishing the offset.
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
| No transport security | Plain HTTP on port 5004, no TLS anywhere. |
| Weak password storage | Unsalted SHA-256. No salt, no work factor. Replace with a memory-hard KDF (Argon2/bcrypt/scrypt) before any real deployment. |
| Development-grade sessions | Cookie sessions with no rotation or expiry policy worth the name. |
| No rate limiting | Nothing throttles login attempts. |
| Unauthenticated surface | Some routes answer before authentication; audit before exposing anything. |

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
- There is no CI. Nothing runs these tests automatically.

---

## 10. Documentation was translated, and translation can be wrong

Comments, docstrings and docs were translated from Chinese to English during the
open-sourcing pass. The web UI, the LLM persona prompts and the Chinese speech-command
matchers stay Chinese on purpose — translating those would change what the robot says and
break its command grammar.

If a comment ever contradicts the code, **the code is right**. Several claims inherited from
the internal notes were already found to be wrong during translation and are recorded in
`docs/HARDWARE.md` under "Known documentation conflicts".
