#!/usr/bin/env python3
"""Phase 1: Vision device ownership and graceful-degradation tests.

No board, camera or model file is required: cv2 and onnxruntime are installed
into ``sys.modules`` as mocks for the duration of each test, so nothing here
opens /dev/video2 or loads a real ONNX session.
"""

import ast
import os
import pathlib
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# vision_worker / fer_onnx_backend bind ``import cv2`` / ``import onnxruntime``
# at module level, so patching sys.modules only helps if the module is imported
# *after* the patch and is not already cached from an earlier test.
_VISION_MODULES = (
    "src.vision.vision_worker", "vision.vision_worker",
    "src.vision.fer_onnx_backend", "vision.fer_onnx_backend",
)


def _forget_vision_modules():
    for name in _VISION_MODULES:
        sys.modules.pop(name, None)


class _FakeModule:
    """Temporarily replace one sys.modules entry.

    ``unittest.mock.patch.dict("sys.modules", ...)`` snapshots the whole dict
    and restores it by clearing sys.modules first, which drops every module
    imported while the patch was active — re-importing numpy after that aborts
    with "cannot load module more than once per process".  Only the single key
    is touched here.
    """

    def __init__(self, name, fake):
        self._name = name
        self._fake = fake
        self._missing = object()
        self._previous = self._missing

    def __enter__(self):
        self._previous = sys.modules.get(self._name, self._missing)
        sys.modules[self._name] = self._fake
        return self._fake

    def __exit__(self, *exc_info):
        if self._previous is self._missing:
            sys.modules.pop(self._name, None)
        else:
            sys.modules[self._name] = self._previous
        return False


class VisionLifecycleTest(unittest.TestCase):
    """Camera ownership and /api/status degradation."""

    def setUp(self):
        self._mock_cv2 = MagicMock()
        # Make VideoCapture appear functional
        self._mock_cv2.VideoCapture.return_value.isOpened.return_value = True
        self._mock_cv2.VideoCapture.return_value.read.return_value = (
            True, MagicMock(shape=(480, 640, 3)))
        self._cv2_patch = _FakeModule("cv2", self._mock_cv2)
        self._cv2_patch.__enter__()
        _forget_vision_modules()

    def tearDown(self):
        self._cv2_patch.__exit__(None, None, None)
        # Never leak a module that holds a mocked cv2 to the next test file.
        _forget_vision_modules()

    def test_vision_worker_is_single_camera_owner(self):
        """VisionWorker._run opens cv2.VideoCapture exactly once."""
        from src.vision.vision_worker import VisionWorker
        worker = VisionWorker()
        worker.start()
        # Let the background thread open and then spin.
        time.sleep(0.3)
        worker.stop()
        # VideoCapture() should be called exactly once
        call_count = self._mock_cv2.VideoCapture.call_count
        self.assertEqual(call_count, 1,
                         "VisionWorker should open VideoCapture exactly once, got %d" % call_count)

    def test_health_returns_graceful_degradation_when_no_camera(self):
        """VisionWorker.health() works even without a running camera thread."""
        from src.vision.vision_worker import VisionWorker
        worker = VisionWorker()
        h = worker.health()
        self.assertIn("running", h)
        self.assertIn("fer_loaded", h)
        # When not started, running should be False.
        self.assertFalse(h["running"])

    def test_status_without_vision_worker(self):
        """/api/status fields degrade cleanly when no VisionWorker is running."""
        from src.core.runtime_services import RuntimeServices
        vision_state = RuntimeServices().get_vision_state()
        self.assertFalse(vision_state["face_detected"])
        self.assertEqual(vision_state["emotion"], "unknown")
        self.assertEqual(vision_state["emotion_zh"], "未识别")
        self.assertEqual(vision_state["face_source"], "none")

    def test_deprecated_vision_loop_emits_warning(self):
        """The legacy NuanyuCore.vision_loop still declares itself deprecated.

        nuanyu_web.py is Linux-only (it imports termios), so the method is read
        out of the source instead of called; it must warn before it touches the
        camera, because VisionWorker is the single camera owner now.
        """
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "nuanyu_web.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(
            (node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == "vision_loop"),
            None,
        )
        self.assertIsNotNone(method, "NuanyuCore.vision_loop must still exist")

        warns_deprecated = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "warn"
            and any(isinstance(arg, ast.Name) and arg.id == "DeprecationWarning"
                    for arg in node.args)
            for node in ast.walk(method)
        )
        self.assertTrue(warns_deprecated,
                        "vision_loop must emit a DeprecationWarning")

    def test_haar_detector_fallback(self):
        """_load_face_detector returns None when no Haar file exists."""
        from src.vision.vision_worker import VisionWorker
        # Hide the real filesystem from the candidate probe so the result does
        # not depend on what OpenCV data happens to be installed on the host.
        with patch("os.path.exists", return_value=False):
            detector = VisionWorker._load_face_detector()
        # None is safe — presence detection degrades instead of crashing.
        self.assertIsNone(detector)


