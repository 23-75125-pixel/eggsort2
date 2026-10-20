import os
import unittest
from contextlib import nullcontext
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SUPABASE_DB_URL"] = ""

import app as app_module
from esp32_bridge import Esp32ProtocolParser


class RealtimePersistenceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        with app_module.EGG_FLOW_LOCK:
            app_module.EGG_FLOW_SEQUENCE += 1
            self.sequence = app_module.EGG_FLOW_SEQUENCE
            app_module.EGG_FLOW_STAGE = "sorting"
            app_module.EGG_FLOW_PENDING = {
                "_capture_id": 7,
                "weight_grams": 58,
                "size": "Medium",
                "quality": "Good",
                "confidence": 0.98,
                "session_ref": "TEST-SESSION",
            }

    def tearDown(self) -> None:
        app_module.reset_egg_flow()

    def test_database_failure_restores_recoverable_sorting_state(self) -> None:
        with (
            patch("app.EggRecord", side_effect=RuntimeError("database offline")),
            patch("app.ESP32_BRIDGE.publish_status") as publish_status,
            patch("app.app.logger.exception"),
        ):
            app_module.save_sorted_egg(
                {"type": "sort_complete", "size": "Medium"}
            )

        with app_module.EGG_FLOW_LOCK:
            self.assertEqual(app_module.EGG_FLOW_STAGE, "sorting")
            self.assertIsNotNone(app_module.EGG_FLOW_PENDING)
        publish_status.assert_called_once()
        self.assertEqual(publish_status.call_args.args[1], "flow_error")

    def test_duplicate_confirmation_is_ignored_while_save_is_in_progress(self) -> None:
        with app_module.EGG_FLOW_LOCK:
            app_module.EGG_FLOW_STAGE = "saving"
        with patch("app.EggRecord") as egg_record:
            app_module.save_sorted_egg(
                {"type": "sort_complete", "size": "Medium"}
            )
        egg_record.assert_not_called()

    def test_good_egg_reaches_save_for_controller_boundary_sizes(self) -> None:
        # These weights previously produced a different Python size, so the
        # real controller's completion message blocked persistence.
        cases = [(49, "SMALL"), (55, "MEDIUM"),
                 (62, "EXTRA LARGE"), (67, "JUMBO")]
        for weight, size in cases:
            with self.subTest(weight=weight):
                context = {"session_ref": "TEST-SESSION", "capture_id": 7,
                           "label": "Good", "confidence": 0.98}
                with app_module.EGG_FLOW_LOCK:
                    app_module.EGG_FLOW_STAGE = "measuring"
                    app_module.EGG_FLOW_QUALITY = context
                    app_module.EGG_FLOW_PENDING = None
                parser = Esp32ProtocolParser()
                parser.parse(f"FINAL WEIGHT: {weight} g")
                measured = parser.parse(f"SIZE: {size}")[0]
                confirmed = parser.parse(f"SERVO SORTED: {size}")[0]
                with (
                    patch("app.ESP32_BRIDGE.publish_status"),
                    patch.object(app_module.LIFECYCLE, "persistence",
                                 return_value=nullcontext()),
                    patch("app.persist_capture", return_value=123) as save,
                ):
                    app_module.queue_sort_after_measurement(measured)
                    save.assert_not_called()
                    app_module.save_sorted_egg(confirmed)
                    app_module.save_sorted_egg(confirmed)
                save.assert_called_once()
                self.assertEqual(save.call_args.args[1]["quality"], "Good")
                self.assertEqual(save.call_args.args[1]["weight_grams"], weight)
                self.assertEqual(save.call_args.args[1]["size"], size.title())
                self.assertIsNone(app_module.EGG_FLOW_PENDING)


if __name__ == "__main__":
    unittest.main()
