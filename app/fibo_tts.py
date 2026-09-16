# -*- coding: utf-8 -*-
"""
fibo_tts.py — Board-local TTS module for Fibocom SC171V3
Uses Fibocom AI SDK api_audio_py.SpeechSynthesisSync (official API)
Fallback: espeak-ng offline TTS

Usage:
  from fibo_tts import fibo_tts_speak, fibo_tts_speak_async
  fibo_tts_speak("Hello, I am Nuanyu")
"""

import os
import sys
import json
import subprocess
import threading
import queue
import ctypes
import wave
import struct
import time
import contextlib


@contextlib.contextmanager
def _silence_sdk(sink_path="/dev/null"):
    """Silence the vendor SDK's C-level fd 1/2 logging during a noisy call."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    sink = None
    try:
        sink = os.open(sink_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        yield
    finally:
        try:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
        except Exception:
            pass
        try:
            os.close(saved_out)
            os.close(saved_err)
        except Exception:
            pass
        if sink is not None:
            try:
                os.close(sink)
            except Exception:
                pass

# ==== Path config ====
# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")

TTS_OUTPUT_DIR = "/tmp/tts_output"
os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)

FIBO_MODEL_PATH = os.path.join(NUANYU_ROOT, "tts_client", "tts_models")
LICENSE_DIR = "/home/fibo/qcom_6490_license"

# Optional PC playback fallback. Disabled by default because port 5005 is used
# by the board-side TTS health/control page in this project.
PC_PLAY_URL = os.environ.get("FIBO_TTS_PC_URL", "http://127.0.0.1:5015/play")
PC_PLAY_FALLBACK = os.environ.get("FIBO_TTS_PC_FALLBACK", "0") == "1"
PC_PLAY_ONLY = os.environ.get("FIBO_TTS_PC_ONLY", "0") == "1"
TTS_ENGINE_NAME = os.environ.get("FIBO_TTS_ENGINE", "").strip().lower()
PIPER_LOCAL_ENABLED = (TTS_ENGINE_NAME in ("piper", "zipvoice") or
                       os.environ.get("FIBO_TTS_PIPER", "0") == "1")
PIPER_SERVER_URL = os.environ.get("FIBO_PIPER_SERVER_URL", "http://127.0.0.1:5016/speak")
PIPER_INPROC = os.environ.get("FIBO_PIPER_INPROC", "0") == "1"
_piper_engine = None
_piper_lock = threading.Lock()
_tts_muted = threading.Event()

_status_lock = threading.Lock()
_tts_status = {
    "state": "init",
    "last_text": "",
    "last_ok": False,
    "last_error": "",
    "last_started": 0,
    "last_finished": 0,
    "queue_size": 0,
    "sdk_ready": False,
}


def _set_status(**kwargs):
    with _status_lock:
        _tts_status.update(kwargs)


def fibo_tts_get_status():
    with _status_lock:
        data = dict(_tts_status)
    data["sdk_ready"] = FIBO_SDK_AVAILABLE
    data["queue_size"] = _tts_queue.qsize() if "_tts_queue" in globals() else 0
    return data


def fibo_tts_set_muted(muted):
    """Hard-mute every TTS backend and discard speech queued before sleep."""
    if muted:
        _tts_muted.set()
        pending_queue = globals().get("_tts_queue")
        if pending_queue is not None:
            while True:
                try:
                    pending_queue.get_nowait()
                    pending_queue.task_done()
                except queue.Empty:
                    break
        try:
            os.remove('/tmp/nuanyu_tts_output.wav')
        except OSError:
            pass
        _set_status(state="muted", queue_size=0)
    else:
        _tts_muted.clear()
        _set_status(state="ready", last_error="")

FIBO_SDK_AVAILABLE = False
_fibo_audio_api = None
_audio_api_module = None
FIBO_TTS_SYNTH_TIMEOUT = float(os.environ.get("FIBO_TTS_SYNTH_TIMEOUT", "12"))
_sdk_call_lock = threading.Lock()
_sdk_failed = threading.Event()


def fibo_tts_synthesize_to_wav(text, wav_path, lang="zh", timeout=None):
    """Run the blocking DSP call behind a bounded wait.

    The vendor call cannot be cancelled safely.  If it stops returning, its
    daemon thread is quarantined and all later calls fail fast until the
    runtime supervisor performs a clean restart.
    """
    timeout = FIBO_TTS_SYNTH_TIMEOUT if timeout is None else float(timeout)
    if _sdk_failed.is_set():
        return {"success": False, "error": "Fibocom DSP is quarantined"}
    if not FIBO_SDK_AVAILABLE or _fibo_audio_api is None:
        return {"success": False, "error": "Fibocom DSP is not initialized"}
    if not _sdk_call_lock.acquire(timeout=min(2.0, max(0.1, timeout))):
        return {"success": False, "error": "Fibocom DSP is busy"}
    if _sdk_failed.is_set():
        _sdk_call_lock.release()
        return {"success": False, "error": "Fibocom DSP is quarantined"}

    release_guard = threading.Lock()
    released = [False]

    def _release_sdk_lock():
        with release_guard:
            if not released[0]:
                released[0] = True
                _sdk_call_lock.release()

    try:
        output_audio = _audio_api_module.FiboAudio()
        output_audio.audio_sample_rate = 16000
        output_audio.audio_channel = 1
        output_audio.audio_format = _audio_api_module.FiboAudioFormat.FIBO_AUDIO_FORMAT_WAV
        output_audio.audio_path = wav_path
        output_audio.extra_params = json.dumps({"language": lang})
        done = threading.Event()
        result = {"status": None, "error": ""}
    except Exception as exc:
        _release_sdk_lock()
        return {"success": False, "error": str(exc)}

    def _call_sdk():
        try:
            with _silence_sdk():
                result["status"] = _fibo_audio_api.SpeechSynthesisSync(text, output_audio)
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            done.set()
            _release_sdk_lock()

    try:
        threading.Thread(target=_call_sdk, daemon=True, name="FiboTTSSDK").start()
    except Exception as exc:
        _release_sdk_lock()
        return {"success": False, "error": str(exc)}
    if not done.wait(timeout=max(0.1, timeout)):
        _sdk_failed.set()
        _release_sdk_lock()
        error = "Fibocom DSP synthesis timed out after %.1fs" % timeout
        _set_status(state="synthesis_timeout", sdk_ready=False,
                    last_ok=False, last_error=error, last_finished=time.time())
        try:
            with open(os.path.join(NUANYU_ROOT, ".nuanyu_tts_unhealthy"), "w") as marker:
                marker.write(error + "\n")
        except OSError:
            pass
        print("[TTS] %s; quarantining SDK until restart" % error, flush=True)
        return {"success": False, "error": error, "timeout": True}
    if result["error"]:
        return {"success": False, "error": result["error"]}
    status = result["status"]
    ok = (status == 0 and os.path.exists(wav_path)
          and os.path.getsize(wav_path) > 100)
    return {"success": ok, "status": status,
            "error": "" if ok else "Fibocom status=%s" % status}

def _init_license():
    """Initialize Fibocom SDK license from standard location"""
    from fiboaisdk import api_aisdk_py
    lic_api = api_aisdk_py.license_py

    key1_path = os.path.join(LICENSE_DIR, "key1.pem")
    key2_path = os.path.join(LICENSE_DIR, "key2.pem")
    key3_path = os.path.join(LICENSE_DIR, "key3.pem")
    lic_path = os.path.join(LICENSE_DIR, "license.bin")

    for p in [key1_path, key2_path, key3_path, lic_path]:
        if not os.path.exists(p):
            print("[TTS] License file missing: {}".format(p))
            return False

    with open(key1_path, 'r') as f:
        key1 = f.read()
    with open(key2_path, 'r') as f:
        key2 = f.read()
    with open(key3_path, 'r') as f:
        key3 = f.read()
    with open(lic_path, 'rb') as f:
        lic_data = f.read().decode('latin-1')

    with _silence_sdk():
        ret = lic_api.Init(key1, key2, key3, lic_data)
    if ret == 0:
        print("[TTS] Fibocom SDK License OK")
        return True
    else:
        print("[TTS] License Init failed: {}".format(ret))
        return False


def _init_fibo_tts():
    """Initialize Fibocom TTS engine"""
    global FIBO_SDK_AVAILABLE, _fibo_audio_api, _audio_api_module

    try:
        sys.path.insert(0, '/usr/local/lib/python3.8/dist-packages')
        from fiboaisdk import api_aisdk_py
        _audio_api_module = api_aisdk_py.api_audio_py

        # Init license first
        if not _init_license():
            return False

        # Check model files
        if not os.path.isdir(FIBO_MODEL_PATH):
            print("[TTS] Model path not found: {}".format(FIBO_MODEL_PATH))
            return False

        fmodel_files = [f for f in os.listdir(FIBO_MODEL_PATH) if f.endswith('.fmodel')]
        if not fmodel_files:
            print("[TTS] No .fmodel file found in: {}".format(FIBO_MODEL_PATH))
            return False

        print("[TTS] Found model: {}".format(fmodel_files[0]))

        # Model loading is extremely noisy at the native fd level.
        with _sdk_call_lock:
            with _silence_sdk():
                api = _audio_api_module.AudioAPI()
                ret = api.Init(FIBO_MODEL_PATH)
        if ret != 0:
            print("[TTS] AudioAPI Init failed: {}".format(ret))
            api.Release()
            return False

        _fibo_audio_api = api
        FIBO_SDK_AVAILABLE = True
        _set_status(state="ready", sdk_ready=True, last_error="")
        print("[TTS] Fibocom SDK TTS READY")
        return True

    except Exception as e:
        print("[TTS] Fibocom SDK init error: {}".format(e))
        _set_status(state="fallback", sdk_ready=False, last_error=str(e))
        return False


# Try to initialize Fibocom TTS at import time, unless Piper is the selected primary backend.
if PIPER_LOCAL_ENABLED:
    print("[TTS] Local {} backend selected; Fibocom SDK will be lazy-loaded only as fallback".format(TTS_ENGINE_NAME or "tts"))
    _set_status(state="ready", sdk_ready=False, last_error="")
else:
    _init_fibo_tts()


# ==== espeak-ng fallback ====
ESPEAK_LIB = None

def _init_espeak():
    global ESPEAK_LIB
    if ESPEAK_LIB is not None:
        return ESPEAK_LIB
    try:
        lib = ctypes.CDLL('libespeak-ng.so.1')
        data_path = b'/usr/lib/aarch64-linux-gnu/espeak-ng-data'
        lib.espeak_ng_InitializePath(data_path)
        lib.espeak_ng_InitializeOutput(1, 0, None)
        lib.espeak_SetVoiceByName(b'cmn')
        ESPEAK_LIB = lib
        print("[TTS] espeak-ng fallback ready")
        return lib
    except Exception as e:
        print("[TTS] espeak-ng init failed: {}".format(e))
        return None


_audio_buffer = bytearray()
_callback_lock = threading.Lock()
_hph_playback_lock = threading.Lock()

# SC171 V3 / lahaina-yupikiot-snd-card analog headphone output.
# Card 0 and card 1 on this board are USB capture devices; the Qualcomm
# MultiMedia1 playback PCM is card 2, device 0.
HPH_ALSA_CARD = int(os.environ.get("FIBO_TTS_HPH_CARD", "2"))
HPH_ALSA_DEVICE = int(os.environ.get("FIBO_TTS_HPH_DEVICE", "0"))
HPH_STREAM_VOLUME = os.environ.get("FIBO_TTS_HPH_STREAM_VOLUME", "8192")

_HPH_ROUTE_ON = (
    (90, "AIF1_PB"),
    (91, "AIF1_PB"),
    (6639, "Two"),
    (112, "RX0"),
    (115, "RX1"),
    (107, "CLSH_DSM_OUT"),
    (108, "CLSH_DSM_OUT"),
    (137, "on"),
    (138, "on"),
    (244, "on"),
    (245, "on"),
    (269, "on"),
    (270, "on"),
    # Without an explicit class-H mode the codec remains in
    # CLS_H_INVALID: ALSA accepts every frame but the physical HPH pins stay
    # silent.  This was the reason aplay reported success with no sound.
    (243, "CLS_H_HIFI"),
    (520, "on"),
)

_HPH_ROUTE_OFF = (
    (520, "off"),
    (243, "CLS_H_INVALID"),
    (270, "off"),
    (269, "off"),
    (245, "off"),
    (244, "off"),
    (138, "off"),
    (137, "off"),
    (108, "NORMAL_DSM_OUT"),
    (107, "NORMAL_DSM_OUT"),
    (115, "ZERO"),
    (112, "ZERO"),
    (6639, "One"),
    (91, "ZERO"),
    (90, "ZERO"),
)


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
def _espeak_callback(wav, numsamples, userdata):
    global _audio_buffer
    if numsamples > 0:
        with _callback_lock:
            samples_array = (ctypes.c_short * numsamples).from_address(
                ctypes.addressof(wav.contents)
            )
            _audio_buffer.extend(bytes(samples_array))
    return 0


def _espeak_synthesize_to_wav(text, output_path):
    """Synthesize text to WAV using espeak-ng"""
    global _audio_buffer
    lib = _init_espeak()
    if lib is None:
        return False

    lib.espeak_SetSynthCallback(_espeak_callback)
    with _callback_lock:
        _audio_buffer = bytearray()

    text_bytes = text.encode('utf-8')
    uid = ctypes.c_uint(0)
    result = lib.espeak_ng_Synthesize(text_bytes, len(text_bytes), 0, 0, 0, 0, ctypes.byref(uid), None)
    if result != 0:
        print("[TTS] espeak-ng Synthesize failed: {}".format(result))
        lib.espeak_SetSynthCallback(None)
        return False

    sample_rate = lib.espeak_ng_GetSampleRate()
    with _callback_lock:
        buf = bytes(_audio_buffer)

    if len(buf) < 44:
        print("[TTS] espeak-ng insufficient audio data ({} bytes)".format(len(buf)))
        lib.espeak_SetSynthCallback(None)
        return False

    num_samples = len(buf) // 2
    fmt = '{}h'.format(num_samples)
    with wave.open(output_path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(fmt, *struct.unpack(fmt, buf)))

    lib.espeak_SetSynthCallback(None)
    return True


_hph_always_on = False
_hph_route_lock = threading.Lock()


def _set_hph_route(enabled):
    """Open or close the SC171 V3 analog HPH mixer route."""
    controls = _HPH_ROUTE_ON if enabled else _HPH_ROUTE_OFF
    for numid, value in controls:
        subprocess.run(
            ["amixer", "-q", "-c", str(HPH_ALSA_CARD), "cset",
             "numid={}".format(numid), str(value)],
            timeout=3, check=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT)

    subprocess.run(
        ["amixer", "-q", "-c", str(HPH_ALSA_CARD), "cset", "numid=5499",
         HPH_STREAM_VOLUME if enabled else "0"],
        timeout=3, check=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT)


def hph_always_on_init():
    """Open HPH route once at startup. Subsequent _play_wav calls skip route toggling.

    Call this from the startup supervisor so every TTS backend shares the open route.
    Route is kept open until process exit or an explicit hph_always_on_close() call.
    """
    global _hph_always_on
    with _hph_route_lock:
        if _hph_always_on:
            return
        try:
            _set_hph_route(True)
            _hph_always_on = True
            print("[TTS] HPH route always-on enabled", flush=True)
        except Exception as e:
            print("[TTS] HPH always-on init failed: {}".format(e), flush=True)


def hph_always_on_close():
    """Close HPH route at shutdown."""
    global _hph_always_on
    with _hph_route_lock:
        if not _hph_always_on:
            return
        try:
            _set_hph_route(False)
            _hph_always_on = False
            print("[TTS] HPH route closed", flush=True)
        except Exception as e:
            print("[TTS] HPH close warning: {}".format(e), flush=True)


def _wav_duration_seconds(wav_path):
    with wave.open(wav_path, "rb") as wav_file:
        rate = wav_file.getframerate()
        return float(wav_file.getnframes()) / float(rate or 1)


def _play_wav(wav_path):
    """Play WAV through the SC171 V3 HPH jack + save browser fallback."""
    if _tts_muted.is_set():
        return False
    # Always save for browser auto-play
    try:
        import shutil
        shutil.copy(wav_path, '/tmp/nuanyu_tts_output.wav')
    except Exception: pass

    # PC speaker fallback: ship the WAV to the host for playback when the
    # board's audio jack is dead.
    _pc_mode = os.environ.get('SURGE_PC_SPEAKER', '0')
    if _pc_mode == '1':
        print('[TTS] PC speaker mode, sending to PC...', flush=True)
        try:
            import urllib.request as _ur
            with open(wav_path, 'rb') as f:
                _ur.urlopen(_ur.Request(
                    os.environ.get('SURGE_PC_SPEAKER_URL', 'http://127.0.0.1:5015/play'),
                    data=f.read(),
                    headers={'Content-Type': 'audio/wav'}
                ), timeout=10)
            return True
        except Exception as e:
            print('[TTS] PC speaker failed: %s' % str(e), flush=True)
            return False

    play_path = _convert_wav_for_hal(wav_path)
    if not play_path:
        print("[TTS] HPH playback failed: unsupported WAV format")
        return False

    try:
        duration = _wav_duration_seconds(play_path)
        device = "hw:{},{}".format(HPH_ALSA_CARD, HPH_ALSA_DEVICE)
        # This Qualcomm PCM may not return from drain after all frames have
        # played. Stop it shortly after the expected WAV duration instead of
        # delaying the TTS queue indefinitely. Cap the timeout: a corrupt WAV
        # header can yield a huge duration, which would defeat the drain guard
        # and hang the playback thread (heard as a reply that stops halfway).
        playback_timeout = min(max(2.5, duration + 1.0), 10.0)

        # The playback lock must have a timeout: if another aplay is stuck in
        # drain and never releases it, skip this segment rather than wait
        # forever — otherwise the playback thread wedges permanently and every
        # later sentence is lost.
        if not _hph_playback_lock.acquire(timeout=5.0):
            print("[TTS] HPH lock busy, skipping playback ({:.2f}s)".format(duration))
            return False
        try:
            route_open = False
            try:
                if not _hph_always_on:
                    _set_hph_route(True)
                    route_open = True
                subprocess.run(
                    ["aplay", "-q", "-D", device, "--period-size=512",
                     "--buffer-size=2048", play_path],
                    timeout=playback_timeout, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                print("[TTS] Playback OK (HPH {}, {:.2f}s)".format(device, duration))
                return True
            except subprocess.TimeoutExpired:
                # Audio has completed; only the platform drain is stuck.
                print("[TTS] Playback OK (HPH {}, {:.2f}s; drain timeout)".format(
                    device, duration))
                return True
            finally:
                if route_open and not _hph_always_on:
                    try:
                        _set_hph_route(False)
                    except Exception as route_error:
                        print("[TTS] HPH route cleanup warning: {}".format(route_error))
        finally:
            _hph_playback_lock.release()
    except Exception as e:
        print("[TTS] HPH playback failed: {}; browser fallback remains available".format(e))
        return False
    finally:
        if play_path != wav_path:
            try:
                os.remove(play_path)
            except OSError:
                pass


def play_music_wav(wav_path):
    """Play a long audio file (music) through the board HPH jack.

    Unlike _play_wav for TTS segments there is no short 10s drain cap: a music
    track must play to completion.  The HPH playback lock is held for the whole
    duration so no TTS segment can interleave.  Blocks until done (or error).
    """
    if not os.path.exists(wav_path):
        print("[TTS] music file not found: %s" % wav_path, flush=True)
        return False
    play_path = _convert_wav_for_hal(wav_path)
    if not play_path:
        print("[TTS] music: unsupported WAV format")
        return False
    try:
        duration = _wav_duration_seconds(play_path)
        device = "hw:{},{}".format(HPH_ALSA_CARD, HPH_ALSA_DEVICE)
        # Music is long: cap generously (30 min) so a corrupt huge-duration
        # header cannot hang the playback thread forever.
        playback_timeout = min(max(duration + 2.0, 5.0), 1800.0)
        if not _hph_playback_lock.acquire(timeout=5.0):
            print("[TTS] music: HPH lock busy, skipping playback ({:.2f}s)"
                  .format(duration))
            return False
        try:
            route_open = False
            try:
                if not _hph_always_on:
                    _set_hph_route(True)
                    route_open = True
                subprocess.run(
                    ["aplay", "-q", "-D", device, "--period-size=512",
                     "--buffer-size=2048", play_path],
                    timeout=playback_timeout, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                print("[TTS] Music playback OK (HPH {}, {:.2f}s)"
                      .format(device, duration))
                return True
            except subprocess.TimeoutExpired:
                # Audio has completed; only the platform drain is stuck.
                print("[TTS] Music playback OK (HPH {}, {:.2f}s; drain timeout)"
                      .format(device, duration))
                return True
            finally:
                if route_open and not _hph_always_on:
                    try:
                        _set_hph_route(False)
                    except Exception as route_error:
                        print("[TTS] music route cleanup warning: {}"
                              .format(route_error))
        finally:
            _hph_playback_lock.release()
    except Exception as e:
        print("[TTS] Music playback failed: {}".format(e))
        return False
    finally:
        if play_path != wav_path:
            try:
                os.remove(play_path)
            except OSError:
                pass


def _convert_wav_for_hal(src_path):
    """Convert SDK WAV to 48 kHz stereo S16_LE for Qualcomm HAL playback."""
    try:
        with open(src_path, 'rb') as f:
            data = f.read()
        if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            return None

        pos = 12
        fmt_data = None
        audio_data = None
        while pos < len(data) - 8:
            chunk_id = data[pos:pos+4]
            chunk_size = struct.unpack_from('<I', data, pos+4)[0]
            chunk = data[pos+8:pos+8+chunk_size]
            if chunk_id == b'fmt ':
                fmt_data = chunk
            elif chunk_id == b'data':
                audio_data = chunk
            pos += 8 + chunk_size + (chunk_size & 1)

        if not fmt_data or audio_data is None:
            return None

        fmt_tag, channels, sample_rate = struct.unpack_from('<HHI', fmt_data, 0)
        bits = struct.unpack_from('<H', fmt_data, 14)[0]

        samples = []
        if fmt_tag == 3 and bits == 32:
            count = len(audio_data) // 4
            floats = struct.unpack('<{}f'.format(count), audio_data[:count * 4])
            samples = [max(-32768, min(32767, int(v * 32767))) for v in floats]
        elif fmt_tag == 1 and bits == 16:
            count = len(audio_data) // 2
            samples = list(struct.unpack('<{}h'.format(count), audio_data[:count * 2]))
        else:
            return None

        if channels > 1:
            mono = []
            for i in range(0, len(samples), channels):
                frame = samples[i:i+channels]
                if frame:
                    mono.append(int(sum(frame) / len(frame)))
        else:
            mono = samples

        target_rate = 48000
        ratio = float(target_rate) / float(sample_rate or 16000)
        out = []
        if ratio >= 1:
            repeat = max(1, int(round(ratio)))
            for s in mono:
                for _ in range(repeat):
                    out.extend((s, s))
        else:
            step = max(1, int(round(1.0 / ratio)))
            for s in mono[::step]:
                out.extend((s, s))

        dst_path = src_path + '.hal.wav'
        with wave.open(dst_path, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(target_rate)
            wf.writeframes(struct.pack('<{}h'.format(len(out)), *out))
        return dst_path
    except Exception as e:
        print("[TTS] HAL WAV conversion failed: {}".format(e))
        return None


def _send_to_pc(wav_path):
    """Send WAV file to PC playback server via adb reverse tunnel"""
    try:
        import urllib.request
        with open(wav_path, 'rb') as f:
            wav_data = f.read()
        req = urllib.request.Request(
            PC_PLAY_URL, data=wav_data,
            headers={"Content-Type": "audio/wav"}
        )
        urllib.request.urlopen(req, timeout=30)
        print("[TTS] PC playback OK (sent {} bytes)".format(len(wav_data)))
        return True
    except Exception as e:
        print("[TTS] PC playback failed: {}".format(e))
        return False


def _try_piper_local(text):
    """Try local Piper ONNX TTS. Returns True/False, or None when disabled."""
    global _piper_engine
    if not PIPER_LOCAL_ENABLED:
        return None
    try:
        if not PIPER_INPROC:
            import urllib.request as _urlreq
            payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
            req = _urlreq.Request(PIPER_SERVER_URL, data=payload, headers={"Content-Type": "application/json"})
            resp = _urlreq.urlopen(req, timeout=120)
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "piper server failed"))
            meta = result.get("meta", {})
        else:
            with _piper_lock:
                if _piper_engine is None:
                    from piper_local import get_engine
                    _piper_engine = get_engine()
            meta = _piper_engine.speak(text)
        print("[TTS] Local TTS OK meta={} infer={:.3f}s play={:.3f}s".format(
            str(meta.get("reference_audio", meta.get("phonemes", "")))[:80],
            float(meta.get("infer_sec", 0.0)),
            float(meta.get("play_sec", 0.0)),
        ))
        _set_status(state="ready", last_ok=True, last_finished=time.time(), last_error="")
        return True
    except Exception as e:
        print("[TTS] Local TTS failed: {}".format(e))
        _set_status(state="piper_failed", last_ok=False, last_error=str(e))
        return False

def fibo_tts_speak(text, lang="zh"):
    """
    Board-local TTS playback.
    Uses Fibocom SDK SpeechSynthesisSync (with FiboAudio object),
    falls back to espeak-ng.
    """
    if _tts_muted.is_set():
        _set_status(state="muted", last_ok=False)
        return False
    if not text or len(text.strip()) < 2:
        return False

    ts = int(time.time() * 1000)
    wav_path = os.path.join(TTS_OUTPUT_DIR, "tts_{}.wav".format(ts))
    _set_status(state="speaking", last_text=text[:80], last_started=time.time(), last_error="")

    piper_ok = _try_piper_local(text)
    if piper_ok is True:
        return True
    if piper_ok is False and TTS_ENGINE_NAME == "zipvoice":
        _set_status(state="failed", last_ok=False, last_finished=time.time(), last_error="ZipVoice local service failed")
        return False
    if piper_ok is False and not FIBO_SDK_AVAILABLE:
        _init_fibo_tts()

    if FIBO_SDK_AVAILABLE and _fibo_audio_api is not None:
        try:
            synth = fibo_tts_synthesize_to_wav(text, wav_path, lang=lang)
            status = synth.get("status")
            if synth.get("success"):
                print("[TTS] Fibocom TTS OK ({} bytes)".format(os.path.getsize(wav_path)))
                if _tts_muted.is_set():
                    _cleanup(wav_path)
                    _set_status(state="muted", last_ok=False)
                    return False
                ok = _play_wav(wav_path)
                _cleanup(wav_path)
                _set_status(state="ready" if ok else "playback_failed", last_ok=ok,
                            last_finished=time.time(), last_error="" if ok else "aplay failed")
                return ok
            else:
                print("[TTS] Fibocom TTS failed: {}".format(synth.get("error")))
                _set_status(state="synthesis_failed", last_ok=False,
                            last_error=synth.get("error", "Fibocom synthesis failed"))
        except Exception as e:
            print("[TTS] Fibocom SDK TTS error: {}".format(e))
            _set_status(state="synthesis_failed", last_ok=False, last_error=str(e))

    # Fallback: espeak-ng
    if FIBO_SDK_AVAILABLE:
        print("[TTS] Falling back to espeak-ng")
    if _espeak_synthesize_to_wav(text, wav_path):
        if _tts_muted.is_set():
            _cleanup(wav_path)
            _set_status(state="muted", last_ok=False)
            return False
        ok = _play_wav(wav_path)
        _cleanup(wav_path)
        _set_status(state="ready" if ok else "playback_failed", last_ok=ok,
                    last_finished=time.time(), last_error="" if ok else "fallback aplay failed")
        return ok

    _cleanup(wav_path)
    _set_status(state="failed", last_ok=False, last_finished=time.time(),
                last_error="no synthesis backend produced audio")
    return False


def _cleanup(path):
    try:
        os.remove(path)
    except:
        pass


_tts_queue = queue.Queue(maxsize=8)
_worker_started = False
_worker_lock = threading.Lock()


def _tts_worker():
    while True:
        text, lang, callback = _tts_queue.get()
        ok = False
        err = ""
        try:
            ok = fibo_tts_speak(text, lang)
        except Exception as e:
            err = str(e)
            _set_status(state="failed", last_ok=False, last_error=err, last_finished=time.time())
            print("[TTS] worker error: {}".format(e))
        finally:
            if callback:
                try:
                    callback(ok, err)
                except Exception:
                    pass
            _tts_queue.task_done()
            _set_status(queue_size=_tts_queue.qsize())


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_tts_worker, daemon=True).start()
            _worker_started = True


def fibo_tts_speak_async(text, lang="zh", callback=None):
    """Queue TTS for serialized non-blocking playback."""
    if _tts_muted.is_set():
        _set_status(state="muted", last_ok=False, queue_size=0)
        return False
    if not text or len(str(text).strip()) < 2:
        return False
    _ensure_worker()
    try:
        if _tts_queue.full():
            try:
                _tts_queue.get_nowait()
                _tts_queue.task_done()
            except Exception:
                pass
        _tts_queue.put_nowait((str(text), lang, callback))
        _set_status(state="queued", last_text=str(text)[:80], queue_size=_tts_queue.qsize())
        return True
    except Exception as e:
        _set_status(state="queue_failed", last_ok=False, last_error=str(e))
        return False


# ==== Test ====
if __name__ == '__main__':
    print("=" * 50)
    print("  Fibocom Board Local TTS Module Test")
    print("  Fibocom SDK: {}".format('READY' if FIBO_SDK_AVAILABLE else 'NOT READY (espeak-ng fallback)'))
    print("=" * 50)
    fibo_tts_speak("你好，欢迎使用广和通TTS语音合成系统。")
