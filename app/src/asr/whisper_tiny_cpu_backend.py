#!/usr/bin/env python3
"""Whisper Tiny CPU ASR backend via Fibocom AudioAPI."""
import sys, os, time, wave

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")

# Try standard import first (when running inside nuanyu main process)
# Fall back to importlib (when running standalone)
_api_mod = None
try:
    from fiboaisdk import api_aisdk_py as _api_mod
except Exception:
    pass

if _api_mod is None:
    try:
        sys.path.insert(0, '/usr/local/lib/python3.8/dist-packages/fiboaisdk')
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            'api_aisdk_py',
            '/usr/local/lib/python3.8/dist-packages/fiboaisdk/api_aisdk_py.cpython-38-aarch64-linux-gnu.so')
        _api_mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_api_mod)
    except Exception:
        pass

class WhisperTinyCpuBackend:
    def __init__(self, model_path=None):
        self.model_path = model_path or os.path.join(
            NUANYU_ROOT, "asr_whisper_test", "models", "whisper_tiny_cpu.fmodel")
        self._au = _api_mod.api_audio_py
        self._license = _api_mod.license_py
        self._api = None
        self._loaded = False
        self._init_license()
        self._init_model()

    def _init_license(self):
        D = '/home/fibo/qcom_6490_license'
        def rf(p):
            with open(p, 'r' if p.endswith('.pem') else 'rb') as f:
                return f.read()
        k1 = rf(os.path.join(D, 'key1.pem'))
        k2 = rf(os.path.join(D, 'key2.pem'))
        k3 = rf(os.path.join(D, 'key3.pem'))
        ld = rf(os.path.join(D, 'license.bin')).decode('latin-1')
        self._license.Init(k1, k2, k3, ld)

    def _init_model(self):
        t0 = time.perf_counter()
        self._api = self._au.AudioAPI()
        ret = self._api.Init(self.model_path)
        self._init_ms = (time.perf_counter() - t0) * 1000
        self._loaded = (ret == 0)
        if self._loaded:
            print(f"[ASR] WhisperTinyCpuBackend ready ({self._init_ms:.0f}ms)")
        else:
            print(f"[ASR] WhisperTinyCpuBackend init FAILED: ret={ret}")

    @property
    def loaded(self):
        return self._loaded

    def transcribe(self, wav_path, timeout=30):
        if not self._loaded:
            return {"success": False, "text": "", "error": "Model not loaded"}
        if not os.path.exists(wav_path):
            return {"success": False, "text": "", "error": f"WAV not found: {wav_path}"}

        try:
            with wave.open(wav_path, 'rb') as w:
                audio_ms = w.getnframes() / w.getframerate() * 1000
        except Exception:
            audio_ms = 0

        audio = self._au.FiboAudio()
        audio.audio_sample_rate = 16000
        audio.audio_channel = 1
        audio.audio_format = self._au.FiboAudioFormat.FIBO_AUDIO_FORMAT_WAV
        audio.audio_path = wav_path
        audio.extra_params = ""

        result = self._au.ResultNlpAudio()
        t0 = time.perf_counter()
        ret = self._api.TranscribeSync(audio, result, timeout)
        inf_ms = (time.perf_counter() - t0) * 1000
        rtf = inf_ms / audio_ms if audio_ms > 0 else 0
        text = result.speech_text.strip() if result.speech_text else ""

        return {
            "success": ret == 0 and len(text) > 0,
            "text": text,
            "backend": "whisper_tiny_cpu",
            "audio_duration_ms": round(audio_ms, 1),
            "inference_ms": round(inf_ms, 1),
            "rtf": round(rtf, 3),
            "language": "zh",
            "fallback": False,
            "error": None if (ret == 0 and text) else f"ret={ret} text_empty={not text}",
        }

    def health(self):
        return {"loaded": self._loaded, "init_ms": round(self._init_ms, 1)}

    def close(self):
        if self._api:
            self._api.Release()
            self._api = None
            self._loaded = False
