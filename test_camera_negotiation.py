"""No device access: property ordering, truthfulness, and rollback tests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from camera_negotiation import configure_camera, verify_frame
from camera_session import CameraDetectionSession


class NegotiationTests(unittest.TestCase):
    def setup_camera(self, corrected=True, backend="dshow"):
        cv2, capture = MagicMock(), MagicMock()
        capture.get.side_effect = [1280, 720, 30, 1196444237]
        report = configure_camera(capture, cv2, backend=backend, width=1280,
                                  height=720, fps=30, corrected=corrected)
        return capture, cv2, report

    def test_corrected_order_finishes_with_mjpg_and_preserves_values(self):
        capture, cv2, report = self.setup_camera()
        self.assertEqual(capture.set.call_args_list, [
            call(cv2.CAP_PROP_FPS, 30), call(cv2.CAP_PROP_FRAME_WIDTH, 1280),
            call(cv2.CAP_PROP_FRAME_HEIGHT, 720),
            call(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc.return_value),
            call(cv2.CAP_PROP_BUFFERSIZE, 1)])
        self.assertEqual(report["fourcc"], "MJPG")
        self.assertFalse(report["resolution_verified"])
        self.assertTrue(verify_frame(report, SimpleNamespace(shape=(720, 1280, 3)))["requested_resolution_matches_frame"])

    def test_legacy_order_is_exact_rollback(self):
        capture, cv2, report = self.setup_camera(False)
        self.assertEqual(capture.set.call_args_list, [
            call(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc.return_value),
            call(cv2.CAP_PROP_FRAME_WIDTH, 1280), call(cv2.CAP_PROP_FRAME_HEIGHT, 720),
            call(cv2.CAP_PROP_FPS, 30), call(cv2.CAP_PROP_BUFFERSIZE, 1)])
        self.assertEqual(report["mode"], "legacy")

    def test_other_backends_keep_existing_order(self):
        for backend in ("auto", "msmf"):
            self.assertEqual(self.setup_camera(backend=backend)[2]["mode"], "legacy")

    def test_true_set_and_matching_readback_do_not_prove_frame_matches(self):
        _, _, report = self.setup_camera()
        verified = verify_frame(report, SimpleNamespace(shape=(1440, 2560, 3)))
        self.assertFalse(verified["requested_resolution_matches_frame"])
        self.assertTrue(verified["resolution_verified"])
        self.assertEqual(verified["frame_width"], 2560)
        self.assertEqual(report["width"], 1280)

    def test_false_property_results_and_unavailable_readback_are_recorded(self):
        capture, cv2 = MagicMock(), MagicMock()
        capture.set.return_value = False
        capture.get.side_effect = RuntimeError("not available")
        capture.getBackendName.side_effect = RuntimeError("not available")
        report = configure_camera(capture, cv2, backend="dshow", width=1280, height=720, fps=30)
        self.assertFalse(any(report["set_accepted"].values()))
        self.assertIsNone(report["fourcc"])
        self.assertIsNone(report["width"])
        json.loads(json.dumps(report, allow_nan=False))

    def run_capture(self, dimensions):
        session = CameraDetectionSession()
        session.camera_backend = "dshow"
        cv2 = MagicMock()
        capture = cv2.VideoCapture.return_value
        capture.get.side_effect = [1280, 720, 30, 1196444237]
        frame = SimpleNamespace(shape=(*dimensions, 3))
        capture.read.return_value = (True, frame)
        cv2.imencode.side_effect = lambda *a: (session._stop_event.set() or True,
                                               SimpleNamespace(tobytes=lambda: b"jpeg"))
        session._lifecycle_handler = MagicMock()
        with patch.dict("sys.modules", {"cv2": cv2}), patch("camera_session._DIAGNOSTIC_LOG") as log:
            session._capture_loop()
        return session, cv2, log, frame

    def test_successful_capture_logs_verification_and_publishes_original_frame(self):
        session, cv2, log, frame = self.run_capture((720, 1280))
        self.assertTrue(session.status()["camera_negotiation"]["requested_resolution_matches_frame"])
        log.info.assert_called_once()
        log.warning.assert_not_called()
        self.assertIs(session._latest_raw_frame, frame)
        cv2.resize.assert_not_called()
        session._lifecycle_handler.assert_called_once()

    def test_unsuccessful_capture_warns_and_does_not_claim_resolution_success(self):
        session, cv2, log, frame = self.run_capture((1440, 2560))
        self.assertFalse(session.status()["camera_negotiation"]["requested_resolution_matches_frame"])
        log.warning.assert_called_once()
        log.info.assert_not_called()
        self.assertIs(session._latest_raw_frame, frame)
        cv2.resize.assert_not_called()
        self.assertIsNone(session._error)  # Preserve working capture, disclose mismatch.

    def test_rollback_environment_flag(self):
        with patch.dict("os.environ", {"CAMERA_RESOLUTION_NEGOTIATION_FIX": "false"}):
            self.assertFalse(CameraDetectionSession().camera_resolution_negotiation_fix)

    def test_native_profile_uses_exact_four_setters(self):
        capture, cv2 = MagicMock(), MagicMock()
        capture.get.side_effect = [1920, 1080, 30, 844715353]
        report = configure_camera(capture, cv2, backend="dshow", width=1920, height=1080,
                                  fps=30, profile="1080p_yuy2")
        cv2.VideoWriter_fourcc.assert_called_once_with(*"YUY2")
        self.assertEqual(capture.set.call_args_list, [call(cv2.CAP_PROP_FPS, 30),
            call(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc.return_value),
            call(cv2.CAP_PROP_FRAME_WIDTH, 1920), call(cv2.CAP_PROP_FRAME_HEIGHT, 1080)])
        self.assertEqual(report["fourcc"], "YUY2")

    def test_profile_selection_and_existing_rollback(self):
        with patch.dict("os.environ", {"CAMERA_CAPTURE_PROFILE": "1080p_yuy2",
                                       "CAMERA_BACKEND": "msmf", "CAMERA_WIDTH": "1280"}):
            session = CameraDetectionSession()
            self.assertEqual((session.camera_backend, session.camera_width, session.camera_height,
                              session.camera_fps), ("dshow", 1920, 1080, 30))
        with patch.dict("os.environ", {"CAMERA_CAPTURE_PROFILE": "existing",
                                       "CAMERA_BACKEND": "dshow", "CAMERA_WIDTH": "1280", "CAMERA_HEIGHT": "720"}):
            session = CameraDetectionSession()
            self.assertEqual((session.camera_width, session.camera_height), (1280, 720))

    def native_capture(self, shape, actual_fourcc):
        with patch.dict("os.environ", {"CAMERA_CAPTURE_PROFILE": "1080p_yuy2"}):
            session = CameraDetectionSession()
        cv2 = MagicMock()
        cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT = 3, 4
        cv2.CAP_PROP_FPS, cv2.CAP_PROP_FOURCC = 5, 6
        capture = cv2.VideoCapture.return_value
        capture.get.side_effect = lambda p: {3: 1920, 4: 1080, 5: 30,
            6: sum(ord(c) << (8*i) for i,c in enumerate(actual_fourcc))}[p]
        frame = SimpleNamespace(shape=(*shape, 3))
        capture.read.return_value = True, frame
        cv2.imencode.side_effect = lambda *a: (session._stop_event.set() or True,
                                               SimpleNamespace(tobytes=lambda: b"jpeg"))
        session._lifecycle_handler = MagicMock()
        with patch.dict("sys.modules", {"cv2": cv2}), patch("camera_session._DIAGNOSTIC_LOG") as log:
            session._capture_loop()
        return session, cv2, log, frame

    def test_native_post_capture_verification_and_no_resize(self):
        session, cv2, log, frame = self.native_capture((1080, 1920), "YUY2")
        self.assertTrue(session.status()["camera_negotiation"]["mode_verified"])
        self.assertIs(session._latest_raw_frame, frame)
        self.assertIsNone(session._error)
        cv2.resize.assert_not_called()
        log.warning.assert_not_called()
        self.assertEqual(session._lifecycle_handler.call_args.args[0], "ready")

    def test_native_mismatch_stops_before_ready_or_inference_handoff(self):
        for shape, fourcc in (((1440, 2560), "YUY2"), ((1080, 1920), "MJPG"), ((1080,1920), "")):
            with self.subTest(shape=shape, fourcc=fourcc):
                session, cv2, log, _ = self.native_capture(shape, fourcc)
                self.assertFalse(session.status()["camera_negotiation"]["mode_verified"])
                self.assertIn("failed verification", session._error)
                self.assertIsNone(session._latest_raw_frame)
                self.assertEqual(session._lifecycle_handler.call_args.args[0], "failed")
                cv2.imencode.assert_not_called()
                log.warning.assert_called_once()

    def test_post_frame_fourcc_replaces_stale_open_readback(self):
        report = {"requested": {"width": 1920, "height": 1080, "fps": 30, "fourcc": "YUY2"},
                  "fourcc": "YUY2"}
        capture, cv2 = MagicMock(), MagicMock()
        capture.get.side_effect = [1920, 1080, 30, 1196444237]  # MJPG after read
        checked = verify_frame(report, SimpleNamespace(shape=(1080,1920,3)), capture, cv2)
        self.assertEqual(checked["fourcc"], "MJPG")
        self.assertFalse(checked["mode_verified"])

    def test_same_size_format_drift_is_rechecked_before_handoff(self):
        import itertools
        with patch.dict("os.environ", {"CAMERA_CAPTURE_PROFILE": "1080p_yuy2"}):
            session = CameraDetectionSession()
        cv2 = MagicMock()
        cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT = 3, 4
        cv2.CAP_PROP_FPS, cv2.CAP_PROP_FOURCC = 5, 6
        capture = cv2.VideoCapture.return_value
        def get(prop):
            if prop != 6:
                return {3: 1920, 4: 1080, 5: 30}[prop]
            value = "MJPG" if capture.read.call_count >= 2 else "YUY2"
            return sum(ord(c) << (8*i) for i,c in enumerate(value))
        capture.get.side_effect = get
        capture.read.return_value = True, SimpleNamespace(shape=(1080,1920,3))
        cv2.imencode.return_value = True, SimpleNamespace(tobytes=lambda: b"jpeg")
        with patch.dict("sys.modules", {"cv2": cv2}), patch("camera_session.monotonic", side_effect=itertools.count(0, 10)), patch("camera_session._DIAGNOSTIC_LOG"):
            session._capture_loop()
        self.assertEqual(capture.read.call_count, 2)
        self.assertEqual(session._raw_sequence, 1)
        self.assertIn("failed verification", session._error)
        self.assertFalse(session.status()["camera_negotiation"]["mode_verified"])


if __name__ == "__main__":
    unittest.main()
