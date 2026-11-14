import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from camera_session import CameraDetectionSession, CameraSessionError


class _FakeCameraCapture:
    def __init__(self, session):
        self.session = session
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        return True, SimpleNamespace(shape=(720, 1280, 3))

    def release(self):
        self.released = True


class _FakeEncodedJpeg:
    def tobytes(self):
        return b"jpeg-frame"


class CameraReadinessPublicationTests(unittest.TestCase):
    def run_capture_loop(self, encode_success=True):
        session = CameraDetectionSession()
        session._running = True
        session._runtime_context = {"session_ref": "SES-test", "runtime_generation": "gen-test"}
        capture = _FakeCameraCapture(session)
        session._capture = capture
        callbacks = []
        session.set_lifecycle_handler(
            lambda kind, context: callbacks.append((kind, session.status()["frame_ready"], context))
        )

        fake_cv2 = SimpleNamespace(
            CAP_ANY=0, CAP_DSHOW=1, CAP_MSMF=2, IMWRITE_JPEG_QUALITY=1,
            VideoCapture=lambda *_args: capture,
            imencode=lambda *_args: (encode_success, _FakeEncodedJpeg() if encode_success else None),
        )
        with patch.dict(sys.modules, {"cv2": fake_cv2}), \
             patch("camera_session.configure_camera", return_value={"mode_verified": True}), \
             patch("camera_session.verify_frame", side_effect=lambda negotiation, *_: negotiation), \
             patch("camera_session.annotate_image", side_effect=lambda frame, _result: frame):
            session._capture_loop()
        return session, callbacks, capture

    def test_camera_ready_callback_follows_first_published_jpeg(self):
        session, callbacks, _capture = self.run_capture_loop()
        ready = [event for event in callbacks if event[0] == "ready"]
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0][1], "frame_ready must be true inside readiness callback")
        self.assertEqual(ready[0][2]["session_ref"], "SES-test")
        self.assertTrue(session.status()["frame_ready"])

    def test_failed_jpeg_publication_does_not_emit_camera_ready(self):
        session, callbacks, _capture = self.run_capture_loop(encode_success=False)
        self.assertFalse(session.status()["frame_ready"])
        self.assertNotIn("ready", [event[0] for event in callbacks])
        self.assertIn("failed", [event[0] for event in callbacks])


class CameraFrameAgeReportingTests(unittest.TestCase):
    def test_status_reports_age_of_source_frame_without_loading_camera(self):
        session = CameraDetectionSession()
        session._latest_inference_frame_timestamp = 20.0
        with patch("camera_session.monotonic", return_value=20.125):
            self.assertEqual(session.status()["inference_frame_age_ms"], 125.0)

    def test_status_reports_unknown_age_until_a_source_timestamp_exists(self):
        session = CameraDetectionSession()
        self.assertIsNone(session.status()["inference_frame_age_ms"])


