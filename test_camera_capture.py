import unittest
from unittest.mock import Mock, patch

from camera_session import CameraDetectionSession
from esp32_bridge import Esp32Bridge


class CameraAutoCaptureTests(unittest.TestCase):
    def test_rotten_cannot_be_overwritten_by_later_good(self) -> None:
        session = self.make_session()
        for label in ("rotten", "good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.8)
        self.assertFalse(session._captured_qualities)

    def test_rotten_wins_over_good_and_no_egg_in_same_frame(self) -> None:
        detections = [
            {"label": label, "confidence": confidence, "box": [40, 40, 60, 60]}
            for label, confidence in (("Rotten", 0.6), ("Good", 0.95), ("no egg", 0.99))
        ]
        self.assertEqual(
            CameraDetectionSession._select_frame_observation(detections, (30, 30, 70, 70)),
            ("rotten", 0.6),
        )

    def test_live_status_reports_each_quality_from_detection_boxes(self) -> None:
        session = CameraDetectionSession()
        for label in ("Rotten", "Crack", "Good", "Undefined"):
            with self.subTest(label=label):
                session._latest_result = {
                    "detections": [{
                        "label": label, "confidence": 0.87,
                        "box": [40, 40, 60, 60],
                    }],
                    "inspection_zone": [30, 30, 70, 70],
                }
                status = session.status()
                self.assertTrue(status["detection_ready"])
                self.assertEqual(status["counts"], {label.lower(): 1})
                self.assertEqual(status["total"], 1)
                self.assertEqual(status["zone_quality"], label.lower())
                self.assertEqual(status["zone_confidence"], 0.87)
                self.assertEqual(len(status["detections"]), 1)

    def test_live_status_clears_previous_detection_on_empty_frame(self) -> None:
        session = CameraDetectionSession()
        self.assertFalse(session.status()["detection_ready"])
        session._latest_result = {"detections": [
            {"label": "Good", "confidence": 0.8, "box": [0, 0, 20, 20]},
        ], "inspection_zone": [30, 30, 70, 70]}
        self.assertEqual(session.status()["counts"], {"good": 1})
        self.assertEqual(session.status()["zone_quality"], "no egg")
        session._latest_result = {"detections": []}
        status = session.status()
        self.assertTrue(status["detection_ready"])
        self.assertEqual(status["counts"], {})
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["zone_quality"], "no egg")

    def make_session(self) -> CameraDetectionSession:
        session = CameraDetectionSession()
        session.capture_min_samples = 3
        session.passage_exit_samples = 2
        return session

    def test_crack_cannot_be_overwritten_by_later_good(self) -> None:
        session = self.make_session()
        session._update_auto_capture("crack", 0.71)
        session._update_auto_capture("good", 0.89)
        session._update_auto_capture("good", 0.92)
        session._update_auto_capture("no egg", 0.0)
        session._update_auto_capture("no egg", 0.0)

        self.assertFalse(session._captured_qualities)

    def test_short_false_passage_is_not_queued(self) -> None:
        session = self.make_session()
        session._update_auto_capture("good", 0.8)
        session._update_auto_capture("no egg", 0.0)
        session._update_auto_capture("no egg", 0.0)

        self.assertFalse(session._captured_qualities)

    def test_separate_eggs_are_queued_in_conveyor_order(self) -> None:
        session = self.make_session()
        for label in ("good", "good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.8)
        for label in ("crack", "good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.7)
        for label in ("undefined", "undefined", "undefined", "no egg", "no egg"):
            session._update_auto_capture(label, 0.8)

        self.assertEqual(
            [result["label"] for result in session._captured_qualities],
            ["good", "undefined"],
        )

    def test_each_defect_sends_immediately_once_per_passage(self) -> None:
        for defect in ("crack", "rotten"):
            with self.subTest(defect=defect):
                session = self.make_session()
                handler = Mock()
                session.set_reject_handler(handler)
                session._process_auto_capture(defect, 0.8)
                handler.assert_called_once_with(defect)
                for label in (defect, "good", "rotten", "no egg", "no egg"):
                    session._process_auto_capture(label, 0.9)
                handler.assert_called_once_with(defect)
                self.assertFalse(session._captured_qualities)
                session._process_auto_capture(defect, 0.8)
                self.assertEqual(handler.call_count, 2)

    def test_failed_send_retries_while_egg_is_visible(self) -> None:
        session = self.make_session()
        handler = Mock(side_effect=[RuntimeError("Disconnected"), None])
        session.set_reject_handler(handler)
        session._process_auto_capture("rotten", 0.8)
        self.assertIn("Disconnected", session.status()["reject_error"])
        session._process_auto_capture("good", 0.9)
        self.assertEqual(handler.call_count, 2)
        handler.assert_called_with("rotten")
        self.assertIsNone(session.status()["reject_error"])

    def test_failed_send_is_not_replayed_after_egg_leaves(self) -> None:
        session = self.make_session()
        handler = Mock(side_effect=RuntimeError("Disconnected"))
        session.set_reject_handler(handler)
        for label in ("crack", "no egg", "no egg"):
            session._process_auto_capture(label, 0.8)
        attempts = handler.call_count
        handler.side_effect = None
        session._process_auto_capture("good", 0.8)
        self.assertEqual(handler.call_count, attempts)
        self.assertFalse(session._captured_qualities)

    def test_inference_sends_reject_serial_command_without_scale_event(self) -> None:
        for defect in ("Crack", "Rotten"):
            with self.subTest(defect=defect):
                session = self.make_session()
                bridge = Esp32Bridge()
                bridge._connected = True
                bridge._serial = Mock()
                bridge._diagnostics["pca9685_ready"] = True
                session.set_reject_handler(bridge.reject_egg)
                frame = Mock()
                frame.shape = (100, 100, 3)
                frame.copy.return_value = frame
                session._latest_raw_frame = frame
                session._raw_sequence = 1

                def detect(_frame):
                    session._stop_event.set()
                    return {"detections": [{
                        "label": defect, "confidence": 0.9,
                        "box": [40, 40, 60, 60],
                    }]}

                with patch("camera_session.detect_image", side_effect=detect):
                    session._inference_loop()
                bridge._serial.write.assert_called_once_with(
                    f"REJECT:{defect.upper()}\n".encode("ascii")
                )
                self.assertFalse(session._captured_qualities)

    def test_good_and_undefined_only_enter_weighing_queue(self) -> None:
        session = self.make_session()
        handler = Mock()
        session.set_reject_handler(handler)
        for quality in ("good", "undefined"):
            for label in (quality, quality, quality, "no egg", "no egg"):
                session._process_auto_capture(label, 0.8)
        handler.assert_not_called()
        self.assertEqual(
            [entry["label"] for entry in session._captured_qualities],
            ["good", "undefined"],
        )

    def test_only_boxes_centered_in_zone_are_used(self) -> None:
        detections = [
            {"label": "Crack", "confidence": 0.95, "box": [0, 0, 20, 20]},
            {"label": "Good", "confidence": 0.80, "box": [40, 40, 60, 60]},
        ]

        self.assertEqual(
            CameraDetectionSession._select_frame_observation(
                detections, (30, 30, 70, 70)
            ),
            ("good", 0.80),
        )

    def test_crack_wins_over_higher_confidence_good_in_same_frame(self) -> None:
        detections = [
            {"label": "Good", "confidence": 0.97, "box": [40, 40, 60, 60]},
            {"label": "Crack", "confidence": 0.60, "box": [42, 42, 62, 62]},
        ]

        self.assertEqual(
            CameraDetectionSession._select_frame_observation(
                detections, (30, 30, 70, 70)
            ),
            ("crack", 0.60),
        )


if __name__ == "__main__":
    unittest.main()
