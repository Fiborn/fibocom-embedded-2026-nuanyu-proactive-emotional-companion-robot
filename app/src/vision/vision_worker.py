#!/usr/bin/env python3
"""VisionWorker — camera frame capture + face crop + FER + smoothing.
Runs in a background thread. Does NOT control robot behavior.
"""
import sys, os, time, threading, queue
import urllib.request
import numpy as np
import cv2

# Add parent dir for local imports
_cur = os.path.dirname(os.path.abspath(__file__))
if _cur not in sys.path:
    sys.path.insert(0, os.path.dirname(_cur))

from vision.fer_onnx_backend import FEROnnxBackend, LABELS, EMOTION_ZH
from vision.expression_smoother import ExpressionSmoother

CAMERA_DEVICE = "/dev/video2"
CAMERA_FALLBACK_URL = os.environ.get(
    "CAMERA_FALLBACK_URL",
    "http://127.0.0.1:5016/camera.jpg",
)
FER_EVERY_N_FRAMES = 4
FACE_DETECT_EVERY_N_FRAMES = 5
FACE_DETECT_WIDTH = 320
# Keep the face ROI short-lived so FER never runs on a stale crop for long;
# person-presence is tracked separately with hysteresis (see _update_presence).
FACE_BBOX_TTL_FRAMES = 5

# ── Person-presence hysteresis ──
# A fresh face detection marks the person present immediately.  Presence is
# kept alive while the face is seen OR there is movement (person turned away,
# typing, shifting), and only drops after a sustained absence of both.  This
# stops the old "slight head turn -> instantly AWAY" flip-flopping.
PRESENCE_HOLD_SECONDS = float(os.environ.get("VISION_PRESENCE_HOLD_S", "4.0"))
PRESENCE_EXIT_SECONDS = float(os.environ.get("VISION_PRESENCE_EXIT_S", "8.0"))
PRESENCE_MOTION_EVERY_N = 3        # cheap frame-diff cadence
PRESENCE_MOTION_WIDTH = 160        # downscale width for the diff
PRESENCE_MOTION_THRESHOLD = 250    # changed pixels that count as movement
FER_RELOAD_INTERVAL_SECONDS = 5.0  # retry FER model load if it failed at boot

