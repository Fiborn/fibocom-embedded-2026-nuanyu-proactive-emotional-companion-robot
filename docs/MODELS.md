# Model inventory

**No model weights are distributed with this repository.** This document is the reference
for what to install on a fresh board, what each model is for, and — importantly — which ones
you cannot legally redistribute.

A complete board installation is roughly **2.5 GB** across the paths below. Paths are
relative to the board runtime root (default `/userdata_fibo`).

## Summary

| # | Model | Path | Size | Runs on | Redistributable |
|---|---|---|---|---|---|
| 1 | Matcha-TTS `matcha-icefall-zh-baker` | `models/tts/drizzle/matcha-icefall-zh-baker/` | 139 MB | Board | Yes — open |
| 2 | Piper `zh_CN-huayan-medium` | `models/tts/drizzle/zh_CN-huayan-medium.onnx` | 61 MB | Board | Yes — open |
| 3 | Piper `zh_CN-huayan-x_low` | `models/tts/drizzle/zh_CN-huayan-x_low.onnx` | 20 MB | Board | Yes — open |
| 4 | Fibocom ASR `fiboasr_base_v1_0711` — **not used**, see below | `models/asr/fiboasr_base_v1_0711/` | 228 MB | Board DSP | **No — vendor** |
| 5 | Fibocom TTS `fibotts_1.0.0` | `tts_client/tts_models/` | 125 MB | Board DSP | **No — vendor** |
| 6 | ZipVoice decoder + vocoder | `models/zipvoice_onnx/` | 171 MB | Host PC | Yes — open |
| 7 | Surge / ZipVoice distill int8 | `models/tts/surge/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia/` | 197 MB | Host PC | Yes — open |
| 8 | Surge `zipvoice_distill_int8` | `models/tts/surge/zipvoice_distill_int8/` | 214 MB | Host PC | Yes — open |
| 9 | Surge `zipvoice_gpu` | `models/tts/surge/zipvoice_gpu/` | 911 MB | Host PC | Yes — open |
| 10 | Vocos vocoder | `models/tts/surge/vocos_24khz.onnx` | 52 MB | Host PC | Yes — open |
| 11 | FER emotion classifier | `fer_ort/fer_emotion_clean.onnx` | 244 KB | Board | Yes — open |
| 12 | Whisper Tiny (default ASR backend) | `asr_whisper_test/models/whisper_tiny_cpu.fmodel` | small | Board CPU | Yes — open |

## 1–3. Board-side TTS — the `drizzle` backend

The default voice. `matcha-icefall-zh-baker` is the primary; the two Piper voices are
lower-weight fallbacks selected with `zh_CN-huayan-medium` / `zh_CN-huayan-x_low`.

