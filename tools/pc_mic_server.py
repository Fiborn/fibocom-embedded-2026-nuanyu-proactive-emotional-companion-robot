#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC microphone bridge service - feeds the PC microphone to board ASR when the
board USB microphone is broken.

Usage:
  1) Connect the board and set up a reverse tunnel (usually established
     automatically by the Nuanyu EXE, but can be confirmed manually):
       adb reverse tcp:5002 tcp:5002
  2) Run this service on the PC:
       python pc_mic_server.py
  3) On the board set ASR_MIC_SOURCE=remote (or auto to fall back
     automatically) and restart nuanyu-runtime.

Endpoints:
  GET /mic     -> 16kHz / 16bit / mono raw PCM stream (chunked). The board
                  asr_worker reads it frame by frame (30ms/640B) and does its
                  own VAD, independently of this service.
  GET /health  -> {"ok":true, "device":..., "sample_rate":16000}

Dependency: sounddevice (pip install sounddevice). Captures the Windows default
microphone by default.
"""
import http.server
import json
import queue
import sys
import threading

# The Windows console defaults to GBK; microphone device names may contain
# characters that cannot be encoded -> force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = 5002
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK = 160  # 10ms per frame

try:
    import sounddevice as sd
except ImportError:
    sd = None

# Each /mic connection gets its own buffer; the callback broadcasts data to all
# active connections.
_REG = set()
_REG_LOCK = threading.Lock()
_stream = None
_device_name = ""


def _broadcast(data):
    with _REG_LOCK:
        for q in list(_REG):
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except Exception:
                    pass


def _capture(indata, frames, time_info, status):
    _broadcast(bytes(indata))


def start_capture():
    global _stream, _device_name
    if sd is None:
        return False
    try:
        info = sd.query_devices(sd.default.device[0])
        _device_name = str(info.get("name", "default"))
    except Exception:
        _device_name = "default"
    try:
        _stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=BLOCK, callback=_capture,
        )
        _stream.start()
    except Exception as e:
        print("ERROR: failed to start microphone capture: %s" % e, flush=True)
        return False
    return True


class MicServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The board disconnecting at the end of each listening cycle is normal
        # behavior; do not print traceback noise.
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({
                "ok": True, "device": _device_name,
                "sample_rate": SAMPLE_RATE,
                "capturing": _stream is not None,
            })
            return
        if self.path == "/mic":
            if _stream is None:
                self._send_json({"ok": False, "error": "no capture"}, 503)
                return
            q = queue.Queue(maxsize=800)  # ~8s buffer
            with _REG_LOCK:
                _REG.add(q)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    try:
                        data = q.get(timeout=1.0)
                    except queue.Empty:
                        continue  # write nothing when idle (a 0-size chunk means end of stream and would drop the connection)
                    self.wfile.write(b"%x\r\n" % len(data))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _REG_LOCK:
                    _REG.discard(q)
            return
        self._send_json({"ok": False, "error": "not found"}, 404)


if __name__ == "__main__":
    if sd is None:
        print("ERROR: sounddevice is not installed, please run: pip install sounddevice", flush=True)
        sys.exit(1)
    print("[PC-Mic] starting PC microphone capture ...", flush=True)
    if not start_capture():
        sys.exit(1)
    server = MicServer(("127.0.0.1", PORT), Handler)
    print("[PC-Mic] listening on 127.0.0.1:%d  mic=%s  rate=16kHz/16bit/mono"
          % (PORT, _device_name), flush=True)
    print("[PC-Mic] board needs adb reverse tcp:5002 tcp:5002 (usually established by the Nuanyu EXE)", flush=True)
    server.serve_forever()
