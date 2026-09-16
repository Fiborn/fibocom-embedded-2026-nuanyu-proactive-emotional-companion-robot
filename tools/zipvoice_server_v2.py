#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZipVoice GPU server V2 - multi voice-pack support
Intel Arc / DirectML acceleration, model kept resident in memory, reference
audio can be switched dynamically.

Usage:
  python zipvoice_server_v2.py

Environment variables:
  ZIPVOICE_MODEL_DIR   model directory (default ./zipvoice)
  ZIPVOICE_PORT        listen port (default 5017)
  ZIPVOICE_NUM_STEPS   diffusion steps (default 3)
  ZIPVOICE_NUM_THREADS number of CPU threads (default 4)
  ZIPVOICE_PROVIDER    onnxruntime provider (default directml)
  ZIPVOICE_DEBUG       debug mode (default 0)

API:
  GET  /health               health check
  GET  /status               status query
  POST /tts                  TTS synthesis {"text": "...", "voice_id": "..."}
  POST /voice/activate       activate a voice pack {"voice_id": "...", "reference_audio_b64": "...", "reference_text": "..."}
  POST /voice/preview        generate a preview {"voice_id": "...", "reference_audio_b64": "...", "reference_text": "..."}
  POST /voice/deactivate     deactivate a voice pack {"voice_id": "..."}
  POST /audio/convert        audio format conversion {"audio_b64": "...", "target_sr": 24000}
  POST /stop                 stop the current synthesis
