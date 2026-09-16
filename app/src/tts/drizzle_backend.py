#!/usr/bin/env python3
"""Drizzle: local TTS backend; synthesis runs through Matcha-TTS (sherpa_onnx, separate worker process).

Background: the onnxruntime bundled with sherpa_onnx (1.24.4) and the
libonnxruntime.so shipped with the board's fiboaisdk (1.22.0) share a SONAME and
conflict, so they cannot coexist in one process alongside DSP/ASR. Matcha
synthesis therefore runs in a separate process, matcha_worker.py, served over a
stdin/stdout JSON line protocol; that isolates onnxruntime, and the main process
only hands the WAV to the playback thread.
The backend structure (synthesis/playback two-thread pipeline) is unchanged,
with no gap between sentences.
"""

import json
import os
import queue
import shutil as _shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

from src.tts.audio_router import play_file_board_first

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")
# The application's own directory (this file lives at app/src/tts/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_ROOT = APP_DIR
WORKER_SCRIPT = os.path.join(APP_ROOT, "src", "tts", "matcha_worker.py")
MATCHA_TMP = os.environ.get("DRIZZLE_MATCHA_TMP", "/tmp/drizzle_matcha")
MODEL_DIR = os.environ.get(
    "DRIZZLE_MATCHA_MODEL_DIR",
    os.path.join(NUANYU_ROOT, "models", "tts", "drizzle", "matcha-icefall-zh-baker"),
)


def _wav_duration_ms(path):
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate()) * 1000
    except Exception:
        return 0.0