class VisionWorker:
    def __init__(self, camera_device=CAMERA_DEVICE, fer_model_path=None):
        self.camera_device = camera_device
        self.fer_model_path = fer_model_path

        # Threading
        self._frame_queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._cap = None
        self._camera_source = "none"
        self._camera_error = ""

        # State
        self._latest_state = self._empty_state()
        self._frame_count = 0
        self._fer_count = 0
        self._last_fer_time = 0.0
        self._fer_latencies = []  # last 100 latencies for stats
        self._face_detect_latencies = []
        self._pipeline_latencies = []
        self._metrics_time = time.monotonic()
        self._metrics_frame = 0

        # Models (init once)
        self._fer = None
        self._smoother = ExpressionSmoother()
        self._face_detector = self._load_face_detector()
        self._last_face_bbox = None
        self._last_face_frame = -FACE_BBOX_TTL_FRAMES

        # Person-presence (sticky) vs raw face detection.
        self._presence = False
        self._presence_activity_at = 0.0
        self._motion_frame_counter = 0
        self._prev_small_gray = None
        self._motion_present_cache = False
        # FER model self-heal.
        self._last_fer_retry_at = 0.0

        # Stats
        self._fer_latency_avg = 0.0
        self._fer_latency_p90 = 0.0
        self._face_detect_latency_ms = 0.0
        self._pipeline_latency_ms = 0.0

    # ---- public API ----
    def start(self):
        if self._running:
            return
        self._running = True
        self._fer = FEROnnxBackend(self.fer_model_path)
        self._thread = threading.Thread(target=self._run, daemon=True, name="VisionWorker")
        self._thread.start()
        print("[VisionWorker] Started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._cap and self._cap.isOpened():
            self._cap.release()
        if self._fer:
            self._fer.close()
        print("[VisionWorker] Stopped")

    def submit_frame(self, frame):
        """Submit a BGR frame from the main camera loop. Non-blocking."""
        if not self._running:
            return
        try:
            # Discard old frame if queue full
            if self._frame_queue.full():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            pass

    def get_latest_state(self) -> dict:
        with self._lock:
            return dict(self._latest_state)

    def health(self) -> dict:
        return {
            "running": self._running,
            "fer_loaded": self._fer.loaded if self._fer else False,
            "fer_load_error": getattr(self._fer, "_load_error", None)
                              if self._fer else None,
            "fer_count": self._fer_count,
            "frame_count": self._frame_count,
            "presence": self._presence,
            "fer_latency_avg_ms": round(self._fer_latency_avg, 2),
            "fer_latency_p90_ms": round(self._fer_latency_p90, 2),
            "face_detect_latency_ms": round(self._face_detect_latency_ms, 2),
            "pipeline_latency_ms": round(self._pipeline_latency_ms, 2),
            "camera_source": self._camera_source,
            "camera_error": self._camera_error,
        }

    # ---- internal ----
    def _empty_state(self):
        return {
            "face_detected": False,
            "presence": False,
            "face_bbox": None,
            "emotion": "unknown",
            "emotion_zh": EMOTION_ZH["unknown"],
            "confidence": 0.0,
            "probabilities": {},
            "face_source": "none",
            "fer_latency_ms": 0.0,
            "face_detect_ms": 0.0,
            "fer_preprocess_ms": 0.0,
            "fer_inference_ms": 0.0,
            "pipeline_total_ms": 0.0,
            "timestamp": 0.0,
        }

    def _run(self):
        """Main vision loop — runs in background thread."""
        self._open_local_camera()

        while self._running:
            try:
                ret, frame = self._read_frame()
                if not ret:
                    time.sleep(0.15)
                    continue

                self._frame_count += 1

                # Run FER every N frames
                do_fer = (self._frame_count % FER_EVERY_N_FRAMES == 0)

                if do_fer:
                    pipeline_t0 = time.perf_counter()
                    face_roi, face_source, bbox, face_detect_ms = self._crop_face(frame)
                    has_face = (face_roi is not None)

                    # Self-heal: retry the FER model load periodically so a
                    # transient startup failure (CPU/IO contention during the
                    # TTS+ASR preload window) self-heals instead of leaving
                    # emotion recognition dead for the whole session.
                    if self._fer is not None and not self._fer.loaded:
                        now_m = time.monotonic()
                        if now_m - self._last_fer_retry_at >= FER_RELOAD_INTERVAL_SECONDS:
                            self._last_fer_retry_at = now_m
                            self._fer.retry_load()

                    # Sticky person-presence: face detection marks the person
                    # present immediately; movement keeps a present person
                    # present; only sustained absence (face AND motion) exits.
                    self._update_presence(has_face, frame)

                    if has_face:
                        fer_result = self._fer.analyze(face_roi)
                        fer_latency = fer_result.get("total_ms", 0.0)
                        self._record_latency(fer_latency)
                        probs = fer_result.get("probabilities", {})
                    else:
                        fer_result = {"success": False, "emotion": "unknown",
                                      "emotion_zh": EMOTION_ZH["unknown"],
                                      "confidence": 0.0, "probabilities": {},
                                      "preprocess_ms": 0.0, "inference_ms": 0.0,
                                      "total_ms": 0.0}
                        fer_latency = 0.0
                        probs = {}

                    # Smooth
                    smoothed = self._smoother.update(probs, has_face)
                    pipeline_ms = (time.perf_counter() - pipeline_t0) * 1000
                    self._face_detect_latency_ms = face_detect_ms
                    self._pipeline_latency_ms = pipeline_ms
                    self._record_pipeline_metrics(face_detect_ms, pipeline_ms)

                    # Update state
                    with self._lock:
                        self._latest_state = {
                            "face_detected": has_face or self._last_face_bbox is not None,
                            "presence": self._presence,
                            "face_bbox": bbox,
                            "emotion": smoothed["emotion"],
                            "emotion_zh": smoothed["emotion_zh"],
                            "confidence": smoothed["confidence"],
                            "probabilities": smoothed.get("probabilities", {}),
                            "candidate": smoothed.get("candidate"),
                            "candidate_age_ms": smoothed.get("candidate_age_ms", 0.0),
                            "face_source": face_source,
                            "face_detect_ms": round(face_detect_ms, 1),
                            "fer_preprocess_ms": round(fer_result.get("preprocess_ms", 0.0), 2),
                            "fer_inference_ms": round(fer_result.get("inference_ms", 0.0), 2),
                            "fer_latency_ms": round(fer_latency, 2),
                            "pipeline_total_ms": round(pipeline_ms, 1),
                            "timestamp": time.time(),
                        }

                    # Log on change
                    if smoothed["changed"] and has_face:
                        prev_emotion = "unknown"
                        print(f"[FER] emotion={smoothed['emotion']} "
                              f"conf={smoothed['confidence']:.2f} "
                              f"source={face_source} face={face_detect_ms:.1f}ms "
                              f"infer={fer_result.get('inference_ms', 0.0):.2f}ms "
                              f"pipeline={pipeline_ms:.1f}ms")

                    self._fer_count += 1
                    self._last_fer_time = time.time()
                    if self._fer_count % 300 == 0:
                        self._log_metrics()

            except Exception as e:
                print(f"[VisionWorker] Error: {e}")
                time.sleep(0.3)

        if self._cap:
            self._cap.release()

    def _open_local_camera(self):
        """Prefer the board camera, while keeping remote fallback non-fatal."""
        try:
            cap = cv2.VideoCapture(self.camera_device, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                for _ in range(3):
                    cap.read()
                self._cap = cap
                self._camera_source = "board"
                self._camera_error = ""
                print("[VisionWorker] Camera ready source=board")
                return True
            cap.release()
        except Exception as exc:
            self._camera_error = str(exc)[:160]
        self._cap = None
        self._camera_source = "pc_fallback"
        print("[VisionWorker] Board camera unavailable; using PC fallback %s"
              % CAMERA_FALLBACK_URL)
        return False

    def _read_frame(self):
        if self._cap is not None and self._cap.isOpened():
            ret, frame = self._cap.read()
            if ret:
                return True, frame
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            self._camera_source = "pc_fallback"

        try:
            req = urllib.request.Request(
                CAMERA_FALLBACK_URL,
                headers={"Connection": "close"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                jpeg = resp.read(2 * 1024 * 1024)
            frame = cv2.imdecode(
                np.frombuffer(jpeg, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if frame is None:
                raise ValueError("invalid JPEG frame")
            self._camera_source = "pc_fallback"
            self._camera_error = ""
            return True, frame
        except Exception as exc:
            self._camera_error = str(exc)[:160]
            return False, None

    def _crop_face(self, frame_bgr):
        """Extract face/head ROI from frame. Returns (roi, source, bbox).

        A fixed center crop must not be treated as a detected face: doing so made
        presence permanently true even with an empty chair.  Use OpenCV's local
        Haar detector and report no face when the detector is unavailable.
        """
        h, w = frame_bgr.shape[:2]
        if self._face_detector is None:
            return None, "none", None, 0.0

        should_detect = (self._last_face_bbox is None or
                         self._frame_count % FACE_DETECT_EVERY_N_FRAMES == 0)
        face_detect_ms = 0.0
        detected_now = False
        if should_detect:
            detect_t0 = time.perf_counter()
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            scale = min(1.0, FACE_DETECT_WIDTH / float(w))
            if scale < 1.0:
                small = cv2.resize(gray, (int(w * scale), int(h * scale)),
                                   interpolation=cv2.INTER_AREA)
            else:
                small = gray
            small = cv2.equalizeHist(small)
            min_face = max(24, int(64 * scale))
            faces = self._face_detector.detectMultiScale(
                small, scaleFactor=1.15, minNeighbors=3,
                minSize=(min_face, min_face), flags=cv2.CASCADE_SCALE_IMAGE,
            )
            face_detect_ms = (time.perf_counter() - detect_t0) * 1000
            if len(faces) > 0:
                sx, sy, sfw, sfh = max(faces, key=lambda item: item[2] * item[3])
                inv = 1.0 / scale
                x, y = int(sx * inv), int(sy * inv)
                fw, fh = int(sfw * inv), int(sfh * inv)
                margin = int(max(fw, fh) * 0.15)
                self._last_face_bbox = [
                    max(0, x - margin), max(0, y - margin),
                    min(w, x + fw + margin), min(h, y + fh + margin),
                ]
                self._last_face_frame = self._frame_count
                detected_now = True
            elif self._frame_count - self._last_face_frame >= FACE_BBOX_TTL_FRAMES:
                self._last_face_bbox = None

        if self._last_face_bbox is None:
            return None, "haar", None, face_detect_ms

        x1, y1, x2, y2 = self._last_face_bbox
        roi = frame_bgr[y1:y2, x1:x2]
        if roi.size == 0 or roi.shape[0] < 32 or roi.shape[1] < 32:
            self._last_face_bbox = None
            return None, "none", None, face_detect_ms
        bbox = [x1, y1, x2, y2]
        source = "haar" if detected_now else "bbox_cache"
        return roi, source, bbox, face_detect_ms

    def _update_presence(self, has_face, frame_bgr):
        """Sticky person-presence with hysteresis.

        face_now (a fresh Haar hit) marks the person present immediately.
        Movement keeps a present person present even when they turn away from
        the camera (typing, shifting, reading).  Presence only drops after
        PRESENCE_EXIT_SECONDS of BOTH no-face AND no-motion, so a brief head
        turn no longer flips the system to "AWAY".  An empty chair cannot
        become "present": movement alone never establishes presence from the
        away state.
        """
        now = time.monotonic()
        motion = self._detect_motion(frame_bgr)
        if has_face or (self._presence and motion):
            self._presence = True
            self._presence_activity_at = now
        elif self._presence and now - self._presence_activity_at >= PRESENCE_EXIT_SECONDS:
            self._presence = False

    def _detect_motion(self, frame_bgr):
        """Cheap frame-difference motion on a downscaled grayscale frame.

        Runs the actual diff at most every PRESENCE_MOTION_EVERY_N frames and
        caches the result in between so presence stays smooth at low CPU cost.
        """
        self._motion_frame_counter += 1
        if self._motion_frame_counter % PRESENCE_MOTION_EVERY_N != 0:
            return self._motion_present_cache
        h, w = frame_bgr.shape[:2]
        scale = PRESENCE_MOTION_WIDTH / float(w or 1)
        small = cv2.resize(
            frame_bgr,
            (PRESENCE_MOTION_WIDTH, max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self._prev_small_gray is None:
            self._prev_small_gray = gray
            self._motion_present_cache = False
            return False
        diff = cv2.absdiff(self._prev_small_gray, gray)
        self._prev_small_gray = gray
        motion = bool(cv2.countNonZero(diff) >= PRESENCE_MOTION_THRESHOLD)
        self._motion_present_cache = motion
        return motion

    @staticmethod
    def _load_face_detector():
        candidates = []
        data_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if data_dir:
            candidates.append(os.path.join(data_dir, "haarcascade_frontalface_default.xml"))
        candidates.extend([
            "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
            "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
        ])
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            detector = cv2.CascadeClassifier(path)
            if not detector.empty():
                print(f"[VisionWorker] Face detector: {path}")
                return detector
        print("[VisionWorker] No Haar face detector found; presence will stay unavailable")
        return None

    def _record_latency(self, ms):
        self._fer_latencies.append(ms)
        if len(self._fer_latencies) > 100:
            self._fer_latencies.pop(0)
        if self._fer_latencies:
            arr = np.array(self._fer_latencies)
            self._fer_latency_avg = float(arr.mean())
            self._fer_latency_p90 = float(np.percentile(arr, 90))

    def _record_pipeline_metrics(self, face_detect_ms, pipeline_ms):
        if face_detect_ms > 0:
            self._face_detect_latencies.append(face_detect_ms)
            self._face_detect_latencies = self._face_detect_latencies[-100:]
        self._pipeline_latencies.append(pipeline_ms)
        self._pipeline_latencies = self._pipeline_latencies[-300:]

    def _log_metrics(self):
        now = time.monotonic()
        elapsed = max(0.001, now - self._metrics_time)
        frames = self._fer_count - self._metrics_frame
        fps = frames / elapsed
        haar_avg = (float(np.mean(self._face_detect_latencies))
                    if self._face_detect_latencies else 0.0)
        fer_avg = (float(np.mean(self._fer_latencies))
                   if self._fer_latencies else 0.0)
        pipeline_avg = (float(np.mean(self._pipeline_latencies))
                        if self._pipeline_latencies else 0.0)
        print(f"[VISION_METRICS] fps={fps:.1f} haar_avg={haar_avg:.1f}ms "
              f"fer_avg={fer_avg:.2f}ms pipeline_avg={pipeline_avg:.1f}ms "
              f"detect_every={FACE_DETECT_EVERY_N_FRAMES}", flush=True)
        self._metrics_time = now
        self._metrics_frame = self._fer_count
