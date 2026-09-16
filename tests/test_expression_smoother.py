#!/usr/bin/env python3
"""Deterministic tests for time-based expression stabilization."""
import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.vision.expression_smoother import ExpressionSmoother, LABELS


def distribution(winner, confidence=0.82):
    rest = (1.0 - confidence) / (len(LABELS) - 1)
    return {label: confidence if label == winner else rest for label in LABELS}


class ExpressionSmootherTest(unittest.TestCase):
    def setUp(self):
        self.smoother = ExpressionSmoother(
            alpha=0.45,
            confirm_seconds=0.18,
            min_hold_seconds=0.45,
            no_face_timeout=0.75,
        )

    def acquire(self, label="happy"):
        self.smoother.update(distribution(label), True, now=0.0)
        return self.smoother.update(distribution(label), True, now=0.20)

    def test_initial_class_requires_time_confirmation(self):
        first = self.smoother.update(distribution("happy"), True, now=0.0)
        early = self.smoother.update(distribution("happy"), True, now=0.10)
        stable = self.smoother.update(distribution("happy"), True, now=0.20)
        self.assertEqual(first["emotion"], "unknown")
        self.assertEqual(early["emotion"], "unknown")
        self.assertEqual(stable["emotion"], "happy")
        self.assertTrue(stable["changed"])

    def test_single_frame_spike_does_not_switch(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        spike = self.smoother.update(distribution("sad", 0.95), True, now=0.35)
        recovered = self.smoother.update(distribution("happy"), True, now=0.40)
        self.assertEqual(spike["emotion"], "happy")
        self.assertEqual(recovered["emotion"], "happy")

    def test_sustained_stronger_class_switches_after_hold(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        self.smoother.update(distribution("sad", 0.95), True, now=0.70)
        candidate = self.smoother.update(distribution("sad", 0.95), True, now=0.80)
        switched = self.smoother.update(distribution("sad", 0.95), True, now=1.00)
        self.assertEqual(candidate["emotion"], "happy")
        self.assertEqual(candidate["candidate"], "sad")
        self.assertEqual(switched["emotion"], "sad")
        self.assertTrue(switched["changed"])

    def test_no_face_uses_timeout(self):
        self.assertEqual(self.acquire()["emotion"], "happy")
        held = self.smoother.update({}, False, now=0.80)
        cleared = self.smoother.update({}, False, now=1.00)
        self.assertEqual(held["emotion"], "happy")
        self.assertEqual(cleared["emotion"], "unknown")

    def test_confidence_updates_while_label_is_stable(self):
        first = self.acquire()["confidence"]
        later = self.smoother.update(distribution("happy", 0.60), True, now=0.30)
        self.assertEqual(later["emotion"], "happy")
        self.assertNotEqual(first, later["confidence"])


if __name__ == "__main__":
    unittest.main()
