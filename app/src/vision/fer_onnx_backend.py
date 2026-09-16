#!/usr/bin/env python3
"""FER ONNX Runtime Backend — SC171 CPU.
Loads ONNX model once, provides analyze(face_bgr) -> dict.
"""
import sys, os, time
import numpy as np
import cv2

# ---- constants ----
# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT = os.environ.get("NUANYU_ROOT", "/userdata_fibo")
DEFAULT_MODEL_PATH = os.path.join(NUANYU_ROOT, "fer_ort", "fer_emotion_clean.onnx")
INPUT_NAME  = "x"
OUTPUT_NAME = "predictions"
INPUT_SHAPE = (1, 64, 64, 1)   # NHWC, grayscale
DTYPE       = np.float32

LABELS = [
    "angry",
    "disgust",
    "fear",
    "happy",
    "sad",
    "surprise",
    "neutral",
]

EMOTION_ZH = {
    "angry":    "生气",
    "disgust":  "厌恶",
    "fear":     "害怕",
    "happy":    "开心",
    "sad":      "难过",
    "surprise": "惊讶",
    "neutral":  "平静",
    "unknown":  "未识别",
}

CONFIDENCE_THRESHOLD = 0.40

# ---- backend ----
class FEROnnxBackend:
    def __init__(self, model_path: str = None):
        self.model_path = model_path or DEFAULT_MODEL_PATH
        self.session = None
        self._loaded = False
        self._load_error = None
        self._init_session()

    def _init_session(self):
        try:
            import onnxruntime as ort
            t0 = time.perf_counter()
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self.session = ort.InferenceSession(
                self.model_path,
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            load_ms = (time.perf_counter() - t0) * 1000
            self._loaded = True
            self._load_error = None
            # verify I/O names match expected
            in_name = self.session.get_inputs()[0].name
            out_name = self.session.get_outputs()[0].name
            print(f"[FER] Loaded {os.path.basename(self.model_path)} "
                  f"in={in_name} out={out_name} ({load_ms:.1f}ms)")
        except Exception as e:
            self._loaded = False
            self._load_error = str(e)
            print(f"[FER] Failed to load model: {e}")

    def retry_load(self) -> None:
        """Re-attempt model load after a transient startup failure.

        The VisionWorker calls this periodically while the model is not
        loaded, so an ONNX session that failed once (CPU/IO contention during
        the TTS+ASR preload window, etc.) self-heals instead of leaving
        emotion recognition dead for the whole session.
        """
        if self._loaded:
            return
        self._init_session()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def preprocess(self, face_bgr: np.ndarray) -> np.ndarray:
        """BGR -> Gray -> Resize 64x64 -> /255 -> (x-0.5)*2 -> NHWC (1,64,64,1)"""
        if face_bgr is None or face_bgr.size == 0:
            raise ValueError("Empty input image")
        if len(face_bgr.shape) < 2:
            raise ValueError(f"Invalid image shape: {face_bgr.shape}")

        gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (64, 64))
        gray_f = gray.astype(np.float32) / 255.0
        gray_f = (gray_f - 0.5) * 2.0
        gray_f = gray_f.reshape(1, 64, 64, 1)          # NHWC
        gray_f = np.ascontiguousarray(gray_f)
        return gray_f

    def analyze(self, face_bgr: np.ndarray) -> dict:
        """Run FER inference on a cropped face BGR image.

        Returns:
            dict with success, emotion, confidence, probabilities, backend,
                 preprocess_ms, inference_ms, total_ms, error
        """
        result = {
            "success": False,
            "emotion": "unknown",
            "emotion_zh": EMOTION_ZH["unknown"],
            "confidence": 0.0,
            "probabilities": {},
            "backend": "onnxruntime_cpu",
            "preprocess_ms": 0.0,
            "inference_ms": 0.0,
            "total_ms": 0.0,
            "error": None,
        }

        if not self._loaded:
            result["error"] = f"Model not loaded: {self._load_error}"
            return result

        t_total = time.perf_counter()

        # --- preprocess ---
        t0 = time.perf_counter()
        try:
            inp = self.preprocess(face_bgr)
        except Exception as e:
            result["error"] = f"Preprocess failed: {e}"
            result["total_ms"] = (time.perf_counter() - t_total) * 1000
            return result
        result["preprocess_ms"] = (time.perf_counter() - t0) * 1000

        # --- inference ---
        t0 = time.perf_counter()
        try:
            out = self.session.run([OUTPUT_NAME], {INPUT_NAME: inp})[0]
        except Exception as e:
            result["error"] = f"Inference failed: {e}"
            result["total_ms"] = (time.perf_counter() - t_total) * 1000
            return result
        result["inference_ms"] = (time.perf_counter() - t0) * 1000

        # --- validate output ---
        probs = out.squeeze()
        if probs.shape != (7,):
            result["error"] = f"Unexpected output shape: {probs.shape}, expected (7,)"
            result["total_ms"] = (time.perf_counter() - t_total) * 1000
            return result
        if np.isnan(probs).any() or np.isinf(probs).any():
            result["error"] = "Output contains NaN or Inf"
            result["total_ms"] = (time.perf_counter() - t_total) * 1000
            return result

        # --- success ---
        top_idx = int(np.argmax(probs))
        top_conf = float(probs[top_idx])
        emotion_en = LABELS[top_idx] if top_conf >= CONFIDENCE_THRESHOLD else "unknown"

        result["success"] = True
        result["emotion"] = emotion_en
        result["emotion_zh"] = EMOTION_ZH.get(emotion_en, EMOTION_ZH["unknown"])
        result["confidence"] = top_conf
        result["probabilities"] = {LABELS[i]: float(probs[i]) for i in range(7)}
        result["total_ms"] = (time.perf_counter() - t_total) * 1000
        return result

    def close(self):
        if self.session is not None:
            del self.session
            self.session = None
            self._loaded = False