Matcha and Piper are open models. Piper voices come from the
[Piper](https://github.com/rhasspy/piper) voice collection; Matcha comes from
[icefall](https://github.com/k2-fsa/icefall) and runs through
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

> `matcha_worker.py` runs the Matcha model in a **separate process** on purpose: the
> `sherpa_onnx` wheel bundles its own ONNX Runtime (1.24.4) which is ABI-incompatible with
> the one inside the vendor `fiboaisdk` (1.22.0). Loading both in one process crashes.
> Do not "simplify" this by inlining it.

## 4–5. Fibocom DSP models — **cannot be published**

`fiboasr_base_v1_0711` (speech recognition) and `fibotts_1.0.0` (the on-device TTS voice)
are compiled for the Qualcomm Hexagon DSP via the Fibocom AI Stack:

```
models/asr/fiboasr_base_v1_0711/
  fiboasr_base_v1_0711_qcom_6490_8550_snpe_2_29_dsp_a3788d6c281cef3cf6e41c733afc489a.fmodel
tts_client/tts_models/
  fibotts_1.0.0_qcom_6490-8550_qnn_2.26_dsp_02c30b45759c7583f61881af2e1d8c24.fmodel
```

These are vendor assets tied to the board's DSP. They are **not downloadable** and must be
obtained from Fibocom or copied from a board that already has them. `tts_client/` also
contains the `fibo_tts.py` SDK binding and the license file the SDK looks for at
`/home/fibo/qcom_6490_license`.

`fibotts_1.0.0` is reachable only through `app/fibo_tts.py`'s own DSP synthesis API
(`FIBO_TTS_ENGINE=fibo`); no default speech path calls it. `fiboasr_base_v1_0711` is not
referenced by any module in this tree — the runtime's ASR backend is Whisper Tiny (§12).

## 6–10. Host-PC TTS — the `surge` backend

Zero-shot voice cloning, driven by `tools/zipvoice_server_v2.py` on the host and reached
over an ADB reverse tunnel. ZipVoice and Vocos are open models, served through `sherpa-onnx`.

The three Surge variants differ in size and speed; `zipvoice_distill_int8` is the practical
choice on CPU, and `zipvoice_gpu` needs a CUDA host. `vocos_24khz.onnx` is the shared
vocoder. These can live anywhere on the host — point the server at the directory.

## 11. Facial-expression recognition

`fer_ort/fer_emotion_clean.onnx` (244 KB) classifies an aligned face crop into the seven
basic emotions. It runs through ONNX Runtime on the board CPU and is deliberately tiny so it
does not contend with the DSP models.

Face *detection* does not use a learned model: `vision_worker.py` uses OpenCV's Haar cascade
(`haarcascade_frontalface_default.xml`), which ships with OpenCV itself.

## 12. Whisper Tiny — the default ASR backend

A Fibo-AI-Stack conversion of Whisper Tiny, loaded by `WhisperTinyCpuBackend`
(`app/src/asr/whisper_tiny_cpu_backend.py`). This is the ASR model the runtime actually uses:
`ASR_BACKEND=whisper_tiny_cpu` is the default in `deploy/start_nuanyu_runtime.sh`,
`config/nuanyu.env.example` and the runtime-config default, and `ASR_FALLBACK_BACKEND` is
`none` — there is no second ASR backend behind it. Whisper is open (MIT); the `.fmodel` here
is only a container conversion.

## Assets on the board that are NOT used

The production board carries a 47 MB `cv_models/` directory:

```
cv_models/yolov5_face.fmodel
cv_models/mediapipe_face.fmodel
cv_models/fibodet_base.fmodel
cv_models/openface.fmodel
```

**Nothing in this codebase references any of them.** Face detection is the OpenCV Haar
cascade and emotion classification is the ONNX model above. These are leftovers from an
earlier Fibocom CV-SDK experiment and can be deleted from a board to reclaim the space.

Also unused is the Fibocom ASR model `models/asr/fiboasr_base_v1_0711/` (item 4 above): no
module in this tree references it, and the tree contains no Fibocom ASR backend — `app/src/asr/`
holds only `asr_worker.py` and `whisper_tiny_cpu_backend.py`. The ASR model the runtime
actually loads is Whisper Tiny (§12). Unlike the `cv_models/` leftovers it is a vendor asset,
so it can only be reinstated by installing it on a board, not from this repository.

## Installing models on a new board

```bash
# From a board that already has them (USB is much faster than network):
adb pull /userdata_fibo/models      ./models
adb pull /userdata_fibo/tts_client  ./tts_client

# Then onto the new board:
adb push models      /userdata_fibo/models
adb push tts_client  /userdata_fibo/tts_client
```

> Under Git Bash, prefix `adb pull`/`adb push` with `MSYS_NO_PATHCONV=1` or the paths get
> rewritten.

### Verifying the install

After restarting the runtime and waiting for the web service to become ready (~40–70 s):

- `GET /api/status` should report `tts.backend` as `drizzle_matcha`.
- A conversation should produce audible speech from the **board** speaker.
- Speaking into the microphone should produce an `[ASR] text=` line in the log and increase
  `stats.success`.

### Converting your own models

Custom models are converted with the Fibocom AI Stack toolchain. Vendor documentation for
that toolchain is not redistributed here — request it from Fibocom.
