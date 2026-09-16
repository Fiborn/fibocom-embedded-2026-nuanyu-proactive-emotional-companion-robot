#!/usr/bin/env python3
"""Board-only diagnostic for Fibocom Whisper accepted WAV durations."""

import math
import os
import struct
import sys
import tempfile
import wave

# Resolve the application directory from this file's location (tools/ and app/
# are siblings), so the probe runs from a checkout as well as on a board.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from src.asr.whisper_tiny_cpu_backend import WhisperTinyCpuBackend


def make_tone(path, seconds, rate=16000):
    frames = bytearray()
    for index in range(int(seconds * rate)):
        sample = int(2500 * math.sin(2 * math.pi * 440 * index / rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(bytes(frames))


def main():
    backend = WhisperTinyCpuBackend()
    print("model_loaded=%s" % backend.loaded, flush=True)
    for duration in (1, 2, 3, 5, 10, 15, 30):
        path = os.path.join(tempfile.gettempdir(), "asr_probe_%ss.wav" % duration)
        make_tone(path, duration)
        result = backend.transcribe(path, timeout=45)
        print(
            "duration=%ss success=%s error=%s inference_ms=%s"
            % (duration, result.get("success"), result.get("error"),
               result.get("inference_ms")),
            flush=True,
        )
        try:
            os.unlink(path)
        except OSError:
            pass
    backend.close()


if __name__ == "__main__":
    main()
