#!/usr/bin/env python3
"""L610 4G Module -> Huawei Cloud IoTDA via HM (Huawei MQTT) AT commands.

Phase 3: HM command system per "Huawei Cloud and L610 Debugging Guide
V1.0.0 (2025-03-05)".

Connection:  AT+HMCON
Receive URC: +HMREC (primary), +MQTTMSG (fallback)
Publish:     AT+HMPUB

Port auto-detection (2026-07-25):
  The L610 exposes a USB composite device with 7 virtual serial ports
  (ttyUSB0–ttyUSB6).  Only ttyUSB5 carries AT commands.  When the
  module is connected via USB → SC171 the option driver creates
  /dev/ttyUSB*.  If USB is unavailable the physical UART fallback
  (/dev/ttyHS6) is tried last.

Standalone mode:
    python3 src/connectivity/l610_service.py
  Sources L610_DEVICE_SECRET from <NUANYU_ROOT>/config/l610.env.
"""

import os, sys, time, json, threading, select, termios, logging, urllib.request

# === Config ===
# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")

UART_PORT       = None   # detected at runtime

# Port auto-detection candidates, tried in order: the L610 USB virtual
# serial ports (ttyUSB*) are probed first, the physical UART (/dev/ttyHS6)
# is the last-resort fallback.
_UART_CANDIDATES = [
    "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyUSB3",
    "/dev/ttyUSB4", "/dev/ttyUSB5", "/dev/ttyUSB6",
    "/dev/ttyHS6",
]
UART_BAUD       = 115200
# IoTDA endpoint identifiers are deployment-specific and come from the
# environment.  An empty value means "not configured"; start() refuses to run.
BROKER_HOST     = os.environ.get("L610_BROKER_HOST", "")
BROKER_PORT     = os.environ.get("L610_BROKER_PORT", "1883")
DEVICE_ID       = os.environ.get("L610_DEVICE_ID", "")
KEEPALIVE_SEC   = 60
REPORT_INTERVAL = 60
RECONNECT_DELAY = 10

WEB_API_BASE    = "http://127.0.0.1:5004"
HTTP_TIMEOUT    = 45

LOG = logging.getLogger("l610")


# ═══════════════════════════════════════════════════════════════════
#  Port auto-detection
# ═══════════════════════════════════════════════════════════════════

def _probe_at_port(port_path, baud=115200):
    """Send AT to *port_path*, return True if OK received within 1.5 s."""
    try:
        fd = os.open(port_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        a = termios.tcgetattr(fd)
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = termios.B115200; a[5] = termios.B115200
        a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 5
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIOFLUSH)

        os.write(fd, b"AT\r\n")
        buf = bytearray()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                buf.extend(os.read(fd, 256))
            if b"OK" in buf:
                return True
        return False
    except Exception:
        return False
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def detect_l610_port(candidates=None):
    """Return the first AT-responsive port path, or None."""
    ports = candidates or _UART_CANDIDATES
    for port in ports:
        if not os.path.exists(port):
            continue
        if _probe_at_port(port):
            return port
    return None


# ═══════════════════════════════════════════════════════════════════
#  UART helpers
# ═══════════════════════════════════════════════════════════════════

def _open_uart():
    if UART_PORT is None:
        raise RuntimeError("L610 AT port not detected — is the module plugged in?")
    fd = os.open(UART_PORT, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = 0; a[1] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0
    a[4] = termios.B115200; a[5] = termios.B115200
    a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def _has_valid_ip(text):
    """Check if a +MIPCALL response contains a valid IP (not 0.0.0.0).
    Handles both formats: '+MIPCALL: 1,<IP>' and '+MIPCALL: <IP>'.
    """
    import re as _re
    m = _re.search(r'\+MIPCALL:\s*(?:1,\s*)?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', text)
    if not m:
        return False
    ip = m.group(1)
    return ip != "0.0.0.0" and not ip.startswith("0.0.0.")


def _read_to(fd, markers, timeout_s=2.0):
    """Blocking read until one of `markers` found, or timeout."""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.3)
        if fd in r:
            buf.extend(os.read(fd, 4096))
        text = bytes(buf).decode("ascii", errors="replace")
        for m in markers:
            if m in text:
                return text
    return bytes(buf).decode("ascii", errors="replace")


# ═══════════════════════════════════════════════════════════════════
#  HM AT commands
# ═══════════════════════════════════════════════════════════════════

def _escape_json_for_at(json_str):
    """Escape for embedding in AT+HMPUB quoted argument.
    \\ -> \\\\,  \" -> \\\\\"
    """
    return json_str.replace("\\", "\\\\").replace('"', '\\"')