"""
import io
import os
import json
import wave
import time
import queue
import base64
import tempfile
import threading
import subprocess
import http.server

import numpy as np

# --- configuration ---------------------------------------
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zipvoice')
# The model only lives under ~/zipvoice; when this file is copied out of the
# repository tools/ directory and run elsewhere the sibling directory has no
# model, so fall back to the home directory. This lets the server start with no
# ZIPVOICE_MODEL_DIR env var at all.
if not os.path.isdir(BASE):
    _home_zipvoice = os.path.join(os.path.expanduser('~'), 'zipvoice')
    if os.path.isdir(_home_zipvoice):
        BASE = _home_zipvoice
MODEL_DIR = os.environ.get('ZIPVOICE_MODEL_DIR', BASE)
MODEL = os.path.join(MODEL_DIR, 'sherpa-onnx-zipvoice-distill-int8-zh-en-emilia')
VOCODER = os.path.join(MODEL_DIR, 'vocos_24khz.onnx')

PORT = int(os.environ.get('ZIPVOICE_PORT', '5018'))
# Diffusion steps default to 3 (user-selected 2026-08-04): 2 steps occasionally
# produce slurred or nonsensical speech, and 4 steps are somewhat slow; 3 steps
# is the compromise - quality close to 4 steps while latency is only ~0.6s worse
# than 2 steps. This only affects surge custom voices.
NUM_STEPS = int(os.environ.get('ZIPVOICE_NUM_STEPS', '3'))
NUM_THREADS = int(os.environ.get('ZIPVOICE_NUM_THREADS', '8'))
# Measured on this machine (Arc 140T): the directml provider has compatibility
# problems and is ~2x slower than cpu, so the default is cpu - same model, same
# number of steps, identical quality and twice the speed (measured 2026-08-03).
PROVIDER = os.environ.get('ZIPVOICE_PROVIDER', 'cpu')
# Hard guard rail: even if an external launcher (such as the desktop EXE)
# explicitly requests directml, fall back to cpu.
if PROVIDER == 'directml':
    PROVIDER = 'cpu'
_DEBUG = os.environ.get('ZIPVOICE_DEBUG', '0') == '1'

TARGET_SR = 24000  # ZipVoice-distill target sample rate

# --- global state ----------------------------------------
_tts = None
_voices = {}           # voice_id -> {"audio": np.array, "sr": int, "text": str, "loaded_at": float}
_active_voice = None   # currently active voice_id
_synth_count = 0
_total_synth_time = 0.0
_lock = threading.Lock()
# ZipVoice generation is serialized because the model instance is not
# guaranteed to be re-entrant. HTTP handling remains concurrent so /health,
# /status and voice activation are never blocked behind a long synthesis.
_inference_lock = threading.Lock()
_stop_event = threading.Event()
_synth_queue = queue.Queue(maxsize=16)
_worker_started = False
_worker_lock = threading.Lock()
_current_task = None   # synthesis task currently in flight

# Synthesis result queue (voice_id, text) -> {"audio": np.array, "sr": int, "error": str}
_result_events = {}    # task_id → threading.Event
_result_data = {}      # task_id → result dict
_result_lock = threading.Lock()


def log(msg):
    ts = time.strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


# --- model management ------------------------------------

def _init_model():
    global _tts
    if _tts is not None:
        return

    import sherpa_onnx
    log(f'loading ZipVoice model... provider={PROVIDER}')
    t0 = time.perf_counter()

    # Validate model files
    encoder_path = os.path.join(MODEL, 'encoder.int8.onnx')
    decoder_path = os.path.join(MODEL, 'decoder.int8.onnx')
    tokens_path = os.path.join(MODEL, 'tokens.txt')
    data_dir_path = os.path.join(MODEL, 'espeak-ng-data')
    lexicon_path = os.path.join(MODEL, 'lexicon.txt')

    for label, p in [('encoder', encoder_path), ('decoder', decoder_path),
                      ('tokens', tokens_path), ('data_dir', data_dir_path),
                      ('lexicon', lexicon_path), ('vocoder', VOCODER)]:
        if not os.path.exists(p):
            raise FileNotFoundError(f'{label} not found: {p}')

    model_cfg = sherpa_onnx.OfflineTtsModelConfig(
        zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
            encoder=encoder_path,
            decoder=decoder_path,
            tokens=tokens_path,
            data_dir=data_dir_path,
            lexicon=lexicon_path,
            vocoder=VOCODER,
        ),
        num_threads=NUM_THREADS,
        debug=_DEBUG,
        provider=PROVIDER,
    )
    config = sherpa_onnx.OfflineTtsConfig(model=model_cfg)
    _tts = sherpa_onnx.OfflineTts(config)

    dt = time.perf_counter() - t0
    log(f'model loaded ({dt:.1f}s) provider={PROVIDER} threads={NUM_THREADS}')


def _warmup():
    """One warm-up inference to trigger GPU JIT compilation."""
    if _tts is None:
        return
    import sherpa_onnx
    log('warm-up inference...')
    t0 = time.perf_counter()
    # Use a very short reference audio for the warm-up
    dummy_audio = np.zeros(24000, dtype=np.float32)  # 1 second of silence
    gen = sherpa_onnx.GenerationConfig()
    gen.reference_audio = dummy_audio
    gen.reference_text = "测试"
    gen.reference_sample_rate = 24000
    gen.num_steps = NUM_STEPS
    try:
        _tts.generate("测试", gen)
        dt = time.perf_counter() - t0
        log(f'warm-up complete ({dt:.1f}s)')
    except Exception as e:
        log(f'warm-up failed (non-fatal): {e}')


# --- voice pack management -------------------------------

def _load_ref_audio(voice_id, audio_b64, ref_text):
    """Load reference audio from a base64 WAV and decode it to float32 numpy."""
    if not audio_b64 or not ref_text:
        raise ValueError("reference_audio_b64 and reference_text are required")

    wav_bytes = base64.b64decode(audio_b64)
    if len(wav_bytes) < 44:  # WAV header minimum
        raise ValueError("invalid WAV data (too short)")

    with wave.open(io.BytesIO(wav_bytes), 'rb') as f:
        sr = f.getframerate()
        ch = f.getnchannels()
        sw = f.getsampwidth()
        raw = f.readframes(f.getnframes())

    if len(raw) == 0:
        raise ValueError("audio contains no samples")

    # Parse to float32
    if sw == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"unsupported sample width: {sw}")

    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)

    duration = len(audio) / sr
    if duration < 0.5:
        raise ValueError(f"audio too short: {duration:.1f}s (min 0.5s)")
    if duration > 30:
        raise ValueError(f"audio too long: {duration:.1f}s (max 30s)")

    # Check for severe clipping
    peak = np.abs(audio).max()
    if peak > 0.99:
        log(f'[WARN] voice={voice_id} near-clipping peak={peak:.3f}')

    # Resample to target SR if needed
    if sr != TARGET_SR:
        audio = _resample_audio(audio, sr, TARGET_SR)
        sr = TARGET_SR

    audio_f32 = audio.astype(np.float32)
    log(f'voice={voice_id} loaded: {duration:.1f}s @ {sr}Hz, text="{ref_text[:30]}..."')
    return audio_f32, sr


def _resample_audio(audio, orig_sr, target_sr):
    """Simple linear resampling using numpy."""
    if orig_sr == target_sr:
        return audio
    from scipy import signal as scipy_signal
    num_samples = int(len(audio) * target_sr / orig_sr)
    return scipy_signal.resample(audio, num_samples)


def activate_voice(voice_id, audio_b64, ref_text):
    """Load the reference audio and cache it."""
    global _active_voice
    if not voice_id or not audio_b64 or not ref_text:
        raise ValueError("voice_id, reference_audio_b64, reference_text required")

    audio, sr = _load_ref_audio(voice_id, audio_b64, ref_text)

    with _lock:
        _voices[voice_id] = {
            "audio": audio,
            "sr": sr,
            "text": ref_text,
            "loaded_at": time.time(),
        }
        _active_voice = voice_id

    log(f'voice={voice_id} activated (total voices: {len(_voices)})')
    return {"voice_id": voice_id, "duration": round(len(audio) / sr, 2), "sample_rate": sr}


def deactivate_voice(voice_id):
    """Remove a cached voice pack."""
    global _active_voice
    with _lock:
        if voice_id in _voices:
            del _voices[voice_id]
            log(f'voice={voice_id} deactivated')
        if _active_voice == voice_id:
            _active_voice = next(iter(_voices), None)
    return {"voice_id": voice_id}


# --- TTS synthesis ---------------------------------------

def synthesize(text, voice_id=None):
    """Synthesize text with the given voice pack, return PCM float32 + sample_rate."""
    global _synth_count, _total_synth_time

    import sherpa_onnx
    _init_model()

    text = str(text).strip()
    if len(text) < 2:
        raise ValueError("text too short")

    # Resolve voice
    vid = voice_id or _active_voice
    with _lock:
        voice = _voices.get(vid) if vid else None
        if not voice and _voices:
            vid = next(iter(_voices))
            voice = _voices[vid]

    if not voice:
        raise RuntimeError("no voice pack available — upload and activate one first")

    ref_audio = voice["audio"]
    ref_text = voice["text"]
    ref_sr = voice["sr"]

    gen = sherpa_onnx.GenerationConfig()
    gen.reference_audio = ref_audio
    gen.reference_text = ref_text
    gen.reference_sample_rate = ref_sr
    gen.num_steps = NUM_STEPS

    t0 = time.perf_counter()
    try:
        with _inference_lock:
            audio = _tts.generate(text, gen)
    except Exception as e:
        raise RuntimeError(f"synthesis failed: {e}")

    synth_time = time.perf_counter() - t0

    samples = np.asarray(audio.samples, dtype=np.float32)
    sr = audio.sample_rate

    with _lock:
        _synth_count += 1
        _total_synth_time += synth_time

    dur = len(samples) / sr
    log(f'synth: "{text[:30]}..." -> {dur:.1f}s inference {synth_time:.2f}s RTF={synth_time/dur:.2f}x voice={vid}')
    return samples, sr, synth_time


def synthesize_to_wav(text, voice_id=None):
    """Synthesize and return WAV bytes."""
    samples, sr, synth_time = synthesize(text, voice_id)

    audio_i16 = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue(), synth_time, len(samples) / sr


# --- audio conversion ------------------------------------

def convert_audio(audio_b64, target_sr=None):
    """Convert audio of any format to 24kHz mono WAV using FFmpeg."""
    sr = target_sr or TARGET_SR
    raw = base64.b64decode(audio_b64)

    # Write temp input
    with tempfile.NamedTemporaryFile(suffix='.audio_in', delete=False) as f:
        f.write(raw)
        input_path = f.name

    output_path = input_path + '.wav'

    try:
        cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', input_path,
            '-ac', '1',           # mono
            '-ar', str(sr),       # target sample rate
            '-sample_fmt', 's16', # 16-bit PCM
            '-f', 'wav',
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)

        with open(output_path, 'rb') as f:
            wav_bytes = f.read()

        if len(wav_bytes) < 44:
            raise ValueError("FFmpeg produced empty output")

        # Validate
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
            out_sr = wf.getframerate()
            out_ch = wf.getnchannels()
            out_frames = wf.getnframes()
        if out_frames == 0:
            raise ValueError("converted audio has no samples")

        dur = out_frames / out_sr
        if dur < 0.5:
            raise ValueError(f"converted audio too short: {dur:.1f}s")

        return {
            "wav_b64": base64.b64encode(wav_bytes).decode('ascii'),
            "sample_rate": out_sr,
            "channels": out_ch,
            "duration": round(dur, 2),
            "size_bytes": len(wav_bytes),
        }

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg conversion failed: {e.stderr.decode()[:200]}")
    finally:
        for p in [input_path, output_path]:
            try:
                os.unlink(p)
            except Exception:
                pass


# --- HTTP request handler --------------------------------

class ZipVoiceHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        if _DEBUG:
            log(f'HTTP: {format % args}')

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        content_len = int(self.headers.get('Content-Length', 0))
        if content_len == 0:
            return {}
        raw = self.rfile.read(content_len)
        return json.loads(raw.decode('utf-8'))

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'OK')
        elif self.path == '/status':
            with _lock:
                avg = _total_synth_time / max(_synth_count, 1)
                voice_list = {
                    vid: {
                        "duration": round(len(v["audio"]) / v["sr"], 2),
                        "text": v["text"][:50],
                        "loaded_at": v["loaded_at"],
                    }
                    for vid, v in _voices.items()
                }
            status = {
                'state': 'ready' if _tts else 'loading',
                'model_ready': _tts is not None,
                'synth_count': _synth_count,
                'avg_synth_time': round(avg, 3),
                'num_steps': NUM_STEPS,
                'num_threads': NUM_THREADS,
                'provider': PROVIDER,
                'active_voice': _active_voice,
                'voices': voice_list,
                'voice_count': len(_voices),
            }
            self._send_json(status)
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        path = self.path.rstrip('/')

        try:
            if path == '/tts':
                self._handle_tts()
            elif path == '/voice/activate':
                self._handle_voice_activate()
            elif path == '/voice/preview':
                self._handle_voice_preview()
            elif path == '/voice/deactivate':
                self._handle_voice_deactivate()
            elif path == '/audio/convert':
                self._handle_audio_convert()
            elif path == '/stop':
                self._handle_stop()
            else:
                self._send_json({'error': 'not found'}, 404)
        except ValueError as e:
            self._send_json({'error': str(e)}, 400)
        except RuntimeError as e:
            self._send_json({'error': str(e)}, 500)
        except Exception as e:
            log(f'ERROR: {e}')
            import traceback
            traceback.print_exc()
            self._send_json({'error': str(e)}, 500)

    def _handle_tts(self):
        data = self._read_json()
        text = str(data.get('text', '')).strip()
        voice_id = data.get('voice_id') or None
        return_wav = data.get('return_wav', True)

        if len(text) < 2:
            self._send_json({'error': 'text too short'}, 400)
            return

        try:
            if return_wav:
                wav_bytes, synth_time, dur = synthesize_to_wav(text, voice_id)
                wav_b64 = base64.b64encode(wav_bytes).decode('ascii')
                self._send_json({
                    'ok': True,
                    'wav_b64': wav_b64,
                    'synth_time': round(synth_time, 3),
                    'audio_duration': round(dur, 3),
                    'rtf': round(synth_time / max(dur, 0.01), 3),
                    'text': text[:50],
                })
            else:
                samples, sr, synth_time = synthesize(text, voice_id)
                # Return as raw PCM base64
                pcm_i16 = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
                pcm_b64 = base64.b64encode(pcm_i16.tobytes()).decode('ascii')
                self._send_json({
                    'ok': True,
                    'pcm_b64': pcm_b64,
                    'sample_rate': sr,
                    'synth_time': round(synth_time, 3),
                })
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)}, 500)

    def _handle_voice_activate(self):
        data = self._read_json()
        voice_id = str(data.get('voice_id', '')).strip()
        audio_b64 = str(data.get('reference_audio_b64', '')).strip()
        ref_text = str(data.get('reference_text', '')).strip()

        if not voice_id:
            raise ValueError("voice_id required")
        if not audio_b64:
            raise ValueError("reference_audio_b64 required")
        if not ref_text:
            raise ValueError("reference_text required")

        result = activate_voice(voice_id, audio_b64, ref_text)
        self._send_json({'ok': True, **result})

    def _handle_voice_preview(self):
        """Generate a short preview utterance (\"Hello, this is my voice\")."""
        data = self._read_json()
        voice_id = str(data.get('voice_id', '')).strip()
        audio_b64 = str(data.get('reference_audio_b64', '')).strip()
        ref_text = str(data.get('reference_text', '')).strip()

        if not voice_id:
            raise ValueError("voice_id required")

        # Temporarily activate if not already
        if voice_id not in _voices:
            if not audio_b64 or not ref_text:
                raise ValueError("voice not loaded and no reference provided")
            activate_voice(voice_id, audio_b64, ref_text)

        preview_text = data.get('preview_text', '你好，这是我的声音。')
        try:
            wav_bytes, synth_time, dur = synthesize_to_wav(preview_text, voice_id)
            wav_b64 = base64.b64encode(wav_bytes).decode('ascii')
            self._send_json({
                'ok': True,
                'wav_b64': wav_b64,
                'synth_time': round(synth_time, 3),
                'audio_duration': round(dur, 3),
                'preview_text': preview_text,
            })
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)}, 500)

    def _handle_voice_deactivate(self):
        data = self._read_json()
        voice_id = str(data.get('voice_id', '')).strip()
        if not voice_id:
            raise ValueError("voice_id required")
        result = deactivate_voice(voice_id)
        self._send_json({'ok': True, **result})

    def _handle_audio_convert(self):
        data = self._read_json()
        audio_b64 = str(data.get('audio_b64', '')).strip()
        target_sr = data.get('target_sr') or None
        if not audio_b64:
            raise ValueError("audio_b64 required")
        result = convert_audio(audio_b64, target_sr)
        self._send_json({'ok': True, **result})

    def _handle_stop(self):
        global _current_task
        with _lock:
            _stop_event.set()
            _current_task = None
        self._send_json({'ok': True})


def main():
    print('=' * 55)
    print('  ZipVoice GPU TTS server V2 - multi voice pack')
    print(f'  model: {MODEL}')
    print(f'  port: {PORT}')
    print(f'  provider: {PROVIDER}')
    print(f'  num_steps: {NUM_STEPS}')
    print(f'  num_threads: {NUM_THREADS}')
    print(f'  target sample rate: {TARGET_SR}Hz')
    print('=' * 55)

    log('preloading model...')
    try:
        _init_model()
        _warmup()
        log('model ready, waiting for requests...')
    except Exception as e:
        log(f'model load failed: {e}')
        import traceback
        traceback.print_exc()
        log('will load lazily on the first request')

    server = http.server.ThreadingHTTPServer(
        ('0.0.0.0', PORT), ZipVoiceHandler)
    server.daemon_threads = True
    log(f'ZipVoice V2 started -> http://0.0.0.0:{PORT}')
    log('API: POST /tts | POST /voice/activate | POST /voice/preview | GET /status')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log('interrupt received, shutting down server...')
        server.shutdown()
        log('shut down')


if __name__ == '__main__':
    main()