class FerBackendTest(unittest.TestCase):
    """FER model loading and inference.

    cv2 is mocked as well: ``fer_onnx_backend`` imports it at module scope, and
    these tests are about model loading, labels and error handling — not about
    pixel conversion.
    """

    def setUp(self):
        self._mock_ort = MagicMock()
        self._mock_ort.InferenceSession.return_value.get_inputs.return_value = [
            MagicMock(name="x")]
        self._mock_ort.InferenceSession.return_value.get_outputs.return_value = [
            MagicMock(name="predictions")]
        self._ort_patch = _FakeModule("onnxruntime", self._mock_ort)
        self._cv2_patch = _FakeModule("cv2", MagicMock())
        self._ort_patch.__enter__()
        self._cv2_patch.__enter__()
        _forget_vision_modules()

    def tearDown(self):
        self._cv2_patch.__exit__(None, None, None)
        self._ort_patch.__exit__(None, None, None)
        _forget_vision_modules()

    def test_fer_model_initializes_with_mocked_onnx(self):
        """FEROnnxBackend loads without crashing (mocked ONNX)."""
        from src.vision.fer_onnx_backend import FEROnnxBackend
        backend = FEROnnxBackend()
        self.assertTrue(backend.loaded)

    def test_labels_match_expected_set(self):
        """The 7 emotion labels haven't changed accidentally."""
        from src.vision.fer_onnx_backend import LABELS, EMOTION_ZH
        self.assertEqual(LABELS, ["angry", "disgust", "fear", "happy", "sad", "surprise", "neutral"])
        self.assertIn("neutral", EMOTION_ZH)
        self.assertEqual(EMOTION_ZH["neutral"], "平静")

    def test_analyze_empty_input_returns_error(self):
        """analyze with empty array returns failure, not a crash."""
        import numpy as np
        from src.vision.fer_onnx_backend import FEROnnxBackend
        backend = FEROnnxBackend()
        result = backend.analyze(np.zeros((1, 1, 3), dtype=np.uint8))
        self.assertFalse(result["success"])
        self.assertIsNotNone(result.get("error"))


class ExpressionSmootherTest(unittest.TestCase):
    """EMA-based expression stabilisation (regression)."""

    LABELS = ["angry", "disgust", "fear", "happy", "sad", "surprise", "neutral"]

    def distribution(self, winner, confidence=0.82):
        rest = (1.0 - confidence) / (len(self.LABELS) - 1)
        return {label: confidence if label == winner else rest for label in self.LABELS}

    def setUp(self):
        from src.vision.expression_smoother import ExpressionSmoother
        self.smoother = ExpressionSmoother(
            alpha=0.45, confirm_seconds=0.18, min_hold_seconds=0.45, no_face_timeout=0.75)

    def acquire(self, label="happy"):
        self.smoother.update(self.distribution(label), True, now=0.0)
        return self.smoother.update(self.distribution(label), True, now=0.20)

    def test_initial_class_requires_confirmation_time(self):
        first = self.smoother.update(self.distribution("happy"), True, now=0.0)
        early = self.smoother.update(self.distribution("happy"), True, now=0.10)
        stable = self.smoother.update(self.distribution("happy"), True, now=0.20)
        self.assertEqual(first["emotion"], "unknown")
        self.assertEqual(early["emotion"], "unknown")
        self.assertEqual(stable["emotion"], "happy")
        self.assertTrue(stable["changed"])

    def test_single_frame_spike_does_not_flip(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        spike = self.smoother.update(self.distribution("sad", 0.95), True, now=0.35)
        recovered = self.smoother.update(self.distribution("happy"), True, now=0.40)
        self.assertEqual(spike["emotion"], "happy")
        self.assertEqual(recovered["emotion"], "happy")

    def test_sustained_stronger_class_switches_after_hold(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        self.smoother.update(self.distribution("sad", 0.95), True, now=0.70)
        candidate = self.smoother.update(self.distribution("sad", 0.95), True, now=0.80)
        switched = self.smoother.update(self.distribution("sad", 0.95), True, now=1.00)
        self.assertEqual(candidate["emotion"], "happy")
        self.assertEqual(candidate["candidate"], "sad")
        self.assertEqual(switched["emotion"], "sad")
        self.assertTrue(switched["changed"])

    def test_no_face_timeout_resets_to_unknown(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        gone = self.smoother.update(self.distribution("happy"), False, now=1.20)
        self.assertEqual(gone["emotion"], "unknown")


if __name__ == "__main__":
    unittest.main()
