"""No real torch/YOLO import, inference, webcam, or serial operations."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import detection_service
from cpu_inference_threads import initialize_cpu_threads


class CPUThreadTests(unittest.TestCase):
    def modules(self):
        torch = MagicMock()
        state = {"threads": 8}
        torch.set_num_threads.side_effect = lambda n: state.update(threads=n)
        torch.get_num_threads.side_effect = lambda: state["threads"]
        utils = SimpleNamespace(NUM_THREADS=8)
        ultralytics = MagicMock()
        modules = {"torch": torch, "ultralytics": ultralytics,
                   "ultralytics.utils": SimpleNamespace(torch_utils=utils)}
        return torch, state, utils, ultralytics, modules

    def test_selector_reset_uses_four_before_any_forward(self):
        torch, state, utils, _, modules = self.modules()
        with patch.dict("sys.modules", modules):
            verify = initialize_cpu_threads(4)
            self.assertEqual(utils.NUM_THREADS, 4)
            # Simulate the actual installed CPU select_device behavior.
            torch.set_num_threads(utils.NUM_THREADS)
            verify(SimpleNamespace(device=SimpleNamespace(type="cpu")))
        self.assertEqual(state["threads"], 4)

    def test_verification_does_not_set_threads_per_frame_or_change_interop(self):
        torch, _, _, _, modules = self.modules()
        predictor = SimpleNamespace(device=SimpleNamespace(type="cpu"))
        with patch.dict("sys.modules", modules):
            verify = initialize_cpu_threads(4)
            for _ in range(100):
                verify(predictor)
                verify(predictor)
        torch.set_num_threads.assert_called_once_with(4)
        torch.set_num_interop_threads.assert_not_called()

    def test_override_detected_instead_of_reset_every_frame(self):
        torch, state, _, _, modules = self.modules()
        with patch.dict("sys.modules", modules):
            verify = initialize_cpu_threads(4)
            state["threads"] = 8
            with self.assertLogs("cpu_inference_threads", level="ERROR"), self.assertRaisesRegex(RuntimeError, "overridden"):
                verify(SimpleNamespace(device=SimpleNamespace(type="cpu")))
        torch.set_num_threads.assert_called_once_with(4)

    def test_failed_initialization_is_not_reported_as_success(self):
        torch, _, _, _, modules = self.modules()
        torch.set_num_threads.side_effect = None
        with patch.dict("sys.modules", modules), self.assertRaisesRegex(RuntimeError, "initialization failed"):
            initialize_cpu_threads(4)

    def test_model_initialization_registers_verifiers_and_caches_model(self):
        torch, state, utils, ultralytics, modules = self.modules()
        def construct(path):
            self.assertEqual(state["threads"], 4)  # Before YOLO constructor.
            return MagicMock()
        ultralytics.YOLO.side_effect = construct
        with patch.dict("sys.modules", modules), patch.object(detection_service, "_model", None), patch.object(
            detection_service, "CPU_INFERENCE_THREADS", 4
        ), patch.object(detection_service.MODEL_PATH.__class__, "is_file", return_value=True):
            model = detection_service._load_model()
            self.assertIs(detection_service._load_model(), model)
        self.assertEqual([c.args[0] for c in model.add_callback.call_args_list],
                         ["on_predict_start", "on_predict_batch_end"])
        ultralytics.YOLO.assert_called_once_with(str(detection_service.MODEL_PATH))
        torch.set_num_threads.assert_called_once_with(4)

    def test_disabled_model_loading_does_not_apply_policy_or_callbacks(self):
        _, state, utils, ultralytics, modules = self.modules()
        with patch.dict("sys.modules", modules), patch.object(detection_service, "_model", None), patch.object(
            detection_service, "CPU_INFERENCE_THREADS", 0
        ), patch.object(detection_service.MODEL_PATH.__class__, "is_file", return_value=True), patch(
            "cpu_inference_threads.initialize_cpu_threads"
        ) as initialize:
            model = detection_service._load_model()
        initialize.assert_not_called()
        model.add_callback.assert_not_called()
        self.assertEqual(utils.NUM_THREADS, 8)
        self.assertEqual(state["threads"], 8)

    def test_predict_arguments_are_unchanged_with_option_enabled(self):
        model = MagicMock()
        model.predict.return_value = [SimpleNamespace(names={}, boxes=None)]
        frame = SimpleNamespace(shape=(1080, 1920, 3))
        with patch.object(detection_service, "CPU_INFERENCE_THREADS", 4), patch.object(
            detection_service, "_load_model", return_value=model
        ):
            detection_service.detect_image(frame)
        model.predict.assert_called_once_with(source=frame, conf=detection_service.CONFIDENCE,
                                              imgsz=detection_service.IMAGE_SIZE, verbose=False)

    def test_runtime_diagnostics_report_configured_and_actual_count(self):
        torch, _, _, _, modules = self.modules()
        torch.get_num_threads.side_effect = None
        torch.get_num_threads.return_value = 4
        torch.get_num_interop_threads.return_value = 16
        timings = {"_collect_runtime": True}
        with patch.dict("sys.modules", modules), patch.object(detection_service, "CPU_INFERENCE_THREADS", 4):
            detection_service._performance_details(SimpleNamespace(),
                SimpleNamespace(predictor=SimpleNamespace(device="cpu")), timings, 100)
        self.assertEqual(timings["_runtime"]["configured_intraop_threads"], 4)
        self.assertEqual(timings["_runtime"]["intraop_threads"], 4)
        self.assertTrue(timings["_runtime"]["thread_limit_verified"])


if __name__ == "__main__":
    unittest.main()
