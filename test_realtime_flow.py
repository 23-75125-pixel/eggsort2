import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SUPABASE_DB_URL"] = ""

import app as app_module


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


if __name__ == "__main__":
    unittest.main()