class CameraAutoCaptureTests(unittest.TestCase):
    def setUp(self):
        # Existing two-miss scenarios represent separated observations, not
        # instant calls. Keep real debounce enabled without sleeping in tests.
        self.clock = 100.0
        timer = patch("camera_session.monotonic", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        original = CameraDetectionSession._update_auto_capture

        def timed_observation(session, *args, **kwargs):
            self.clock += 0.7
            return original(session, *args, **kwargs)

        observations = patch.object(CameraDetectionSession, "_update_auto_capture", timed_observation)
        observations.start()
        self.addCleanup(observations.stop)

    def test_mixed_detections_keep_good_eggs_in_original_pending_order(self):
        session = CameraDetectionSession()
        session.capture_min_samples = 2
        for quality in ("good", "crack", "good", "rotten", "good"):
            for label in (quality, quality, "no egg", "no egg"):
                session._update_auto_capture(label, 0.9)
        self.assertEqual([session.wait_for_captured_quality(0)["capture_id"]
                          for _ in range(3)], [1, 3, 5])
        self.assertIsNone(session.wait_for_captured_quality(0))

    def test_loaded_good_egg_does_not_need_to_leave_camera_before_release(self):
        session = CameraDetectionSession()
        session._running = True
        session.capture_min_samples = 2
        session._update_auto_capture("good", 0.9)
        session._update_auto_capture("good", 0.9)
        capture = session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(capture["capture_id"], 1)
        self.assertEqual(capture["label"], "good")
        self.assertTrue(capture["claimed_on_scale"])
        self.assertEqual(session.status()["egg_count"], 0)
        for label in ("good", "good", "no egg", "no egg"):
            self.assertIsNone(session._update_auto_capture(label, 0.9))
        self.assertIsNone(session.wait_for_captured_quality(0, allow_loaded_capture=True))
        self.assertEqual(session.status()["egg_count"], 1)
        for label in ("good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.9)
        self.assertEqual(session.wait_for_captured_quality(0)["capture_id"], 2)

    def test_loaded_good_claim_requires_one_frame_even_when_samples_is_three(self):
        session = CameraDetectionSession()
        session.capture_min_samples = 3
        session._running = True
        session._update_auto_capture("good", 0.9)
        capture = session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(capture["label"], "good")
        self.assertTrue(capture["claimed_on_scale"])

    def test_crack_and_rotten_keep_configured_three_frame_confirmation(self):
        for defect in ("crack", "rotten"):
            with self.subTest(defect=defect):
                session = CameraDetectionSession()
                session.capture_min_samples = 3
                session._running = True
                for _ in range(2):
                    session._update_auto_capture(defect, 0.9)
                self.assertIsNone(session._passage_defect)
                session._update_auto_capture(defect, 0.9)
                self.assertEqual(session._passage_defect, defect)
                self.assertIsNone(
                    session.wait_for_captured_quality(0, allow_loaded_capture=True)
                )

    def test_loaded_good_claim_requires_uninterrupted_agreement_streak(self):
        session = CameraDetectionSession()
        session.capture_min_samples = 2
        session._running = True
        session._update_auto_capture("good", 0.9)
        session._update_auto_capture("no egg", 0.0, passage_occupied=True)
        session._update_auto_capture("good", 0.9)
        capture = session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(capture["label"], "good")
        self.assertTrue(capture["claimed_on_scale"])

    def test_queued_egg_keeps_priority_over_visible_loaded_capture(self):
        session = CameraDetectionSession()
        for label in ("good", "no egg", "no egg", "good", "good", "good"):
            session._update_auto_capture(label, 0.9)
        capture = session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertEqual(capture["capture_id"], 1)
        self.assertNotIn("claimed_on_scale", capture)

    def test_cancelled_scale_wait_cannot_consume_next_capture(self):
        session = CameraDetectionSession()
        for label in ("good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.9)
        self.assertIsNone(session.wait_for_captured_quality(0, is_current=lambda: False))
        self.assertEqual(session.wait_for_captured_quality(0)["capture_id"], 1)

    def test_expired_capture_is_not_silently_replaced_by_next_egg(self):
        session = CameraDetectionSession()
        session._captured_qualities.extend([
            {"capture_id": 1, "captured_at": 0},
            {"capture_id": 2, "captured_at": 100},
        ])
        with patch("camera_session.monotonic", return_value=100):
            with self.assertRaises(CameraSessionError):
                session.wait_for_captured_quality(0)
        self.assertEqual(session._captured_qualities[0]["capture_id"], 1)

    def test_single_defect_is_unresolved_and_does_not_reject(self):
        session = self.make_session()
        capture_handler = Mock()
        reject_handler = Mock()
        session.set_capture_handler(capture_handler)
        session.set_reject_handler(reject_handler)
        for label in ("crack", "no egg", "no egg"):
            session._process_auto_capture(label, 0.9)
        capture_handler.assert_called_once()
        self.assertEqual(capture_handler.call_args.args[0]["label"], "unknown")
        self.assertEqual(capture_handler.call_args.args[0]["quality"], "Not Classified")
        self.assertFalse(capture_handler.call_args.args[0]["quality_verified"])
        reject_handler.assert_not_called()
        self.assertFalse(session._captured_qualities)

    def test_stop_releases_camera_before_waiting_for_workers(self) -> None:
        session = CameraDetectionSession()
        capture = Mock()
        session._capture = capture

        session.stop()

        capture.release.assert_called_once_with()

    def test_single_rotten_can_be_overridden_only_by_confirmed_good(self) -> None:
        session = self.make_session()
        for label in ("rotten", "good", "good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.8)
        self.assertEqual([capture["label"] for capture in session._captured_qualities], ["good"])

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
        for label in ("Rotten", "Crack", "Good"):
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
        session._update_auto_capture("crack", 0.73)
        session._update_auto_capture("crack", 0.75)
        session._update_auto_capture("good", 0.89)
        session._update_auto_capture("good", 0.92)
        session._update_auto_capture("no egg", 0.0)
        session._update_auto_capture("no egg", 0.0)

        self.assertFalse(session._captured_qualities)

    def test_single_good_observation_is_queued_after_confirmed_exit(self) -> None:
        session = self.make_session()
        session._update_auto_capture("good", 0.8)
        session._update_auto_capture("no egg", 0.0)
        session._update_auto_capture("no egg", 0.0)

        self.assertEqual(len(session._captured_qualities), 1)
        self.assertEqual(session._captured_qualities[0]["label"], "good")

    def test_good_capture_waits_for_zone_exit_before_weighing(self) -> None:
        session = CameraDetectionSession()
        session._running = True
        session._update_auto_capture("good", 0.88)
        self.assertEqual(session.capture_min_samples, 1)
        self.assertIsNone(session.wait_for_captured_quality(timeout=0.0))
        session._update_auto_capture("no egg", 0.0)
        self.assertIsNone(session.wait_for_captured_quality(timeout=0.0))
        session._update_auto_capture("no egg", 0.0)
        capture = session.wait_for_captured_quality(timeout=0.0)
        self.assertEqual(capture["capture_id"], 1)
        self.assertEqual(capture["label"], "good")
        self.assertEqual(session.status()["egg_count"], 1)
        self.assertIsNone(session.wait_for_captured_quality(timeout=0.0))

    def test_confirmed_defect_latch_blocks_scale_measurement(self) -> None:
        for defect in ("crack", "rotten"):
            with self.subTest(defect=defect):
                session = self.make_session()
                session._running = True
                for label in (defect, defect, defect, "good", "good", "good", "no egg", "no egg"):
                    session._update_auto_capture(label, 0.9)
                self.assertIsNone(session.wait_for_captured_quality(timeout=0.0))
                self.assertEqual(session.status()["egg_count"], 1)
                self.assertEqual(session.status()["last_capture"]["label"], defect)

    def test_completed_camera_capture_survives_scale_inspection_reset(self) -> None:
        session = self.make_session()
        for label in ("good", "good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.86)

        session.begin_egg_inspection()
        capture = session.wait_for_captured_quality(timeout=0.0)
        self.assertEqual(capture["capture_id"], 1)
        self.assertEqual(capture["label"], "good")
        self.assertEqual(capture["confidence"], 0.86)

    def test_separate_eggs_are_queued_in_conveyor_order(self) -> None:
        session = self.make_session()
        session.capture_min_samples = 2
        for label in ("good", "good", "no egg", "no egg"):
            session._update_auto_capture(label, 0.8)
        for label in ("crack", "crack", "no egg", "no egg"):
            session._update_auto_capture(label, 0.7)
        self.assertEqual(
            [result["label"] for result in session._captured_qualities],
            ["good"],
        )
        self.assertEqual(
            [result["capture_id"] for result in session._captured_qualities],
            [1],
        )
        self.assertEqual(session.status()["egg_count"], 2)

    def test_one_shot_count_and_capture_event_on_exit(self) -> None:
        session = CameraDetectionSession()
        handler = Mock()
        session.set_capture_handler(handler)
        for label in ("no egg", "good", "good", "good", "no egg"):
            session._process_auto_capture(label, 0.8)
        handler.assert_not_called()
        self.assertEqual(session.status()["egg_count"], 0)
        session._process_auto_capture("no egg", 0.0)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0]["capture_id"], 1)
        self.assertEqual(handler.call_args.args[0]["label"], "good")
        for _ in range(5):
            session._process_auto_capture("no egg", 0.0)
        handler.assert_called_once()
        self.assertEqual(session.status()["quality_counts"]["good"], 1)

    def test_each_defect_sends_only_after_confirmation_once_per_passage(self) -> None:
        for defect in ("crack", "rotten"):
            with self.subTest(defect=defect):
                session = self.make_session()
                handler = Mock()
                session.set_reject_handler(handler)
                session._process_auto_capture(defect, 0.8)
                handler.assert_not_called()
                session._process_auto_capture(defect, 0.8)
                handler.assert_not_called()
                session._process_auto_capture(defect, 0.8)
                handler.assert_called_once_with(defect)
                for label in (defect, "good", "rotten", "no egg", "no egg"):
                    session._process_auto_capture(label, 0.9)
                handler.assert_called_once_with(defect)
                self.assertFalse(session._captured_qualities)
                for _ in range(3):
                    session._process_auto_capture(defect, 0.8)
                self.assertEqual(handler.call_count, 2)

    def test_failed_send_retries_while_egg_is_visible(self) -> None:
        session = self.make_session()
        handler = Mock(side_effect=[RuntimeError("Disconnected"), None])
        session.set_reject_handler(handler)
        session._process_auto_capture("rotten", 0.8)
        session._process_auto_capture("rotten", 0.8)
        self.assertEqual(handler.call_count, 0)
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
        for label in ("crack", "crack", "crack", "no egg", "no egg"):
            session._process_auto_capture(label, 0.8)
        attempts = handler.call_count
        handler.side_effect = None
        session._process_auto_capture("good", 0.8)
        self.assertEqual(handler.call_count, attempts)
        self.assertFalse(session._captured_qualities)

    def test_inference_reject_requires_two_fresh_results_without_hardware(self) -> None:
        for defect in ("Crack", "Rotten"):
            with self.subTest(defect=defect):
                session = self.make_session()
                session.capture_min_samples = 2
                reject = Mock(side_effect=lambda _label: session._stop_event.set())
                session.set_reject_handler(reject)
                frame = Mock()
                frame.shape = (100, 100, 3)
                frame.copy.return_value = frame
                session._latest_raw_frame = frame
                session._raw_sequence = 1

                class FeedTwoFrames:
                    def __init__(self):
                        self.turn = 0

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def wait_for(self, predicate, timeout=None):
                        self.turn += 1
                        if self.turn == 2:
                            session._raw_sequence = 2
                        return predicate()

                def detect(_frame):
                    return {"detections": [{
                        "label": defect, "confidence": 0.9,
                        "box": [40, 40, 60, 60],
                    }]}

                session._raw_frame_ready = FeedTwoFrames()
                with patch("camera_session.detect_image", side_effect=detect):
                    session._inference_loop()
                reject.assert_called_once_with(defect.lower())
                self.assertFalse(session._captured_qualities)

    def test_only_good_enters_the_weighing_queue(self) -> None:
        session = self.make_session()
        handler = Mock()
        session.set_reject_handler(handler)
        for label in ("good", "good", "good", "no egg", "no egg"):
            session._process_auto_capture(label, 0.8)
        handler.assert_not_called()
        self.assertEqual(
            [entry["label"] for entry in session._captured_qualities],
            ["good"],
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

    def test_outside_zone_defect_is_ignored_as_no_egg(self) -> None:
        detections = [{
            "label": "Crack", "confidence": 0.99, "box": [0, 0, 20, 20],
        }]

        self.assertEqual(
            CameraDetectionSession._select_frame_observation(
                detections, (30, 30, 70, 70)
            ),
            ("no egg", 0.0),
        )

    def test_partly_entered_egg_does_not_trigger_capture(self) -> None:
        detections = [{
            "label": "Rotten", "confidence": 0.99, "box": [0, 40, 20, 60],
        }]

        self.assertEqual(
            CameraDetectionSession._select_frame_observation(
                detections, (30, 30, 70, 70)
            ),
            ("no egg", 0.0),
        )

    def test_large_centered_egg_is_accepted(self) -> None:
        detections = [{
            "label": "Good", "confidence": 0.97, "box": [10, 20, 90, 80],
        }]

        self.assertEqual(
            CameraDetectionSession._select_frame_observation(
                detections, (30, 30, 70, 70)
            ),
            ("good", 0.97),
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


class QualityConfirmationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.clock = 200.0
        timer = patch("camera_session.monotonic", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        original = CameraDetectionSession._update_auto_capture

        def timed_observation(session, *args, **kwargs):
            self.clock += 0.7
            return original(session, *args, **kwargs)

        observation_patch = patch.object(
            CameraDetectionSession, "_update_auto_capture", timed_observation
        )
        observation_patch.start()
        self.addCleanup(observation_patch.stop)

    def make_session(self):
        session = CameraDetectionSession()
        session.capture_min_samples = 2
        return session

    @staticmethod
    def feed(session, labels, start=1):
        for offset, label in enumerate(labels):
            session._process_auto_capture(
                label, 0.9, observation_sequence=start + offset
            )

    def test_good_good_confirms_one_good_capture(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["good", "good", "no egg", "no egg"])
        reject.assert_not_called()
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[0]["label"], "good")
        self.assertEqual([item["label"] for item in session._captured_qualities], ["good"])

    def test_crack_crack_confirms_then_rejects(self):
        session = self.make_session()
        reject = Mock()
        session.set_reject_handler(reject)
        self.feed(session, ["crack"])
        reject.assert_not_called()
        self.feed(session, ["crack", "no egg", "no egg"], start=2)
        reject.assert_called_once_with("crack")
        self.assertEqual(session.status()["last_capture"]["label"], "crack")

    def test_rotten_rotten_confirms_then_rejects(self):
        session = self.make_session()
        reject = Mock()
        session.set_reject_handler(reject)
        self.feed(session, ["rotten"])
        reject.assert_not_called()
        self.feed(session, ["rotten"], start=2)
        reject.assert_called_once_with("rotten")

    def test_single_unconfirmed_crack_does_not_block_single_good(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["crack", "good", "no egg", "no egg"])
        reject.assert_not_called()
        self.assertEqual(len(session._captured_qualities), 1)
        self.assertEqual(session._captured_qualities[0]["label"], "good")
        self.assertEqual(capture.call_args.args[0]["label"], "good")

    def test_good_crack_good_keeps_good_with_one_frame_threshold(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["good", "crack", "good", "no egg", "no egg"])
        reject.assert_not_called()
        self.assertEqual(len(session._captured_qualities), 1)
        self.assertEqual(session._captured_qualities[0]["label"], "good")
        self.assertEqual(capture.call_args.args[0]["label"], "good")

    def test_exit_before_confirmation_is_not_classified_or_queued(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["crack", "no egg", "no egg"])
        reject.assert_not_called()
        capture.assert_called_once()
        unresolved = capture.call_args.args[0]
        self.assertEqual(unresolved["quality"], "Not Classified")
        self.assertFalse(unresolved["quality_verified"])
        self.assertFalse(session._captured_qualities)
        self.assertEqual(session.status()["egg_count"], 1)

    def test_duplicate_and_delayed_inference_sequences_count_once(self):
        session = self.make_session()
        reject = Mock()
        session.set_reject_handler(reject)
        session._process_auto_capture("crack", 0.9, observation_sequence=10)
        session._process_auto_capture("crack", 0.9, observation_sequence=10)
        session._process_auto_capture("crack", 0.9, observation_sequence=9)
        reject.assert_not_called()
        session._process_auto_capture("crack", 0.9, observation_sequence=11)
        reject.assert_called_once_with("crack")

    def test_single_good_sample_is_verified_after_confirmed_passage_exit(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["good", "no egg", "no egg"])
        reject.assert_not_called()
        self.assertEqual(len(session._captured_qualities), 1)
        self.assertEqual(session._captured_qualities[0]["label"], "good")
        self.assertEqual(capture.call_args.args[0]["label"], "good")

    def test_sequential_passages_keep_distinct_capture_ids(self):
        session = self.make_session()
        reject = Mock()
        capture = Mock()
        session.set_reject_handler(reject)
        session.set_capture_handler(capture)
        self.feed(session, ["good", "good", "no egg", "no egg",
                           "crack", "crack", "no egg", "no egg"])
        self.assertEqual(capture.call_count, 2)
        self.assertEqual([call.args[0]["capture_id"] for call in capture.call_args_list], [1, 2])
        self.assertEqual([item["capture_id"] for item in session._captured_qualities], [1])
        self.assertEqual(session.status()["egg_count"], 2)
        reject.assert_called_once_with("crack")


class ZoneSettingsRegressionTests(unittest.TestCase):
    def test_lower_zone_keeps_outside_frames_out_and_locks_inside_passage(self):
        zone_settings = {
            "CAMERA_INSPECTION_LEFT": "0.25",
            "CAMERA_INSPECTION_RIGHT": "0.75",
            "CAMERA_INSPECTION_TOP": "0.35",
            "CAMERA_INSPECTION_BOTTOM": "0.85",
            "CAMERA_QUALITY_SAMPLES": "2",
        }
        with patch.dict("os.environ", zone_settings):
            session = CameraDetectionSession()

        self.assertEqual(
            (session.inspection_left, session.inspection_right,
             session.inspection_top, session.inspection_bottom,
             session.capture_min_samples),
            (0.25, 0.75, 0.35, 0.85, 2),
        )
        session.passage_exit_seconds = 0.0
        session.set_capture_handler(Mock())
        width = height = 1000
        zone = (
            width * session.inspection_left,
            height * session.inspection_top,
            width * session.inspection_right,
            height * session.inspection_bottom,
        )
        outside = [{"label": "Crack", "confidence": 0.99, "box": [490, 290, 510, 330]}]
        first_inside = [{"label": "Good", "confidence": 0.91, "box": [480, 480, 520, 520]}]
        second_inside = [{"label": "Good", "confidence": 0.94, "box": [480, 580, 520, 620]}]

        label, confidence = session._select_frame_observation(outside, zone)
        self.assertEqual(label, "no egg")
        session._process_auto_capture(label, confidence, observation_sequence=1)
        self.assertIsNone(session._active_capture)

        label, confidence = session._select_frame_observation(first_inside, zone)
        session._process_auto_capture(label, confidence, observation_sequence=2)
        self.assertEqual(session._active_capture["label"], "good")
        label, confidence = session._select_frame_observation(second_inside, zone)
        session._process_auto_capture(label, confidence, observation_sequence=3)
        self.assertEqual(session._active_capture["label"], "good")

        session._process_auto_capture("no egg", 0.0, observation_sequence=4)
        session._process_auto_capture("no egg", 0.0, observation_sequence=5)
        self.assertEqual(session.status()["egg_count"], 1)
        capture = session.wait_for_captured_quality(timeout=0.0)
        self.assertEqual(capture["label"], "good")


if __name__ == "__main__":
    unittest.main()
