"""Timed passage regressions with synthetic detections and no hardware I/O."""

import os
import unittest
from unittest.mock import Mock, patch

from camera_session import CameraDetectionSession
from config import ConfigurationError


class CameraExitTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "CAMERA_PASSAGE_DIAGNOSTICS": "false",
            "CAMERA_PASSAGE_EXIT_SECONDS": "0.60",
            "CAMERA_PASSAGE_OCCUPANCY_MARGIN": "0.01",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 100.0
        timer = patch("camera_session.monotonic", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.session = CameraDetectionSession()
        self.session._running = True
        self.session.capture_min_samples = 3
        self.session.passage_exit_samples = 2
        self.reject = Mock()
        self.completed = Mock()
        self.session.set_reject_handler(self.reject)
        self.session.set_capture_handler(self.completed)
        self.zone = (192.0, 158.4, 1088.0, 561.6)

    def observe(self, t, label="good", *, occupied=False):
        self.now = 100.0 + t
        self.session._process_auto_capture(label, .9, passage_occupied=occupied)

    def detection(self, t, cx, *, label="good", cy=360):
        detections = [] if cx is None else [{
            "label": label, "confidence": .9,
            "box": [cx - 50, cy - 40, cx + 50, cy + 40],
        }]
        selected, confidence = self.session._select_frame_observation(detections, self.zone)
        occupied = self.session._has_passage_occupancy(detections, self.zone, 1280, 720)
        self.now = 100.0 + t
        self.session._process_auto_capture(selected, confidence, passage_occupied=occupied)
        return selected, occupied

    def begin_good(self, t=0):
        for delta in (0, .2, .4):
            self.observe(t + delta)

    def exit(self, t):
        self.observe(t, "no egg")
        self.observe(t + .61, "no egg")

    def test_one_egg_many_observations_counts_once_only_after_exit(self):
        for i in range(60):
            self.observe(i * .2)
        self.assertEqual(self.session._egg_count, 0)
        self.exit(12)
        for i in range(10):
            self.observe(13 + i * .2, "no egg")
        self.assertEqual(self.session._egg_count, 1)
        self.assertEqual(self.session._quality_counts["good"], 1)
        self.assertEqual(len(self.session._captured_qualities), 1)
        self.completed.assert_called_once()

    def test_multiple_two_result_dropouts_keep_the_original_identity(self):
        self.begin_good()
        original = self.session._active_capture
        for base in (.6, 1.4, 2.2):
            self.observe(base, "no egg")
            self.observe(base + .2, "no egg")
            self.assertIs(self.session._active_capture, original)
            self.assertEqual(self.session._egg_count, 0)
            self.observe(base + .4)
            self.assertIsNone(self.session._passage_missing_since)
        self.exit(3)
        self.assertEqual(self.session._egg_count, 1)
        self.assertEqual(self.completed.call_args.args[0]["capture_id"], 1)

    def test_both_elapsed_duration_and_miss_count_are_required(self):
        self.begin_good()
        self.observe(.6, "no egg")
        self.observe(.8, "no egg")
        self.observe(1.0, "no egg")
        self.assertEqual(self.session._egg_count, 0)
        self.observe(1.21, "no egg")
        self.assertEqual(self.session._egg_count, 1)
        # A long time since the last detection is not itself exit evidence.
        self.begin_good(2)
        self.observe(10, "no egg")
        self.assertEqual(self.session._egg_count, 1)
        self.observe(10.61, "no egg")
        self.assertEqual(self.session._egg_count, 2)

    def test_custom_sample_count_is_not_bypassed_by_time(self):
        self.session.passage_exit_samples = 4
        self.begin_good()
        for t in (1, 2, 3):
            self.observe(t, "no egg")
            self.assertEqual(self.session._egg_count, 0)
        self.observe(4, "no egg")
        self.assertEqual(self.session._egg_count, 1)

    def test_boundary_jitter_keeps_identity_without_quality_or_rejection(self):
        self.begin_good()
        original = self.session._active_capture
        for i in range(10):
            selected, occupied = self.detection(.6 + i * .2, 1094, label="crack")
            self.assertEqual(selected, "no egg")
            self.assertTrue(occupied)
        self.assertIs(self.session._active_capture, original)
        self.assertEqual(len(self.session._passage_samples), 3)
        self.assertEqual(original["label"], "good")
        self.assertEqual(self.session._egg_count, 0)
        self.reject.assert_not_called()
        # Occupancy alone must not authorize a stale Good scale claim.
        self.assertIsNone(self.session.wait_for_captured_quality(0, allow_loaded_capture=True))
        self.detection(2.7, 640)
        self.assertIs(self.session._active_capture, original)
        self.exit(3)
        self.assertEqual(self.session._egg_count, 1)

    def test_margin_only_detection_cannot_start_a_passage(self):
        for label in ("good", "crack", "rotten"):
            self.detection(0, 1094, label=label)
            self.assertIsNone(self.session._active_capture)
        self.reject.assert_not_called()
        self.assertEqual(self.session._egg_count, 0)

    def test_presence_in_margin_cancels_pending_absence(self):
        self.begin_good()
        self.observe(.6, "no egg")
        self.detection(1, 1094)
        self.assertIsNone(self.session._passage_missing_since)
        self.observe(1.2, "no egg")
        self.observe(1.5, "no egg")
        self.assertEqual(self.session._egg_count, 0)
        self.observe(1.81, "no egg")
        self.assertEqual(self.session._egg_count, 1)

    def test_margin_applies_on_each_edge_but_not_beyond_it(self):
        self.begin_good()
        for cx, cy in ((186, 360), (1094, 360), (640, 154), (640, 566)):
            selected, occupied = self.detection(1, cx, cy=cy)
            self.assertEqual(selected, "no egg")
            self.assertTrue(occupied)
        selected, occupied = self.detection(2, 1110)
        self.assertEqual(selected, "no egg")
        self.assertFalse(occupied)
        self.detection(2.61, 1110)
        self.assertEqual(self.session._egg_count, 1)

    def test_three_eggs_with_dropouts_count_three_in_order(self):
        for base in (0, 4, 8):
            self.begin_good(base)
            self.observe(base + .6, "no egg")
            self.observe(base + .8, "no egg")
            self.observe(base + 1)
            self.exit(base + 1.2)
        self.assertEqual(self.session._egg_count, 3)
        self.assertEqual([c["capture_id"] for c in self.session._captured_qualities], [1, 2, 3])
        self.assertEqual(self.completed.call_count, 3)

    def test_close_eggs_with_observed_clearance_remain_distinct(self):
        self.begin_good()
        self.exit(.6)  # exit confirmed at 1.21
        for t in (1.25, 1.3, 1.35):
            self.observe(t)
        self.assertEqual(self.session._active_capture["capture_id"], 2)
        self.exit(1.4)
        self.assertEqual(self.session._egg_count, 2)

    def test_insufficient_clearance_is_explicitly_not_distinguishable(self):
        self.begin_good()
        self.observe(.6, "no egg")
        self.observe(.8, "no egg")
        self.begin_good(1)
        self.assertEqual(self.session._active_capture["capture_id"], 1)
        self.exit(2)
        self.assertEqual(self.session._egg_count, 1)

    def test_loaded_claim_preserves_payload_without_count_or_duplicate_delivery(self):
        self.begin_good()
        capture = self.session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(capture["capture_id"], 1)
        self.assertTrue(capture["claimed_on_scale"])
        self.assertEqual(self.session._egg_count, 0)
        self.assertIsNone(self.session._last_capture)
        self.observe(.6, "no egg")
        self.observe(.8, "no egg")
        self.observe(1)
        self.assertEqual(self.session._active_capture["capture_id"], capture["capture_id"])
        self.assertIsNone(self.session.wait_for_captured_quality(0, allow_loaded_capture=True))
        self.exit(1.2)
        self.assertEqual(self.session._egg_count, 1)
        self.assertEqual(self.session._last_capture["capture_id"], 1)
        self.assertFalse(self.session._captured_qualities)
        self.completed.assert_not_called()
        self.assertIsNone(self.session.wait_for_captured_quality(0, allow_loaded_capture=True))
        self.begin_good(3)
        next_capture = self.session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(next_capture["capture_id"], 2)

    def test_defects_reject_immediately_once_despite_dropout_and_count_on_exit(self):
        for defect in ("crack", "rotten"):
            with self.subTest(defect=defect):
                self.reject.reset_mock()
                base = 0 if defect == "crack" else 4
                self.observe(base, defect)
                self.reject.assert_called_once_with(defect)
                self.observe(base + .2, "no egg")
                self.observe(base + .4, "no egg")
                self.observe(base + .6, "good")
                self.assertEqual(self.session._active_capture["label"], defect)
                self.assertIsNone(self.session.wait_for_captured_quality(0, allow_loaded_capture=True))
                self.exit(base + .8)
                self.reject.assert_called_once_with(defect)
                self.assertFalse(self.session._captured_qualities)
        self.assertEqual(self.session._egg_count, 2)

    def test_configuration_is_validated_and_can_disable_margin(self):
        for name, value in (("CAMERA_PASSAGE_EXIT_SECONDS", "0"),
                            ("CAMERA_PASSAGE_OCCUPANCY_MARGIN", "-0.01")):
            with patch.dict(os.environ, {name: value}), self.assertRaises(ConfigurationError):
                CameraDetectionSession()
        with patch.dict(os.environ, {"CAMERA_PASSAGE_EXIT_SECONDS": "0.8",
                                    "CAMERA_PASSAGE_OCCUPANCY_MARGIN": "0"}):
            configured = CameraDetectionSession()
        self.assertEqual(configured.passage_exit_seconds, .8)
        self.assertEqual(configured.passage_occupancy_margin, 0)
        self.assertFalse(configured._has_passage_occupancy(
            [{"label": "good", "confidence": .9, "box": [1084, 350, 1104, 370]}],
            self.zone, 1280, 720,
        ))


if __name__ == "__main__":
    unittest.main()
