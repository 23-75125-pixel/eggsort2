"""Hardware-free tests: no application import, camera, model load, or serial I/O."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from camera_performance import CameraPerformance, camera_readback
from camera_session import CameraDetectionSession
import detection_service


class PerformanceTests(unittest.TestCase):
    def test_disabled_creates_no_thread_or_file(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = CameraPerformance(directory=folder)
            monitor.start(lambda: self.fail("disabled diagnostics sampled"))
            self.assertIsNone(monitor._thread)
            self.assertEqual(list(Path(folder).iterdir()), [])
        with patch.dict(os.environ, {"CAMERA_PERFORMANCE_DIAGNOSTICS": "false"}):
            self.assertFalse(CameraDetectionSession()._performance.enabled)

    def test_bounded_aggregates_and_actual_window_throughput(self):
        monitor = CameraPerformance(True)
        monitor._window = monitor._started = 100
        for _ in range(10000):
            monitor.record({"read_ms": 2}, reads=1)
        self.assertEqual(len(monitor._metrics), 1)
        with patch("camera_performance.monotonic", return_value=110):
            summary = monitor.summary()
        self.assertEqual(summary["rates_per_s"]["reads"], 1000)
        self.assertEqual(summary["timings_ms"]["read_ms"], {"samples": 10000, "mean": 2, "max": 2})
        self.assertEqual(monitor._metrics, {})
        self.assertEqual(summary["rates_per_s"]["predictions"], 0)

    def test_stall_visible_before_call_returns(self):
        monitor = CameraPerformance(True)
        with patch("camera_performance.monotonic", return_value=100):
            monitor.stage("inference", "detect_image_including_lock_wait")
        with patch("camera_performance.monotonic", return_value=109):
            summary = monitor.summary()
        self.assertEqual(summary["workers"]["inference"]["stage_age_ms"], 9000)
        self.assertEqual(summary["counts"]["predictions"], 0)

    def test_background_jsonl_and_stop_flush(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = CameraPerformance(True, directory=folder)
            monitor.start(lambda: {"capture_alive": False, "inference_alive": False})
            monitor.record({"prediction_ms": 123}, predictions=1)
            monitor.stop()
            monitor._thread.join(2)
            self.assertFalse(monitor._thread.is_alive())
            records = [json.loads(line) for line in monitor.path.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["counts"]["predictions"], 1)
            self.assertNotIn("session_ref", records[0])

    def test_failed_export_does_not_raise_on_producer(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "file"
            target.write_text("not a directory")
            monitor = CameraPerformance(True, directory=target)
            with self.assertLogs("camera_performance", level="WARNING"):
                monitor.start(lambda: {})
                monitor._thread.join(2)
            monitor.record({"prediction_ms": 1}, predictions=1)
            monitor.stop()

    def test_writer_never_holds_camera_or_inference_lock(self):
        session = CameraDetectionSession()
        with tempfile.TemporaryDirectory() as folder:
            monitor = CameraPerformance(True, directory=folder)
            def gauges():
                self.assertFalse(session._lock._is_owned())
                self.assertFalse(detection_service._inference_lock.locked())
                # The worker has already released the telemetry lock too.
                self.assertTrue(monitor._lock.acquire(blocking=False))
                monitor._lock.release()
                return {"capture_alive": False, "inference_alive": False}
            monitor.start(gauges)
            monitor.stop()
            monitor._thread.join(2)
            self.assertTrue(monitor.path.is_file())

    def test_separate_runs_keep_separate_files_and_accumulators(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for count in (1, 2):
                monitor = CameraPerformance(True, directory=folder)
                monitor.start(lambda: {"capture_alive": False, "inference_alive": False})
                monitor.record(predictions=count)
                monitor.stop()
                monitor._thread.join(2)
                paths.append(monitor.path)
                self.assertEqual(json.loads(monitor.path.read_text())["counts"]["predictions"], count)
            self.assertNotEqual(*paths)

    def test_readback_errors_are_advisory(self):
        capture, cv2 = MagicMock(), MagicMock()
        capture.get.side_effect = [2560, 1440, float("nan")]
        result = camera_readback(capture, cv2, {"width": 1280, "height": 720, "fps": 30})
        self.assertEqual(result["width"], 2560)
        self.assertIsNone(result["fps"])
        capture.get.side_effect = RuntimeError("unsupported")
        self.assertIsNone(camera_readback(capture, cv2, {})["width"])

    def test_prediction_timings_separate_lock_wait_and_keep_kwargs(self):
        frame = SimpleNamespace(shape=(720, 1280, 3))
        model = MagicMock()
        model.predict.return_value = [SimpleNamespace(names={}, boxes=None)]
        timings = {}
        with patch.object(detection_service, "_load_model", return_value=model), patch.object(
            detection_service, "monotonic", side_effect=[10, 10.1, 10.3, 10.8, 11]
        ):
            result = detection_service.detect_image(frame, performance=timings)
        self.assertAlmostEqual(timings["inference_lock_wait_ms"], 200)
        self.assertAlmostEqual(timings["prediction_ms"], 500)
        self.assertAlmostEqual(timings["detection_total_ms"], 1000)
        self.assertEqual(result["detections"], [])
        model.predict.assert_called_once_with(source=frame, conf=detection_service.CONFIDENCE,
                                              imgsz=detection_service.IMAGE_SIZE, verbose=False)

    def test_disabled_prediction_does_not_call_diagnostic_clock(self):
        model = MagicMock()
        model.predict.return_value = [SimpleNamespace(names={}, boxes=None)]
        with patch.object(detection_service, "_load_model", return_value=model), patch.object(
            detection_service, "monotonic", side_effect=AssertionError("diagnostic timing disabled")
        ):
            detection_service.detect_image(SimpleNamespace(shape=(720, 1280, 3)))

    def test_capture_metrics_use_real_shape_without_changing_settings(self):
        session = CameraDetectionSession()
        session._performance = CameraPerformance(True)
        cv2 = MagicMock()
        capture = cv2.VideoCapture.return_value
        capture.read.return_value = (True, SimpleNamespace(shape=(1440, 2560, 3)))
        capture.get.side_effect = [2560, 1440, 30]
        def encode(*args):
            session._stop_event.set()
            return True, SimpleNamespace(tobytes=lambda: b"jpeg")
        cv2.imencode.side_effect = encode
        with patch.dict("sys.modules", {"cv2": cv2}):
            session._capture_loop()
        summary = session._performance.summary()
        self.assertFalse(summary["camera"]["requested_resolution_matches_frame"])
        self.assertEqual(summary["counts"]["jpegs"], 1)
        for name in ("read_ms", "annotation_ms", "jpeg_encode_ms"):
            self.assertIn(name, summary["timings_ms"])
        self.assertEqual(capture.set.call_count, 5)
        self.assertIsNotNone(session._latest_raw_returned_at)

    def test_result_age_callbacks_and_latest_only_skip_counts(self):
        session = CameraDetectionSession()
        session._performance = CameraPerformance(True)
        session._latest_raw_frame = MagicMock(shape=(720, 1280, 3))
        session._latest_raw_frame.copy.return_value = session._latest_raw_frame
        session._raw_sequence = 3
        session._latest_raw_returned_at = monotonic() - 0.5
        result = {"image_width": 1280, "image_height": 720, "detections": []}
        def detect(frame, *, performance):
            performance.update(prediction_ms=1, inference_lock_wait_ms=0)
            return result
        with patch("camera_session.detect_image", side_effect=detect), patch.object(
            session, "_process_auto_capture", side_effect=lambda *a, **kw: session._stop_event.set()
        ) as process:
            session._inference_loop()
        summary = session._performance.summary()
        self.assertEqual(summary["counts"]["skipped_raw_frames"], 2)
        self.assertEqual(summary["counts"]["inference_cycles"], 1)
        self.assertGreaterEqual(summary["timings_ms"]["frame_age_at_result_ms"]["mean"], 500)
        self.assertIn("passage_and_callbacks_ms", summary["timings_ms"])
        process.assert_called_once()
        self.assertEqual(session._egg_count, 0)


if __name__ == "__main__":
    unittest.main()
