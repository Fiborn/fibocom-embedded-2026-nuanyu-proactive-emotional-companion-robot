#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SurgeLiteProvider — calls a PC-side ZipVoice GPU server over HTTP (optimized v2)

Optimizations (2026-07-23):
  1. urllib3 pool + Keep-Alive        → eliminates the per-request TCP handshake
  2. Pipeline prefetch                → synthesize sentence N+1 while N plays
  3. numpy audio resampling           → pure C vectorization replaces Python loops
  4. raw PCM pipe playback            → skips WAV re-encoding, feeds aplay directly
  5. Precise latency measurement      → fetch / convert / play / first_audio

Interface (compatible with DrizzleBackend / DoubaoTTSProvider):
    speak_async(text, callback) → bool
    stop()
    is_speaking() → bool
    shutdown()

Dependencies:
    - PC-side zipvoice_server_v2.py reachable at ZIPVOICE_SERVER_URL
    - board fibo_tts playback pipeline
    - urllib3, numpy (already installed on system Python 3.8)
"""
import os
import sys
import json
import time
import queue
import base64
import threading
import wave
import io as _io

import urllib3
import numpy as np
from src.tts.audio_router import output_mode, play_on_pc_bytes

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get('NUANYU_ROOT', '/userdata_fibo')
# The application's own directory (this file lives at app/src/tts/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── Config ───────────────────────────────────────────────
ZIPVOICE_SERVER_URL = os.environ.get(
    'ZIPVOICE_SERVER_URL', 'http://127.0.0.1:5017'
)
ZIPVOICE_TIMEOUT = int(os.environ.get('ZIPVOICE_TIMEOUT', '60'))

# Playback-thread stall guard: waiting on a seq longer than this many seconds
# (far beyond normal synthesis time) skips it, so one lost segment can't freeze
# the whole reply forever ("stops halfway through").
PLAY_STALL_TIMEOUT = float(os.environ.get('SURGE_PLAY_STALL_TIMEOUT', '60.0'))

VOICE_PACKS_DIR = os.environ.get(
    'SURGE_VOICE_PACKS_DIR',
    os.path.join(APP_DIR, 'data', 'voice_packs'),
)
ACTIVE_VOICE_FILE = os.environ.get(
    'SURGE_ACTIVE_VOICE_FILE',
    os.path.join(APP_DIR, 'data', 'active_surge_voice.json'),
)

sys.path.insert(0, os.path.join(NUANYU_ROOT, 'tts_client'))
import fibo_tts

# aplay device parameters
HPH_CARD = fibo_tts.HPH_ALSA_CARD
HPH_DEVICE = fibo_tts.HPH_ALSA_DEVICE
AUDIO_DEVICE = "hw:%d,%d" % (HPH_CARD, HPH_DEVICE)


# ─── HTTP session (urllib3 connection pool + Keep-Alive)───────────────

class HTTPSession:
    """Persistent HTTP session; connection reuse removes repeated TCP handshakes."""

    def __init__(self, base_url, timeout=60.0, max_retries=2, pool_size=4):
        self._base_url = base_url.rstrip('/')
        self._timeout = urllib3.Timeout(connect=5.0, read=float(timeout))
        self._retries = urllib3.Retry(
            total=max_retries,
            backoff_factor=0.1,
            status_forcelist=[500, 502, 503, 504],
        )
        self._pool = urllib3.PoolManager(
            num_pools=pool_size,
            maxsize=pool_size,
            timeout=self._timeout,
            retries=self._retries,
            block=False,
        )
        self._headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Connection': 'keep-alive',
        }

    def post_json(self, path, data, timeout=None):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        url = self._base_url + path
        try:
            resp = self._pool.request(
                'POST', url, body=body, headers=self._headers,
                timeout=urllib3.Timeout(connect=5.0, read=float(timeout or 60)),
            )
            return json.loads(resp.data.decode('utf-8'))
        except urllib3.exceptions.HTTPError as e:
            return {'ok': False, 'error': 'HTTP error: %s' % str(e)[:200]}
        except urllib3.exceptions.TimeoutError:
            return {'ok': False, 'error': 'Request timed out'}
        except Exception as e:
            return {'ok': False, 'error': str(e)[:200]}

    def get(self, path, timeout=None):
        url = self._base_url + path
        try:
            resp = self._pool.request(
                'GET', url, headers={'Connection': 'keep-alive'},
                timeout=urllib3.Timeout(connect=5.0, read=float(timeout or 10)),
            )
            raw = resp.data.decode('utf-8')
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return {'_raw': raw.strip()}
        except urllib3.exceptions.HTTPError as e:
            return {'ok': False, 'error': 'HTTP error: %s' % str(e)[:200]}
        except Exception as e:
            return {'ok': False, 'error': str(e)[:200]}

    def close(self):
        self._pool.clear()


# ─── Audio playback (numpy-accelerated HAL conversion + fibo_tts reliable path)───

def _play_wav_bytes(wav_bytes):
    """numpy-accelerated HAL format conversion, then playback via fibo_tts's aplay path.

    Same logic as fibo_tts._convert_wav_for_hal, but numpy replaces the Python loop.
    Writes a temporary WAV after conversion and hands it to fibo_tts._play_wav
    (avoids raw PCM compatibility issues).

    Returns: (ok: bool, parse_ms, convert_ms, play_ms)
    """
    if not wav_bytes:
        return False, 0, 0, 0

    try:
        # ── 1. Parse the raw WAV ──
        t0 = time.perf_counter()
        buf = _io.BytesIO(wav_bytes)
        with wave.open(buf, 'rb') as wf:
            sample_rate = wf.getframerate()
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw_frames = wf.readframes(wf.getnframes())
        parse_ms = (time.perf_counter() - t0) * 1000

        # ── 2. numpy vectorized resampling ──
        t1 = time.perf_counter()

        if sampwidth == 2:
            samples = np.frombuffer(raw_frames, dtype=np.int16)
        elif sampwidth == 4:
            floats = np.frombuffer(raw_frames, dtype=np.float32)
            samples = (floats * 32767).clip(-32768, 32767).astype(np.int16)
        else:
            return False, parse_ms, 0, 0

        if nchannels > 1:
            samples = samples.reshape(-1, nchannels).mean(axis=1).astype(np.int16)

        target_rate = 48000
        if sample_rate != target_rate:
            ratio = target_rate / float(sample_rate)
            if ratio == 2.0:
                samples = np.repeat(samples, 2)
            elif ratio == int(ratio) and ratio > 1:
                samples = np.repeat(samples, int(ratio))
            elif ratio < 1.0:
                step = int(round(1.0 / ratio))
                samples = samples[::step]
            else:
                old_len = len(samples)
                new_len = int(old_len * ratio)
                old_idx = np.linspace(0, old_len - 1, new_len)
                samples = np.interp(old_idx, np.arange(old_len),
                                    samples.astype(np.float32)).astype(np.int16)

        # mono → stereo interleave
        stereo = np.empty(len(samples) * 2, dtype=np.int16)
        stereo[0::2] = samples
        stereo[1::2] = samples

        convert_ms = (time.perf_counter() - t1) * 1000

        # ── 3. Playback ──
        # Browser fallback: keep the original unconverted WAV
        try:
            with open('/tmp/nuanyu_tts_output.wav', 'wb') as f:
                f.write(wav_bytes)
        except Exception:
            pass

        t2 = time.perf_counter()

        # PC speaker fallback mode (used when the board 3.5mm jack is broken)
        mode = output_mode()
        if mode == 'pc':
            ok = play_on_pc_bytes(wav_bytes)
        else:
            # Onboard HPH playback
            import tempfile
            hal_path = tempfile.mktemp(prefix='surge_hal_', suffix='.wav', dir='/tmp')
            with wave.open(hal_path, 'wb') as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(target_rate)
                wf.writeframes(stereo.tobytes())
            ok = bool(fibo_tts._play_wav(hal_path))
            try:
                os.unlink(hal_path)
            except Exception:
                pass
            if not ok and mode == 'auto':
                ok = play_on_pc_bytes(wav_bytes)
                if ok:
                    print('[SurgeLite] board unavailable; PC fallback OK',
                          flush=True)

        play_ms = (time.perf_counter() - t2) * 1000

        return ok, parse_ms, convert_ms, play_ms

    except Exception as e:
        print("[SurgeLite] _play_wav_bytes error: %s" % str(e), flush=True)
        return False, 0, 0, 0


# ─── Voice pack persistence ────────────────────────────────────────

class VoicePackStore:
    """File storage for voice packs."""

    _VOICE_ID_RE = __import__('re').compile(r'^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$')

    def __init__(self, base_dir=VOICE_PACKS_DIR):
        self._base_dir = os.path.realpath(base_dir)
        os.makedirs(self._base_dir, exist_ok=True)

    @classmethod
    def _validate_voice_id(cls, voice_id):
        if not isinstance(voice_id, str) or not cls._VOICE_ID_RE.match(voice_id):
            raise ValueError("invalid voice_id")

    def _voice_dir(self, voice_id):
        self._validate_voice_id(voice_id)
        resolved = os.path.realpath(os.path.join(self._base_dir, voice_id))
        if (not resolved.startswith(self._base_dir + os.sep)
                and resolved != self._base_dir):
            raise ValueError("voice_id escapes base directory")
        return resolved

    def _metadata_path(self, voice_id):
        return os.path.join(self._voice_dir(voice_id), 'metadata.json')

    def _ref_path(self, voice_id, filename='reference_normalized.wav'):
        return os.path.join(self._voice_dir(voice_id), filename)

    def _preview_path(self, voice_id):
        return os.path.join(self._voice_dir(voice_id), 'preview.wav')

    def _safe_id(self, name):
        import uuid
        import re
        safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:20]
        safe = safe.strip('_') or 'voice'
        return "v_%s_%s" % (safe, uuid.uuid4().hex[:8])

    def create(self, name, reference_audio_bytes, reference_text,
               original_filename='', original_format=''):
        voice_id = self._safe_id(name)
        voice_dir = self._voice_dir(voice_id)

        if os.path.exists(voice_dir):
            raise ValueError("voice_id collision: %s" % voice_id)

        os.makedirs(voice_dir, exist_ok=False)

        ref_path = self._ref_path(voice_id)
        with open(ref_path, 'wb') as f:
            f.write(reference_audio_bytes)

        with wave.open(os.fdopen(os.open(ref_path, os.O_RDONLY), 'rb'), 'rb') as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            frames = wf.getnframes()
        duration = frames / max(sr, 1)

        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        metadata = {
            'voice_id': voice_id, 'name': name,
            'reference_text': reference_text, 'created_at': now,
            'duration': round(duration, 2), 'sample_rate': sr,
            'channels': ch, 'format': 'wav',
            'original_filename': original_filename,
            'original_format': original_format,
            'preview_available': False, 'status': 'created',
        }
        self._write_atomic(self._metadata_path(voice_id), metadata)
        return voice_id, metadata

    def update_preview(self, voice_id, preview_wav_bytes):
        path = self._preview_path(voice_id)
        with open(path, 'wb') as f:
            f.write(preview_wav_bytes)
        meta = self.get(voice_id)
        if meta:
            meta['preview_available'] = True
            self._write_atomic(self._metadata_path(voice_id), meta)

    def update_status(self, voice_id, status, error=''):
        meta = self.get(voice_id)
        if meta:
            meta['status'] = status
            if error:
                meta['error'] = error
            self._write_atomic(self._metadata_path(voice_id), meta)

    def get(self, voice_id):
        path = self._metadata_path(voice_id)
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def list_all(self):
        voices = []
        if not os.path.exists(self._base_dir):
            return voices
        for name in os.listdir(self._base_dir):
            meta_path = os.path.join(self._base_dir, name, 'metadata.json')
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        voices.append(json.load(f))
                except Exception:
                    pass
        voices.sort(key=lambda v: v.get('created_at', ''), reverse=True)
        return voices

    def get_reference_audio_b64(self, voice_id):
        path = self._ref_path(voice_id)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode('ascii')

    def get_preview_wav(self, voice_id):
        path = self._preview_path(voice_id)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return f.read()

    def delete(self, voice_id):
        import shutil
        voice_dir = self._voice_dir(voice_id)
        if os.path.exists(voice_dir):
            shutil.rmtree(voice_dir)
            return True
        return False

    def _write_atomic(self, path, data):
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


# ─── Active voice pack persistence ─────────────────────────────────

def _load_active_voice():
    try:
        if os.path.exists(ACTIVE_VOICE_FILE):
            with open(ACTIVE_VOICE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('voice_id')
    except Exception:
        pass
    return None


def _save_active_voice(voice_id):
    os.makedirs(os.path.dirname(ACTIVE_VOICE_FILE), exist_ok=True)
    tmp = ACTIVE_VOICE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'voice_id': voice_id, 'updated_at': time.time()}, f)
    os.replace(tmp, ACTIVE_VOICE_FILE)


# ─── SurgeLiteProvider ───────────────────────────────────

class SurgeLiteProvider:
    """ZipVoice TTS Provider — pipeline-optimized v2"""

    def __init__(self):
        self._store = VoicePackStore()
        self._active_voice_id = _load_active_voice()
        self._server_voice_id = None   # the voice actually active on the server
        self._server_available = False
        self._loaded = False
        self._speaking = False
        self._lock = threading.Lock()
        # Synthesis task queue: items are (seq, text, callback). Enqueuing starts
        # synthesis (concurrent pool); playback thread plays results in seq order.
        self._req_queue = queue.Queue(maxsize=32)
        self._worker_started = False
        self._worker_lock = threading.Lock()
        self._pool_lock = threading.Lock()
        self._synth_pool = None

        # Concurrent synthesis window: board CPU is limited; 3 lanes measured best
        self._max_concurrent = int(os.environ.get('SURGE_MAX_CONCURRENT', '3'))

        # In-order result buffer: seq -> (wav_bytes, error, info, callback, submitted_at)
        self._results = {}
        self._next_play_seq = 0
        self._seq_counter = 0
        self._play_cond = threading.Condition(self._lock)
        # Cancellation signal (used by stop)
        self._cancel_event = threading.Event()
        # First-sentence exclusive gate: before the first sentence (seq=0) finishes
        # synthesizing, at most 1 later sentence runs in parallel with it
        # (pre-synthesizing during the playback window of a greeting like "<nickname>，"),
        # so full concurrency cannot steal resources and slow the first audio.
        # Once the first sentence completes everything runs in parallel → later
        # sentences are pre-synthesized during playback → inter-sentence gap is 0.
        # A condition variable + counter replaces the old BoundedSemaphore:
        # releasing unconditionally on seq=0 completion plus the in-flight worker's
        # release double-released, raising "ValueError: Semaphore released too many
        # times", which killed the synthesis thread and caused queue backlog,
        # growing waits, and "stops halfway through".
        self._first_syn_done = threading.Event()
        self._late_cond = threading.Condition()
        self._late_holders = 0   # later sentences holding the first-sentence slot now (≤1)
        # Text cache: fixed short phrases such as the opener "昵称，" are cached as
        # wav after the first synthesis and reused afterwards. The cache key MUST
        # include voice_id; otherwise switching voices reuses the old voice's opener.
        self._greeting_cache = {}

        # Persistent HTTP session
        self._http = HTTPSession(ZIPVOICE_SERVER_URL, timeout=ZIPVOICE_TIMEOUT)

        self._check_server(retries=1)  # fast check, no retry at init; retry on first use
        if self._server_available and self._active_voice_id:
            ok = self._activate_on_server(self._active_voice_id)
            if ok:
                self._server_voice_id = self._active_voice_id

    @property
    def loaded(self):
        return self._loaded

    @property
    def server_available(self):
        return self._server_available

    def _check_server(self, retries=3, delay=2.0):
        for i in range(retries):
            result = self._http.get('/health', timeout=5)
            if isinstance(result, dict):
                ok = (result.get('_raw') == 'OK' or result.get('state') == 'ready')
            else:
                ok = (result == 'OK')
            if ok:
                self._server_available = True
                self._loaded = True
                return True
            if i < retries - 1:
                print('[SurgeLite] server not ready, retry %d/%d...' % (i+2, retries), flush=True)
                import time as _t
                _t.sleep(delay)
        self._server_available = False
        self._loaded = False
        return False

    def _activate_on_server(self, voice_id):
        ref_b64 = self._store.get_reference_audio_b64(voice_id)
        meta = self._store.get(voice_id)
        if not ref_b64 or not meta:
            print("[SurgeLite] voice_id=%s not found locally" % voice_id, flush=True)
            return False

        result = self._http.post_json(
            '/voice/activate',
            {'voice_id': voice_id,
             'reference_audio_b64': ref_b64,
             'reference_text': meta['reference_text']},
            timeout=30,
        )
        ok = result.get('ok', False)
        if ok:
            print("[SurgeLite] voice=%s activated on server" % voice_id, flush=True)
        else:
            print("[SurgeLite] server activation failed: %s" % result.get('error', ''),
                  flush=True)
        return ok

    # ─── Public interface ────────────────────────────────────────

    def speak_async(self, text, callback=None, delay_ms=0):
        text_clean = str(text).strip()
        if not text_clean or len(text_clean) < 2:
            if callback:
                callback(False, "text too short")
            return False
        if not self._active_voice_id:
            if callback:
                callback(False, "no active voice pack")
            return False
        if not self._server_available:
            self._check_server(retries=1)  # fast check, no retry at init; retry on first use
            if not self._server_available:
                if callback:
                    callback(False, "ZipVoice server unavailable")
                return False

        # Enqueue means synthesize: assign a seq and submit to the synthesis pool
        # immediately; the playback thread plays in order. A segment no longer
        # waits for the previous one to finish playing before starting synthesis.
        with self._lock:
            if self._cancel_event.is_set():
                self._cancel_event.clear()  # new task arrived, clear the stop state
                self._first_syn_done.clear()  # new reply, reset the first-sentence gate
            # Safety net: no in-flight work (queue empty + result buffer empty)
            # means a new reply started, so reset the first-sentence gate. Even
            # when the coordinator sends no stop/new-batch signal, the first
            # sentence of every reply still gets exclusive synthesis (fast onset).
            if self._req_queue.empty() and not self._results:
                self._first_syn_done.clear()
            seq = self._seq_counter
            self._seq_counter += 1
            # submitted_at = enqueue time, for the true onset latency (submit → audio)
            submitted_at = time.perf_counter()
            try:
                # Tuple: (seq, text, callback, submitted_at, delay_ms)
                # With delay_ms>0 the playback thread waits out the delay before
                # playing (opener "昵称，" only: gives DeepSeek's first sentence a
                # reasoning window without breaking seq order).
                self._req_queue.put_nowait(
                    (seq, text_clean, callback, submitted_at, int(delay_ms or 0)))
            except Exception:
                if callback:
                    callback(False, "queue full")
                return False

        self._ensure_synth_pool()
        self._ensure_worker()
        return True

    def stop(self):
        """Stop current playback, clear the queue and result buffer, notify the server."""
        self._cancel_event.set()
        self._first_syn_done.clear()  # the next reply re-enters first-sentence exclusive mode
        with self._lock:
            self._speaking = False
            # Drop unsynthesized tasks (queue items are 5-tuples: seq, text, callback, submitted_at, delay_ms)
            while not self._req_queue.empty():
                try:
                    _, _, cb, _, _ = self._req_queue.get_nowait()
                    if cb:
                        cb(False, "stopped")
                    self._req_queue.task_done()
                except Exception:
                    break
            # Discard synthesized-but-unplayed results; the playback thread exits once woken
            self._results.clear()
            self._next_play_seq = 0
            self._seq_counter = 0
            self._play_cond.notify_all()
        # Tell the server to stop
        self._http.post_json('/stop', {}, timeout=5)

    def is_speaking(self):
        with self._lock:
            if self._speaking:
                return True
            return (not self._req_queue.empty()) or bool(self._results)

    def shutdown(self):
        self.stop()
        self._http.close()

    def health(self):
        return {
            "loaded": self._loaded,
            "server_available": self._server_available,
            "server_url": ZIPVOICE_SERVER_URL,
            "active_voice_id": self._active_voice_id,
            "voice_count": len(self._store.list_all()),
            "speaking": self._speaking,
        }

    # ─── Voice pack management ──────────────────────────────────────

    def create_voice(self, name, audio_b64, ref_text, original_filename=''):
        if not name or not name.strip():
            raise ValueError("语音包名称不能为空")
        if not ref_text or not ref_text.strip():
            raise ValueError("参考文本不能为空")
        if not audio_b64:
            raise ValueError("参考音频不能为空")

        name = name.strip()
        ref_text = ref_text.strip()

        convert_result = self._http.post_json(
            '/audio/convert', {'audio_b64': audio_b64}, timeout=30,
        )
        if not convert_result.get('ok'):
            raise ValueError("音频格式转换失败: %s" % convert_result.get('error', ''))

        normalized_b64 = convert_result['wav_b64']
        normalized_bytes = base64.b64decode(normalized_b64)
        duration = convert_result.get('duration', 0)

        if duration < 0.5:
            raise ValueError("音频时长过短: %.1f秒 (最少 0.5秒)" % duration)
        if duration > 30:
            raise ValueError("音频时长过长: %.1f秒 (最多 30秒)" % duration)

        voice_id, metadata = self._store.create(
            name=name, reference_audio_bytes=normalized_bytes,
            reference_text=ref_text, original_filename=original_filename,
            original_format='upload',
        )

        preview_result = self._http.post_json(
            '/voice/preview',
            {'voice_id': voice_id, 'reference_audio_b64': normalized_b64,
             'reference_text': ref_text},
            timeout=45,
        )
        if preview_result.get('ok') and preview_result.get('wav_b64'):
            preview_bytes = base64.b64decode(preview_result['wav_b64'])
            self._store.update_preview(voice_id, preview_bytes)
            self._store.update_status(voice_id, 'ready')
            print("[SurgeLite] voice=%s preview generated" % voice_id, flush=True)
        else:
            self._store.update_status(
                voice_id, 'preview_failed',
                "试听生成失败: %s" % preview_result.get('error', '')
            )
            print("[SurgeLite] voice=%s preview FAILED" % voice_id, flush=True)

        return voice_id, self._store.get(voice_id)

    def activate_voice(self, voice_id):
        """Activate the given voice pack. Notify the server first; update local state only on success."""
        meta = self._store.get(voice_id)
        if not meta:
            raise ValueError("voice_id not found: %s" % voice_id)

        # Notify the PC server first — on failure raise, leaving local state unchanged
        ok = self._activate_on_server(voice_id)
        if not ok:
            raise RuntimeError("server activation failed for %s" % voice_id)

        # On voice switch, stop the old voice's in-flight playback and drop its short-phrase cache.
        self.stop()
        with self._lock:
            self._greeting_cache.clear()

        old = self._active_voice_id
        self._active_voice_id = voice_id
        self._server_voice_id = voice_id
        _save_active_voice(voice_id)

        print("[SurgeLite] active voice: %s -> %s" % (old, voice_id), flush=True)
        return self._store.get(voice_id)

    def get_active_voice(self):
        if not self._active_voice_id:
            return None
        return self._store.get(self._active_voice_id)

    def list_voices(self):
        return self._store.list_all()

    def get_voice(self, voice_id):
        return self._store.get(voice_id)

    def delete_voice(self, voice_id):
        meta = self._store.get(voice_id)
        if not meta:
            raise ValueError("voice_id not found: %s" % voice_id)

        if self._active_voice_id == voice_id:
            self._http.post_json(
                '/voice/deactivate', {'voice_id': voice_id}, timeout=10,
            )
            self._active_voice_id = None
            self._server_voice_id = None
            _save_active_voice(None)

        self._store.delete(voice_id)
        return True

    def get_preview_url_base64(self, voice_id):
        wav_bytes = self._store.get_preview_wav(voice_id)
        if not wav_bytes:
            return None
        return base64.b64encode(wav_bytes).decode('ascii')

    # ─── Internal synthesis methods ────────────────────────────────────

    def _do_synthesize(self, text, voice_id=None):
        """Run HTTP TTS synthesis; returns (wav_bytes, error, info_dict)."""
        voice_id = voice_id or self._active_voice_id
        t_start = time.perf_counter()
        result = self._http.post_json(
            '/tts',
            {'text': text, 'voice_id': voice_id, 'return_wav': True},
            timeout=ZIPVOICE_TIMEOUT,
        )
        fetch_ms = (time.perf_counter() - t_start) * 1000

        if result.get('ok') and result.get('wav_b64'):
            self._server_available = True
            self._loaded = True
            t_decode = time.perf_counter()
            wav_bytes = base64.b64decode(result['wav_b64'])
            b64decode_ms = (time.perf_counter() - t_decode) * 1000

            info = {
                'synth_time': result.get('synth_time', fetch_ms / 1000.0),
                'audio_duration': result.get('audio_duration', 0),
                'rtf': result.get('rtf', 0),
                'fetch_ms': fetch_ms,
                'b64decode_ms': b64decode_ms,
            }
            return wav_bytes, '', info
        else:
            error = result.get('error', 'TTS failed')
            lowered = str(error).lower()
            if any(mark in lowered for mark in (
                    'connection', 'timed out', 'timeout', 'http error',
                    'remote end closed', 'refused')):
                self._server_available = False
                self._loaded = False
            return None, error, {'fetch_ms': fetch_ms}

    # ─── Internal pipeline: concurrent synthesis pool + in-order playback (replaces the old prefetch pipeline) ──

    def _ensure_synth_pool(self):
        """Lazily start the concurrent synthesis thread pool."""
        with self._pool_lock:
            if self._synth_pool is None:
                self._synth_pool = [
                    threading.Thread(target=self._synth_worker, daemon=True,
                                     name="SurgeLiteSynth%d" % i)
                    for i in range(self._max_concurrent)
                ]
                for t in self._synth_pool:
                    t.start()

    def _acquire_late_slot(self):
        """Request the exclusive slot before the first sentence completes; once it
        completes, or after a 30s timeout, pass straight through.

        Returns True = slot held (must be released after synthesis finishes),
        False = not held (passed through / timed out). Replaces the old
        BoundedSemaphore, ruling out the double-release thread crash at the root.
        """
        deadline = time.monotonic() + 30.0
        with self._late_cond:
            while (not self._first_syn_done.is_set()
                   and self._late_holders >= 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._late_cond.wait(timeout=remaining)
            if not self._first_syn_done.is_set():
                self._late_holders += 1
                return True
            return False

    def _release_late_slot(self):
        with self._late_cond:
            self._late_holders = max(0, self._late_holders - 1)
            self._late_cond.notify_all()

    def _synth_worker(self):
        """Synthesis thread: take a task → synthesize via the PC server → write the result to the in-order buffer."""
        while True:
            try:
                seq, text, callback, submitted_at, _delay_ms = self._req_queue.get()
            except Exception:
                break
            late = seq != 0 and not self._first_syn_done.is_set()
            held = False
            if late:
                # Before the first sentence completes, at most 1 later sentence
                # runs alongside it (pre-synthesizing during its playback window
                # without full concurrency stealing resources from the first
                # audio). Released once the first sentence completes.
                held = self._acquire_late_slot()
            try:
                self._synthesize_item(seq, text, callback, submitted_at, _delay_ms)
            except Exception as exc:
                # Safety net: no exception may kill the thread or drop a segment (a lost segment stalls playback)
                with self._play_cond:
                    if not self._cancel_event.is_set():
                        self._results[seq] = (None, str(exc)[:200], {},
                                              callback, submitted_at, text, _delay_ms)
                        self._play_cond.notify_all()
                print("[SurgeLite] synth item failed seq=%d: %s" %
                      (seq, str(exc)[:160]), flush=True)
            finally:
                if held:
                    self._release_late_slot()
            if seq == 0:
                # First sentence done synthesizing (pass or fail): release waiting later sentences + full concurrency
                self._first_syn_done.set()
                with self._late_cond:
                    self._late_cond.notify_all()
            try:
                self._req_queue.task_done()
            except Exception:
                pass

    def _synthesize_item(self, seq, text, callback, submitted_at, delay_ms=0):
        """One segment: cache/HTTP synthesis → result into the in-order buffer for
        the playback thread to consume by seq.

        Result tuple: (wav_bytes, error, info, callback, submitted_at, text, delay_ms)
        """
        voice_id = self._active_voice_id
        cache_key = (voice_id, text)
        cached = self._greeting_cache.get(cache_key)
        if cached is not None and not self._cancel_event.is_set():
            wav_bytes, error, info = cached, '', {'fetch_ms': 0, 'cached': True}
        else:
            try:
                wav_bytes, error, info = self._do_synthesize(text, voice_id=voice_id)
            except Exception as e:
                wav_bytes, error, info = None, str(e)[:200], {}
        with self._play_cond:
            if self._cancel_event.is_set():
                # Tasks completing after stop are dropped
                if callback:
                    try:
                        callback(False, "stopped")
                    except Exception:
                        pass
            else:
                # Keep the enqueue time so playback can compute true onset latency (submit→audio)
                self._results[seq] = (wav_bytes, error, info, callback,
                                      submitted_at, text, int(delay_ms or 0))
                self._play_cond.notify_all()
        # Cache: short phrases (≤6 chars, fixed content such as openers) that synthesized OK → store for reuse
        if wav_bytes and len(text) <= 6 and len(self._greeting_cache) < 32:
            self._greeting_cache[cache_key] = wav_bytes

    def _ensure_worker(self):
        with self._worker_lock:
            if not self._worker_started:
                self._worker_started = True
                threading.Thread(target=self._play_worker, daemon=True,
                                 name="SurgeLiteQ").start()

    def _play_worker(self):
        """Playback thread: consume results in seq order, seamless between sentences.

        Metric meanings:
          fetch:   HTTP round trip + PC synthesis time
          parse:   WAV header parsing time
          convert: numpy resampling + format conversion time
          play:    aplay playback time (incl. small buffer, ≈ audio duration)
          first:   enqueue → first audio reaching the speaker (the key metric!)
        """
        while True:
            with self._play_cond:
                stall_since = None
                while (self._next_play_seq not in self._results
                       and not self._cancel_event.is_set()):
                    if stall_since is None:
                        stall_since = time.monotonic()
                    elif time.monotonic() - stall_since > PLAY_STALL_TIMEOUT:
                        # Stall guard: a seq's result went missing (upstream never
                        # drops segments in theory, but defend anyway) → skip that
                        # seq so the whole reply never freezes ("stops halfway").
                        print("[SurgeLite] stall-guard skip seq=%d" %
                              self._next_play_seq, flush=True)
                        break
                    self._play_cond.wait(timeout=0.5)
                if self._cancel_event.is_set():
                    # After stop: clear leftovers and leave the wait loop for the next batch
                    self._results.clear()
                    self._cancel_event.wait(timeout=0.2)
                    if self._cancel_event.is_set():
                        continue
                item = self._results.pop(self._next_play_seq, None)
                if item is None:
                    # Stall guard fired or pop raced: advance to the next seq
                    self._next_play_seq += 1
                    continue
                self._next_play_seq += 1
                (wav_bytes, error, synth_info, callback,
                 submitted_at, text, delay_ms) = item

            ok = False
            first_audio_ms = 0
            try:
                with self._lock:
                    self._speaking = True

                # Opener delay: once audio is ready, wait out delay_ms (relative
                # to the enqueue time) before playing. While waiting, the synthesis
                # pool finishes later segments → they follow the first seamlessly;
                # wait_ms (first_audio) naturally includes this delay → the
                # frontend shows the real onset.
                if wav_bytes and delay_ms:
                    elapsed_ms = (time.perf_counter() - submitted_at) * 1000
                    remain_ms = int(delay_ms) - elapsed_ms
                    if remain_ms > 0:
                        time.sleep(remain_ms / 1000.0)

                # Pre-playback wait = synthesis wait (already blocked on cond if not ready)
                wait_ms = (time.perf_counter() - submitted_at) * 1000

                parse_ms = 0
                convert_ms = 0
                play_ms = 0
                if wav_bytes:
                    ok, parse_ms, convert_ms, play_ms = _play_wav_bytes(wav_bytes)
                    if not ok:
                        error = error or "playback failed"
                else:
                    error = error or "synthesis failed"

                fetch_ms = synth_info.get('fetch_ms', 0)
                b64decode_ms = synth_info.get('b64decode_ms', 0)
                dur = synth_info.get('audio_duration', 0)
                rtf = synth_info.get('rtf', 0)

                # First-audio latency = enqueue → playback start
                first_audio_ms = wait_ms
                board_overhead_ms = parse_ms + convert_ms + b64decode_ms

                status = "OK" if ok else "FAIL"
                print(
                    "[SurgeLite] %s chars=%d | fetch=%.0fms decode=%.0fms "
                    "parse=%.0fms convert=%.0fms play=%.0fms | "
                    "first_audio=%.0fms wait=%.0fms overhead=%.0fms | "
                    "dur=%.1fs rtf=%.2f err=%s" % (
                        status, len(text),
                        fetch_ms, b64decode_ms, parse_ms, convert_ms, play_ms,
                        first_audio_ms, wait_ms, board_overhead_ms,
                        dur, rtf, str(error)[:120]
                    ), flush=True)

            except Exception as e:
                error = str(e)[:200]
            finally:
                with self._lock:
                    self._speaking = False

            if callback:
                try:
                    print("[Surge] play seq=%d calling cb (first_audio=%d)" % (self._next_play_seq - 1, first_audio_ms), flush=True)
                    surge_latency = int(first_audio_ms) if first_audio_ms else 0
                    try:
                        with open("/tmp/_last_tts_ms", "w") as f: f.write(str(surge_latency))
                    except: pass
                    callback(ok, error, {"first_audio_ms": surge_latency, "backend": "surge"})
                except Exception:
                    pass


# ─── Global singleton ────────────────────────────────────────────

_global_surge = None
_global_lock = threading.Lock()


def get_surge_provider():
    global _global_surge
    if _global_surge is not None:
        return _global_surge
    with _global_lock:
        if _global_surge is not None:
            return _global_surge
        _global_surge = SurgeLiteProvider()
        return _global_surge


# ─── speak_async adapter interface ────────────────────────────────

def surge_speak_async(text, callback=None, delay_ms=0):
    provider = get_surge_provider()
    return provider.speak_async(text, callback, delay_ms=delay_ms)


def surge_stop():
    provider = get_surge_provider()
    provider.stop()


def surge_is_speaking():
    provider = get_surge_provider()
    return provider.is_speaking()


def surge_shutdown():
    global _global_surge
    if _global_surge is not None:
        _global_surge.shutdown()
        _global_surge = None