class DrizzleBackend:
    """A local Matcha-TTS backend: synthesis/playback two-thread pipeline, no cloud fallback."""

    def __init__(self):
        self._proc = None
        self._inference_lock = threading.Lock()
        self._loaded = False
        self._init_ms = 0.0
        self._queue = queue.Queue(maxsize=16)   # pending synthesis: (text, callback)
        self._ready = queue.Queue(maxsize=8)    # synthesized, awaiting playback: (wav_path, callback, synth_ms)
        self._worker_started = False
        self._worker_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Short-phrase cache (fixed content such as the opener "昵称，"): the wav is
        # cached after the first synthesis, so onset ≈ 0ms.
        self._cache_dir = "/tmp/drizzle_cache"
        self._cache = {}   # text -> cached wav path
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
        except Exception:
            pass
        try:
            os.makedirs(MATCHA_TMP, exist_ok=True)
        except Exception:
            pass
        self._init()

    def _init(self):
        """Start the Matcha worker subprocess and wait for READY."""
        started = time.perf_counter()
        try:
            env = dict(os.environ)
            env["DRIZZLE_MATCHA_MODEL_DIR"] = MODEL_DIR
            env["DRIZZLE_MATCHA_TMP"] = MATCHA_TMP
            self._proc = subprocess.Popen(
                [sys.executable, WORKER_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
            ready_line = self._proc.stdout.readline().strip()
            self._loaded = ready_line == "READY"
            self._init_ms = (time.perf_counter() - started) * 1000
            if self._loaded:
                print("[Drizzle] Matcha-TTS ready init=%.0fms" % self._init_ms,
                      flush=True)
            else:
                print("[Drizzle] Matcha-TTS init failed (got %r)"
                      % ready_line, flush=True)
        except Exception as exc:
            self._loaded = False
            print("[Drizzle] Matcha init failed: %s" % str(exc)[:240], flush=True)

    @property
    def loaded(self):
        return (self._loaded
                and self._proc is not None
                and self._proc.poll() is None)

    def _request(self, text):
        """Send text to the matcha worker; returns (ok, wav_path, rate, synth_ms). Thread-safe."""
        if self._proc is None or self._proc.poll() is not None:
            return False, "matcha worker not running", 0, 0
        with self._inference_lock:
            try:
                self._proc.stdin.write(json.dumps({"text": text}) + "\n")
                self._proc.stdin.flush()
                resp_line = self._proc.stdout.readline()
                if not resp_line:
                    return False, "matcha worker closed", 0, 0
                resp = json.loads(resp_line)
                if resp.get("ok"):
                    return True, resp["wav"], int(resp.get("rate", 22050)), \
                        float(resp.get("synth_ms", 0))
                return False, resp.get("error", "matcha error"), 0, 0
            except Exception as exc:
                return False, str(exc)[:240], 0, 0

    def synthesize(self, text, output_path=None):
        if not self.loaded:
            return {"success": False, "error": "Drizzle 后端(Matcha-TTS)未加载"}
        text = str(text).strip()
        if len(text) < 2:
            return {"success": False, "error": "empty text"}

        wav_path = output_path or tempfile.mktemp(
            prefix="drizzle_", suffix=".wav", dir="/tmp")
        started = time.perf_counter()
        try:
            # Short-phrase cache hit (fixed content such as the opener "昵称，"): copy the cached wav, onset ≈ 0ms.
            cached = self._cache.get(text)
            if cached and os.path.exists(cached):
                try:
                    _shutil.copyfile(cached, wav_path)
                    duration_ms = _wav_duration_ms(wav_path)
                    return {
                        "success": True,
                        "backend": "drizzle_matcha_cached",
                        "audio_path": wav_path,
                        "sample_rate": 22050,
                        "duration_ms": round(duration_ms, 1),
                        "synthesis_ms": 0,
                        "rtf": 0,
                    }
                except Exception:
                    pass  # a failed copy counts as a cache miss and re-synthesizes

            ok, worker_wav, rate, synth_ms = self._request(text)
            if not ok:
                return {"success": False, "error": str(worker_wav)[:240]}
            # Copy the worker's wav to the local output path (this backend owns caching/cleanup)
            _shutil.copyfile(worker_wav, wav_path)
            # Delete the worker-side temp wav (otherwise /tmp (tmpfs=RAM) grows without bound)
            try:
                os.remove(worker_wav)
            except OSError:
                pass
            synthesis_ms = (time.perf_counter() - started) * 1000
            duration_ms = _wav_duration_ms(wav_path)
            # Short phrases (≤6 chars) go into the cache for reuse
            if len(text) <= 6 and len(self._cache) < 32:
                try:
                    cache_path = os.path.join(
                        self._cache_dir,
                        "drizzle_%d_%d.wav" % (len(self._cache), len(text)))
                    _shutil.copyfile(wav_path, cache_path)
                    self._cache[text] = cache_path
                except Exception:
                    pass
            return {
                "success": True,
                "backend": "drizzle_matcha",
                "audio_path": wav_path,
                "sample_rate": rate,
                "duration_ms": round(duration_ms, 1),
                "synthesis_ms": round(synthesis_ms, 1),
                "rtf": round(synthesis_ms / duration_ms, 3) if duration_ms else 0,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)[:240]}

    def speak_async(self, text, callback=None):
        if not self.loaded or len(str(text).strip()) < 2:
            if callback:
                callback(False, "Drizzle 后端不可用或文本为空")
            return False
        try:
            self._queue.put_nowait((str(text).strip(), callback))
        except queue.Full:
            if callback:
                callback(False, "Drizzle queue is full")
            return False
        self._ensure_worker()
        return True

    def _ensure_worker(self):
        with self._worker_lock:
            if not self._worker_started:
                self._worker_started = True
                threading.Thread(target=self._synth_worker, daemon=True,
                                 name="DrizzleSynth").start()
                threading.Thread(target=self._play_worker, daemon=True,
                                 name="DrizzlePlay").start()

    # ── Synthesis thread: serial Matcha synthesis, results go to _ready for playback ──
    def _synth_worker(self):
        while True:
            text, callback = self._queue.get()
            # A new segment resets stop (consistent with queue semantics: stop
            # only clears unprocessed items, later sentences proceed normally).
            self._stop_event.clear()
            ok = False
            error = ""
            wav_path = None
            synth_ms = 0
            try:
                result = self.synthesize(text)
                ok = bool(result.get("success"))
                error = result.get("error", "")
                wav_path = result.get("audio_path")
                synth_ms = result.get("synthesis_ms", 0)
                if ok and not self._stop_event.is_set():
                    # Synthesized → hand to the playback thread; synthesis(N+1) runs
                    # parallel to playback(N), seamless between sentences.
                    self._ready.put((wav_path, callback, synth_ms))
                    wav_path = None  # ownership passes to the playback thread
                    continue
            except Exception as exc:
                error = str(exc)[:240]
            finally:
                if wav_path:
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                if callback and not ok:
                    try:
                        callback(ok, error, {"first_audio_ms": 0, "backend": "drizzle"})
                    except Exception:
                        pass
                self._queue.task_done()

    # ── Playback thread: serial playback of synthesized WAVs; the completion callback releases coordinator pending ──
    def _play_worker(self):
        _tts_client_dir = os.path.join(NUANYU_ROOT, "tts_client")
        if _tts_client_dir not in sys.path:
            sys.path.insert(0, _tts_client_dir)
        import fibo_tts

        while True:
            wav_path, callback, synth_ms = self._ready.get()
            ok = False
            error = ""
            tts_latency_ms = 0
            try:
                if not self._stop_event.is_set():
                    ok, output_target = play_file_board_first(
                        wav_path,
                        fibo_tts._play_wav,
                    )
                    print("[Drizzle] playback target=%s ok=%s"
                          % (output_target, ok), flush=True)
                    # HPH always-on: first audio ≈ synth_time + aplay startup (~40ms)
                    tts_latency_ms = int(synth_ms + 40)
                    if not ok:
                        error = "board speaker playback failed"
            except Exception as exc:
                error = str(exc)[:240]
            finally:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
                if callback:
                    try:
                        callback(ok, error, {"first_audio_ms": tts_latency_ms, "backend": "drizzle"})
                    except Exception:
                        pass
                self._ready.task_done()

    def stop(self):
        self._stop_event.set()
        # Clear pending synthesis
        while True:
            try:
                _, callback = self._queue.get_nowait()
                if callback:
                    callback(False, "stopped")
                self._queue.task_done()
            except queue.Empty:
                break
        # Clear synthesized-but-unplayed
        while True:
            try:
                wav_path, callback, _ = self._ready.get_nowait()
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
                if callback:
                    callback(False, "stopped")
                self._ready.task_done()
            except queue.Empty:
                break

    def health(self):
        return {
            "loaded": self.loaded,
            "backend": "drizzle_matcha",
            "model": os.path.basename(MODEL_DIR),
            "sample_rate": 22050,
            "init_ms": round(self._init_ms, 1),
            "queued": self._queue.qsize(),
            "ready": self._ready.qsize(),
            "worker_alive": bool(self._proc is not None
                                 and self._proc.poll() is None),
        }

    def close(self):
        self.stop()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._loaded = False


# Compatibility for older imports.
PiperBackend = DrizzleBackend
