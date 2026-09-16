#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Matcha-TTS standalone worker process (invoked by the Drizzle backend).

Background: the onnxruntime bundled with sherpa_onnx, 1.24.4, and the
libonnxruntime.so shipped with the board's fiboaisdk (1.22.0) share a SONAME and
conflict, so they cannot coexist in one process alongside DSP/ASR.
This worker loads sherpa_onnx Matcha in its own process and serves synthesis
requests over a stdin/stdout JSON line protocol, isolating onnxruntime.

Protocol:
  worker → drizzle: prints a single `READY` line once startup completes
  drizzle → worker: `{"text": "..."}\n`
  worker → drizzle: `{"ok": true, "wav": "...", "rate": N, "dur_ms": N, "synth_ms": N}\n`
                    or `{"ok": false, "error": "..."}\n`
The wav is written to the DRIZZLE_MATCHA_TMP directory; the drizzle side deletes it.
sherpa's noisy C++ logging writes straight to fd 1/2, so os.dup2 redirects them to
/dev/null, keeping stdout to protocol lines only.
"""
import contextlib
import json
import os
import sys
import time
import wave


@contextlib.contextmanager
def _quiet_fd():
    """Redirect fd 1/2 to /dev/null to swallow sherpa's C++ log noise."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        yield
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        os.close(sink)


# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")

MODEL_DIR = os.environ.get(
    "DRIZZLE_MATCHA_MODEL_DIR",
    os.path.join(
        NUANYU_ROOT,
        "models", "tts", "drizzle", "matcha-icefall-zh-baker",
    ),
)
TMP_DIR = os.environ.get("DRIZZLE_MATCHA_TMP", "/tmp/drizzle_matcha")


def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    import numpy as np
    import sherpa_onnx

    with _quiet_fd():
        m = sherpa_onnx.OfflineTtsMatchaModelConfig(
            acoustic_model=os.path.join(MODEL_DIR, "model-steps-3.onnx"),
            vocoder=os.path.join(MODEL_DIR, "vocos-22khz-univ.onnx"),
            lexicon=os.path.join(MODEL_DIR, "lexicon.txt"),
            tokens=os.path.join(MODEL_DIR, "tokens.txt"),
            dict_dir=os.path.join(MODEL_DIR, "dict"),
        )
        mc = sherpa_onnx.OfflineTtsModelConfig(matcha=m)
        mc.num_threads = 2
        tts = sherpa_onnx.OfflineTts(
            sherpa_onnx.OfflineTtsConfig(model=mc))
        tts.generate("你好", sid=0, speed=1.0)  # warm up

    counter = 0
    print("READY", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            text = str(req.get("text", "")).strip()
            if len(text) < 2:
                print(json.dumps({"ok": False, "error": "empty text"}),
                      flush=True)
                continue
            t0 = time.perf_counter()
            with _quiet_fd():
                audio = tts.generate(text, sid=0, speed=1.0)
                arr = np.clip(
                    np.asarray(audio.samples, dtype=np.float32), -1.0, 1.0)
                pcm = (arr * 32767.0).astype(np.int16)
                counter += 1
                out = os.path.join(
                    TMP_DIR, "m_%d_%d.wav" % (os.getpid(), counter))
                with wave.open(out, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(audio.sample_rate)
                    wf.writeframes(pcm.tobytes())
            synth_ms = (time.perf_counter() - t0) * 1000
            dur_ms = len(audio.samples) / float(audio.sample_rate) * 1000.0
            print(json.dumps({
                "ok": True, "wav": out, "rate": audio.sample_rate,
                "dur_ms": round(dur_ms, 1), "synth_ms": round(synth_ms, 1),
            }), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:240]}),
                  flush=True)


if __name__ == "__main__":
    main()