def _hm_con(fd, secret):
    """AT+HMCON=0,keepalive,\"host\",port,\"device_id\",\"secret\",0"""
    cmd = (
        f'AT+HMCON=0,{KEEPALIVE_SEC},"{BROKER_HOST}","1883",'
        f'"{DEVICE_ID}","{secret}",0\r\n'
    )
    LOG.info("HMCON connecting...")
    # Send HMDIS to close stale connection, drain its response
    os.write(fd, b"AT+HMDIS\r\n")
    time.sleep(0.3)
    _read_to(fd, ["OK", "ERROR"], timeout_s=2.0)  # drain HMDIS response
    # Now send HMCON with a clean buffer
    os.write(fd, cmd.encode())
    resp = _read_to(fd, ["+HMCON:", "+HMCON OK", "ERROR"], timeout_s=40.0)
    ok = "+HMCON:0,1" in resp or "+HMCON OK" in resp
    LOG.info("HMCON %s: %s", "OK" if ok else "FAIL", resp.strip()[:300])
    return ok, resp


def _hm_pub(fd, topic, payload_str):
    """AT+HMPUB=1,\"topic\",utf8_byte_len,\"escaped_payload\"
    Returns (ok: bool, response_text: str)."""
    payload_bytes = payload_str.encode("utf-8")
    plen = len(payload_bytes)
    escaped = _escape_json_for_at(payload_str)
    cmd = f'AT+HMPUB=1,"{topic}",{plen},"{escaped}"\r\n'
    os.write(fd, cmd.encode())
    resp = _read_to(fd, ["+HMPUB", "ERROR"], timeout_s=5.0)
    ok = "+HMPUB OK" in resp or "+HMPUB:0" in resp
    return ok, resp


# ═══════════════════════════════════════════════════════════════════
#  URC parsing  (supports +HMREC primary, +MQTTMSG fallback)
# ═══════════════════════════════════════════════════════════════════

def _parse_urc(text):
    """Extract (topic, payload) from +HMREC or +MQTTMSG URC line.
    Both formats: ... \"topic_string\",\"quoted_json_payload\"
    """
    text = text.strip()
    if not text:
        return None, None
    q1 = text.find('"')
    if q1 == -1:
        return None, None
    q2 = text.find('"', q1 + 1)
    if q2 == -1:
        return None, None
    topic = text[q1 + 1:q2]
    after = text[q2 + 1:].strip()
    if after.startswith(","):
        after = after[1:].strip()

    # Quoted payload  "...\"...json...\""
    if after.startswith('"'):
        ps = 1
        pe = after.rfind('"')
        if pe > ps:
            payload = after[ps:pe]
            payload = payload.replace('\\\\"', '"').replace('\\"', '"')
            return topic, payload
        return None, None

    # Fallback: bare JSON { ... }
    bs = after.find("{")
    if bs == -1:
        return None, None
    depth = 0
    for i in range(bs, len(after)):
        if after[i] == "{":
            depth += 1
        elif after[i] == "}":
            depth -= 1
            if depth == 0:
                return topic, after[bs:i + 1]
    return None, None


# ═══════════════════════════════════════════════════════════════════
#  L610Service  (HM command system)
# ═══════════════════════════════════════════════════════════════════

