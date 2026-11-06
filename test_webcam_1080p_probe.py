"""Mocked webcam-only tests. No hardware, app or model startup."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import webcam_1080p_probe as probe


class ProbeTests(unittest.TestCase):
    def camera(self, *, shape=(1080, 1920, 3), fourcc="MJPG"):
        cv2 = MagicMock()
        cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT = 3, 4
        cv2.CAP_PROP_FPS, cv2.CAP_PROP_FOURCC = 5, 6
        cap = cv2.VideoCapture.return_value
        code = sum(ord(c) << (8*i) for i, c in enumerate(fourcc))
        cap.get.side_effect = lambda prop: {3: shape[1], 4: shape[0], 5: 30, 6: code}[prop]
        cap.read.return_value = (True, SimpleNamespace(shape=shape))
        return cv2, cap

    def run_attempt(self, cv2, step=1/30):
        value = [-step]
        def clock():
            value[0] += step
            return value[0]
        return probe.attempt(cv2, index=0, backend="dshow", fourcc="MJPG",
                             order=("fps", "fourcc", "width", "height"),
                             seconds=5, warmup=0, clock=clock)

    def test_matching_dimensions_format_and_measured_fps(self):
        cv2, cap = self.camera()
        row, sample = self.run_attempt(cv2)
        self.assertEqual(row["status"], "PASS")
        self.assertAlmostEqual(row["delivered_fps"], 30)
        self.assertIsNotNone(sample)
        self.assertEqual(len(row["snapshots"]), 5)
        cap.release.assert_called_once()
        cv2.resize.assert_not_called()

    def test_1440p_fallback_rejected_immediately(self):
        cv2, cap = self.camera(shape=(1440, 2560, 3))
        row, sample = self.run_attempt(cv2)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("Resolution mismatch", row["reason"])
        self.assertIsNone(sample)
        cap.read.assert_called_once()
        cap.release.assert_called_once()

    def test_format_fallback_rejected(self):
        cv2, cap = self.camera(fourcc="YUY2")
        row, sample = self.run_attempt(cv2)
        self.assertIn("FourCC mismatch", row["reason"])
        self.assertEqual(row["status"], "FAILED")
        self.assertIsNone(sample)

    def test_fps_is_measured_not_taken_from_readback(self):
        cv2, _ = self.camera()
        row, _ = self.run_attempt(cv2, step=0.1)
        self.assertEqual(row["after_read"]["fps"], 30)
        self.assertAlmostEqual(row["delivered_fps"], 10)
        self.assertEqual(row["status"], "FPS_MISMATCH")

    def test_read_failure_releases_device(self):
        cv2, cap = self.camera()
        cap.read.return_value = False, None
        row, sample = self.run_attempt(cv2)
        self.assertIn("read failed", row["reason"])
        cap.release.assert_called_once()

    def test_open_failure_releases_device(self):
        cv2, cap = self.camera()
        cap.isOpened.return_value = False
        row, _ = self.run_attempt(cv2)
        self.assertEqual(row["reason"], "Device did not open")
        cap.release.assert_called_once()
        cap.read.assert_not_called()

    def test_cli_mjpg_priority_and_json_report_without_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            failed = {"status": "FAILED", "reason": "mock unsupported mode"}
            passed = {"status": "PASS", "reason": "mock measured success"}
            with patch.dict("sys.modules", {"cv2": MagicMock(__version__="TEST")}), patch.object(
                probe, "attempt", side_effect=[(failed.copy(), None), (passed.copy(), None)]
            ) as attempt, patch("builtins.print"):
                self.assertEqual(probe.main(["--output", folder]), 0)
            self.assertEqual([c.kwargs["fourcc"] for c in attempt.call_args_list], ["MJPG", "MJPG"])
            data = json.loads((Path(folder) / "report.json").read_text())
            self.assertEqual(len(data["attempts"]), 2)
            self.assertIn("NOT complete", data["scope"])

    def test_exception_and_interrupt_still_release_camera(self):
        for error in (RuntimeError("driver failure"), KeyboardInterrupt()):
            cv2, cap = self.camera()
            cap.read.side_effect = error
            if isinstance(error, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_attempt(cv2)
            else:
                row, _ = self.run_attempt(cv2)
                self.assertEqual(row["status"], "FAILED")
            cap.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
