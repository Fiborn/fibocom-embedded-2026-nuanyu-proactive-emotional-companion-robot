#!/usr/bin/env python3
"""Small persistent HTTP client for DeepSeek's OpenAI-compatible API.

v2: Auto-retry on connection errors (4G network resilience).
"""
import http.client
import json
import threading
import time
from urllib.parse import urlparse


class DeepSeekHTTPClient:
    """Reuse DNS/TCP/TLS state while exposing streamed SSE lines.

    Automatically retries once on RemoteDisconnected / connection errors,
    which are common on 4G connections where the NAT mapping can expire.
    """

    def __init__(self, base_url, ssl_context, timeout=60, max_retries=1):
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid DeepSeek base URL")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._prefix = parsed.path.rstrip("/")
        self._ssl_context = ssl_context
        self._timeout = timeout
        self._max_retries = max_retries
        self._connection = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        # keep-alive idle threshold: the server normally closes idle connections
        # at ~60s, so an older connection is guaranteed RemoteDisconnected on
        # the next request (dead-conn retry -> slow first token + stream break).
        self._idle_reset_s = 30.0

    def warmup(self):
        """Open DNS/TCP/TLS without spending an API request."""
        with self._lock:
            try:
                connection = self._get_connection()
                connection.connect()
                return True
            except Exception:
                self._reset()
                return False

    def stream_chat(self, payload, api_key):
        """Yield SSE lines. Auto-retries once on connection errors."""
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "Accept": "text/event-stream",
            "Connection": "keep-alive",
        }

        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                yield from self._stream_chat_once(body, headers)
                return  # success
            except (http.client.RemoteDisconnected,
                    ConnectionResetError, BrokenPipeError,
                    OSError, TimeoutError) as e:
                last_error = e
                if attempt < self._max_retries:
                    print("[DEEPSEEK] connection lost (attempt %d/%d), retrying..."
                          % (attempt + 1, self._max_retries + 1), flush=True)
                    time.sleep(0.5)
                    with self._lock:
                        self._reset()
                else:
                    with self._lock:
                        self._reset()
                    raise
            except Exception:
                with self._lock:
                    self._reset()
                raise

        # Should not reach here, but just in case
        if last_error:
            raise last_error

    def _stream_chat_once(self, body, headers):
        """Single-attempt streaming request. Must hold self._lock."""
        with self._lock:
            connection = self._get_connection()
            connection.request("POST", self._prefix + "/v1/chat/completions",
                               body=body, headers=headers)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                # Drain a bounded prefix of the error body so the pooled
                # connection stays reusable, but do not surface it in the
                # exception: it may contain auth headers.
                response.read(1024).decode("utf-8", errors="ignore")
                raise RuntimeError("DeepSeek HTTP %d" % response.status)
            while True:
                line = response.readline()
                if not line:
                    break
                yield line.decode("utf-8", errors="ignore").strip()
            if response.getheader("Connection", "").lower() == "close":
                self._reset()

    # ── Non-streaming API (for function-calling loops) ──────────

    def chat_completion(self, payload, api_key):
        """Send a **non-streaming** chat-completion request and return
        the parsed JSON response object.

        Auto-retries on connection errors (same resilience as stream_chat).
        """
        payload = dict(payload)
        payload["stream"] = False
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "Connection": "keep-alive",
        }

        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                with self._lock:
                    connection = self._get_connection()
                    connection.request(
                        "POST", self._prefix + "/v1/chat/completions",
                        body=body, headers=headers)
                    response = connection.getresponse()
                    if response.status < 200 or response.status >= 300:
                        detail = response.read(1024).decode(
                            "utf-8", errors="ignore")
                        raise RuntimeError(
                            "DeepSeek HTTP %s: %s" %
                            (response.status, detail[:240]))
                    raw = response.read()
                    if response.getheader("Connection", "").lower() == "close":
                        self._reset()
                    return json.loads(raw.decode("utf-8", errors="ignore"))
            except (http.client.RemoteDisconnected,
                    ConnectionResetError, BrokenPipeError,
                    OSError, TimeoutError) as e:
                last_error = e
                if attempt < self._max_retries:
                    print("[DEEPSEEK] connection lost (attempt %d/%d), retrying..."
                          % (attempt + 1, self._max_retries + 1), flush=True)
                    time.sleep(0.5)
                    with self._lock:
                        self._reset()
                else:
                    with self._lock:
                        self._reset()
                    raise
            except Exception:
                with self._lock:
                    self._reset()
                raise

        if last_error:
            raise last_error
        return None

    def close(self):
        with self._lock:
            self._reset()

    def _get_connection(self):
        now = time.time()
        if self._connection is not None and now - self._last_used > self._idle_reset_s:
            # keep-alive idle timeout: server closed it; rebuild to avoid dead-conn retry
            self._reset()
        if self._connection is None:
            if self._scheme == "https":
                self._connection = http.client.HTTPSConnection(
                    self._host, self._port, timeout=self._timeout,
                    context=self._ssl_context)
            else:
                self._connection = http.client.HTTPConnection(
                    self._host, self._port, timeout=self._timeout)
        self._last_used = now
        return self._connection

    def _reset(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
        self._connection = None