class L610Service:

    def __init__(self, device_secret=None):
        self.secret = device_secret or os.environ.get("L610_DEVICE_SECRET", "")
        self._fd = None
        self._connected = False
        self._running = False
        self._thread = None
        self._urc_buffer = bytearray()
        self._uart_lock = threading.Lock()

    @property
    def connected(self):
        return self._connected

    def start(self):
        global UART_PORT
        if not self.secret:
            LOG.error("L610_DEVICE_SECRET not set")
            return False
        if not BROKER_HOST or not DEVICE_ID:
            LOG.error("L610_BROKER_HOST / L610_DEVICE_ID not set — "
                      "IoTDA endpoint not configured")
            return False

        # Auto-detect AT port on first start
        if UART_PORT is None:
            LOG.info("Probing L610 AT port...")
            port = detect_l610_port()
            if port is None:
                LOG.error("L610 AT port not found — candidates: %s", _UART_CANDIDATES)
                return False
            UART_PORT = port
            LOG.info("L610 AT port detected: %s", UART_PORT)

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="L610")
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._fd:
            try:
                os.close(self._fd)
            except Exception:
                pass

    # ── Main loop ─────────────────────────────────────────────

    def _init_modem(self):
        """AT check, SIM, network, PDP — prerequisites for HMCON.
        Blocks until a valid IP is obtained via AT+MIPCALL.
        Logs raw serial data on timeout; never proceeds without an IP.
        """
        fd = self._fd
        for cmd, label, timeout in [
            ("AT", "modem check", 2),
            ("AT+CPIN?", "SIM check", 2),
            ("AT+CEREG?", "network reg", 3),
            ("AT+CSQ", "signal", 2),
        ]:
            os.write(fd, (cmd + "\r\n").encode())
            buf = bytearray(); dl = time.monotonic() + timeout
            while time.monotonic() < dl:
                r, _, _ = select.select([fd], [], [], 0.3)
                if fd in r: buf.extend(os.read(fd, 4096))
            LOG.info("%s: %s", label, bytes(buf).decode("ascii", "replace").strip()[:120])

        # ── PDP / IP acquisition ──
        LOG.info("Checking PDP...")
        os.write(fd, b"AT+MIPCALL?\r\n")
        resp = _read_to(fd, ["OK", "ERROR"], 2)
        LOG.info("MIPCALL?: %s", resp.strip()[:120])

        has_ip = _has_valid_ip(resp)
        if not has_ip:
            LOG.info("No IP, activating PDP (AT+MIPCALL=1)...")
            os.write(fd, b'AT+MIPCALL=1,"CMNET"\r\n')
            # Wait for +MIPCALL URC with a valid IP
            pdp_buf = bytearray()
            pdp_deadline = time.monotonic() + 15
            ip_ok = False
            while time.monotonic() < pdp_deadline:
                r, _, _ = select.select([fd], [], [], 1.0)
                if fd in r:
                    pdp_buf.extend(os.read(fd, 4096))
                raw = bytes(pdp_buf).decode("ascii", "replace")
                if _has_valid_ip(raw):
                    ip_ok = True
                    break
            raw_all = bytes(pdp_buf).decode("ascii", "replace")
            LOG.info("PDP activation raw: %s", raw_all.strip()[:200])
            if not ip_ok:
                raise RuntimeError(
                    "PDP activation failed — no valid IP after 15s. "
                    "Raw: %s" % raw_all.strip()[:200]
                )
            LOG.info("PDP active, IP obtained")

        LOG.info("Modem init done")

    def _run(self):
        while self._running:
            try:
                self._fd = _open_uart()
                LOG.info("UART open port=%s", UART_PORT)
                self._init_modem()
                ok, _ = _hm_con(self._fd, self.secret)
                if not ok:
                    raise RuntimeError("HMCON failed")
                self._connected = True
                LOG.info("*** CONNECTED to Huawei Cloud IoTDA via HM ***")
                self._main_loop()
            except Exception as e:
                LOG.error("Error: %s", e)
            finally:
                self._connected = False
                if self._fd:
                    try:
                        os.close(self._fd)
                    except Exception:
                        pass
                self._fd = None
            if self._running:
                LOG.info("Reconnect in %ds...", RECONNECT_DELAY)
                time.sleep(RECONNECT_DELAY)

    # ── Main reader loop (single thread reads all UART data) ──

    def _main_loop(self):
        last_report = 0
        while self._running and self._connected:
            r, _, _ = select.select([self._fd], [], [], 1.0)
            if self._fd in r:
                data = os.read(self._fd, 4096)
                if data:
                    self._urc_buffer.extend(data)
                    LOG.debug("UART rx %dB", len(data))
                    self._process_urc_buffer()
            now = time.monotonic()
            if now - last_report >= REPORT_INTERVAL:
                self._upload_status()
                last_report = now

    def _process_urc_buffer(self):
        """Extract and dispatch +HMREC or +MQTTMSG URCs from buffer."""
        text = bytes(self._urc_buffer).decode("ascii", errors="replace")

        for marker in ("+HMREC:", "+HMREC ", "+MQTTMSG:"):
            while marker in text:
                idx = text.index(marker)
                after = text[idx + len(marker):]
                # Find next marker
                next_idx = -1
                for m2 in ("+HMREC:", "+HMREC ", "+MQTTMSG:"):
                    pos = after.find(m2)
                    if pos != -1 and (next_idx == -1 or pos < next_idx):
                        next_idx = pos
                if next_idx == -1:
                    segment = after
                    text = ""
                else:
                    segment = after[:next_idx]
                    text = after[next_idx:]

                topic, payload = _parse_urc(segment)
                if topic and payload:
                    LOG.info("URC %s topic=%s", marker.strip(), topic[:150])
                    LOG.info("URC payload=%s", payload[:500])
                    threading.Thread(
                        target=self._handle_command,
                        args=(topic, payload),
                        daemon=True,
                    ).start()
                if next_idx == -1:
                    break

        self._urc_buffer = bytearray(text.encode("ascii", errors="replace"))

    # ── Command handler ───────────────────────────────────────

    def _handle_command(self, topic, payload_str):
        request_id = ""
        try:
            if "request_id=" in topic:
                request_id = topic.split("request_id=")[-1].split(",")[0].split("&")[0]

            obj = json.loads(payload_str)
            paras = obj.get("paras", obj)
            method = paras.get("method", "GET")
            path   = paras.get("path", "/api/status")
            body_raw = paras.get("body_json") or paras.get("body")
            body = None
            if body_raw is not None:
                if isinstance(body_raw, str):
                    try:
                        body = json.loads(body_raw)
                    except json.JSONDecodeError:
                        body = {"text": body_raw}
                elif isinstance(body_raw, dict):
                    body = body_raw

            LOG.info("CMD %s %s platform_id=%s", method, path, request_id)

            result = self._http_call(method, path, body)
            web_status = result.get("_http_status", 200) if isinstance(result, dict) else 200

            # Summarize large responses to fit HMPUB 281-byte limit
            if isinstance(result, dict) and path == "/api/status":
                study_sec = result.get("study_seconds", 0) or 0
                summary = {
                    "ai_ok": result.get("ai_ok"),
                    "study_running": result.get("study_running"),
                    "study_duration_sec": int(study_sec) if study_sec else 0,
                    "current_mood": result.get("current_mood", "一般"),
                    "sleeping": result.get("assistant_sleeping", False),
                    "present": result.get("visual_state") == "PRESENT",
                    "tts": result.get("tts", {}).get("state", "?") if isinstance(result.get("tts"), dict) else str(result.get("tts", "?")),
                }
                result = summary

            resp_payload = json.dumps({
                "result_code": 0,
                "response_name": "COMMAND_RESPONSE",
                "paras": {
                    "status_code": web_status,
                    "body_json": json.dumps(result, ensure_ascii=False),
                },
            }, ensure_ascii=False)

            resp_topic = (
                f"$oc/devices/{DEVICE_ID}/sys/commands/response/"
                f"request_id={request_id}"
            )

            LOG.info("HMPUB topic=%s", resp_topic)
            with self._uart_lock:
                ok, rsp = _hm_pub(self._fd, resp_topic, resp_payload)
            LOG.info("HMPUB %s: %s", "OK" if ok else "FAIL", rsp.strip()[:200])

        except Exception as e:
            LOG.error("CMD error: %s", e)
            if request_id:
                try:
                    err = json.dumps({
                        "result_code": 1,
                        "response_name": "COMMAND_RESPONSE",
                        "paras": {
                            "status_code": 502,
                            "body_json": json.dumps({"error": str(e)[:200]}, ensure_ascii=False),
                        },
                    }, ensure_ascii=False)
                    rt = f"$oc/devices/{DEVICE_ID}/sys/commands/response/request_id={request_id}"
                    with self._uart_lock:
                        ok, rs = _hm_pub(self._fd, rt, err)
                    LOG.info("HMPUB err %s: %s", "OK" if ok else "FAIL", rs.strip()[:200])
                except Exception:
                    pass

    def _http_call(self, method, path, body):
        url = WEB_API_BASE + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        raw = resp.read().decode("utf-8", errors="replace")
        LOG.info("HTTP %d %dB", resp.status, len(raw))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _upload_status(self):
        try:
            payload_str = json.dumps({
                "services": [{
                    "service_id": "device_status",
                    "properties": {"online": True},
                }]
            }, ensure_ascii=False)
            topic = f"$oc/devices/{DEVICE_ID}/sys/properties/report"
            with self._uart_lock:
                ok, rsp = _hm_pub(self._fd, topic, payload_str)
            if not ok:
                LOG.warning("Upload HMPUB fail: %s", rsp.strip()[:200])
        except Exception as e:
            LOG.error("Upload error: %s", e)

    def health(self):
        return {
            "uart": UART_PORT or "(not detected)",
            "connected": self._connected,
            "broker": f"{BROKER_HOST}:{BROKER_PORT}",
            "device_id": DEVICE_ID,
        }


# ═══════════════════════════════════════════════════════════════════
#  Standalone runner  (python3 l610_service.py)
# ═══════════════════════════════════════════════════════════════════

def _load_env_file(path):
    """Minimal .env parser — no shell, no variable expansion."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [L610] %(message)s",
        datefmt="%H:%M:%S",
    )
    LOG.info("L610 service starting...")

    _load_env_file(os.path.join(NUANYU_ROOT, "config", "l610.env"))

    svc = L610Service()
    if not svc.start():
        LOG.error("Failed to start L610 service — check L610_DEVICE_SECRET and hardware")
        sys.exit(1)

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        LOG.info("Shutting down...")
        svc.stop()
