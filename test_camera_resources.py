"""Mock OS counters, YOLO results, torch getters; never start model or hardware."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from camera_resources import ResourceSampler
from camera_performance import CameraPerformance
from detection_service import _performance_details


class ResourceTests(unittest.TestCase):
    def fake(self):
        psutil = MagicMock()
        psutil.cpu_count.return_value = 16
        psutil.cpu_percent.return_value = 75
        psutil.Process.return_value.cpu_percent.return_value = 800
        psutil.Process.return_value.num_threads.return_value = 32
        psutil.Process.return_value.memory_info.return_value = SimpleNamespace(private=1000, rss=2000, num_page_faults=100)
        psutil.virtual_memory.return_value = SimpleNamespace(available=4000, percent=60)
        psutil.swap_memory.return_value = SimpleNamespace(used=300, percent=10, sin=123, sout=456)
        psutil.cpu_freq.return_value = SimpleNamespace(current=1800, max=4300)
        return psutil

    def test_counter_scaling_private_memory_and_windows_limits(self):
        psutil = self.fake()
        with patch.dict("sys.modules", {"psutil": psutil}), patch("camera_resources.sys.platform", "win32"):
            sampler = ResourceSampler()
            a = sampler.sample()
            self.assertEqual(a["process_cpu_percent_one_core"], 800)
            self.assertEqual(a["process_cpu_percent_machine"], 50)
            self.assertEqual(a["system_cpu_percent"], 75)
            self.assertEqual(a["process_private_bytes"], 1000)
            self.assertEqual(a["process_threads"], 32)
            self.assertEqual(a["reported_clock_mhz"], 1800)
            self.assertIsNone(a["swap_in_bytes"])
            self.assertIsNone(a["effective_clock_mhz"])
            self.assertIsNone(a["temperature_c"])
            self.assertIsNone(a["process_page_faults_per_s"])
            psutil.Process.return_value.memory_info.return_value.num_page_faults = 150
            with patch("camera_resources.monotonic", return_value=a["monotonic_s"] + 5):
                b = sampler.sample()
            self.assertEqual(b["process_page_faults_per_s"], 10)
            psutil.Process.return_value.cpu_percent.assert_called_with(interval=None)
            json.dumps(b, allow_nan=False)

    def test_missing_psutil_is_nonfatal(self):
        with patch.dict("sys.modules", {"psutil": None}):
            self.assertFalse(ResourceSampler().sample()["available"])

    def test_individual_access_denied_or_missing_metrics_do_not_hide_others(self):
        psutil = self.fake()
        psutil.Process.return_value.memory_info.side_effect = PermissionError()
        psutil.cpu_freq.side_effect = NotImplementedError()
        psutil.swap_memory.side_effect = PermissionError()
        with patch.dict("sys.modules", {"psutil": psutil}):
            result = ResourceSampler().sample()
        self.assertTrue(result["available"])
        self.assertEqual(result["system_available_bytes"], 4000)
        self.assertIsNone(result["process_private_bytes"])
        self.assertIsNone(result["reported_clock_mhz"])
        self.assertIsNone(result["swap_used_bytes"])

    def test_nonfinite_counters_are_null(self):
        psutil = self.fake()
        psutil.cpu_freq.return_value.current = float("nan")
        psutil.Process.return_value.cpu_percent.return_value = float("inf")
        with patch.dict("sys.modules", {"psutil": psutil}):
            result = ResourceSampler().sample()
        self.assertIsNone(result["reported_clock_mhz"])
        self.assertIsNone(result["process_cpu_percent_one_core"])
        json.dumps(result, allow_nan=False)

    def test_stage_sum_signed_residual_and_actual_runtime_getters_only(self):
        torch = MagicMock()
        torch.get_num_threads.return_value = 8
        torch.get_num_interop_threads.return_value = 16
        model = SimpleNamespace(predictor=SimpleNamespace(device="cpu"))
        timings = {"_collect_runtime": True}
        result = SimpleNamespace(speed={"preprocess": 2, "inference": 90, "postprocess": 3})
        with patch.dict("sys.modules", {"torch": torch}):
            _performance_details(result, model, timings, 100)
        self.assertEqual(timings["prediction_minus_yolo_stages_ms"], 5)
        self.assertEqual(timings["yolo_stages_total_ms"], 95)
        self.assertEqual(timings["_runtime"]["intraop_threads"], 8)
        self.assertEqual(timings["_runtime"]["interop_threads"], 16)
        self.assertEqual(timings["_runtime"]["device"], "cpu")
        torch.set_num_threads.assert_not_called()
        torch.set_num_interop_threads.assert_not_called()
        torch.cuda.synchronize.assert_not_called()

    def test_missing_or_incomplete_speed_does_not_invent_residual(self):
        for speed in (None, {"inference": 10}, {"preprocess": float("nan"), "inference": 10, "postprocess": 2}):
            timings = {}
            _performance_details(SimpleNamespace(speed=speed), None, timings, 100)
            self.assertNotIn("prediction_minus_yolo_stages_ms", timings)
        with patch.dict("sys.modules", {"torch": None}):
            timings = {"_collect_runtime": True}
            _performance_details(SimpleNamespace(), None, timings, 100)
            self.assertIsNone(timings["_runtime"]["device"])

    def test_summary_preserves_signed_residual_and_bounded_runtime(self):
        monitor = CameraPerformance(True)
        monitor.record({"prediction_minus_yolo_stages_ms": -2})
        monitor.runtime({"device": "cpu", "intraop_threads": 8})
        summary = monitor.summary()
        self.assertEqual(summary["timings_ms"]["prediction_minus_yolo_stages_ms"]["max"], -2)
        self.assertEqual(summary["inference_runtime"]["device"], "cpu")

    def test_sampler_failure_does_not_stop_jsonl_writer(self):
        with tempfile.TemporaryDirectory() as folder, patch("camera_resources.ResourceSampler") as factory:
            factory.return_value.sample.side_effect = PermissionError()
            monitor = CameraPerformance(True, directory=folder)
            monitor.start(lambda: {"capture_alive": False, "inference_alive": False})
            monitor.stop()
            monitor._thread.join(2)
            result = json.loads(monitor.path.read_text())
            self.assertFalse(result["resources"]["available"])
            self.assertIn("monotonic_s", result)


if __name__ == "__main__":
    unittest.main()
