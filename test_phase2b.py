"""Mocked Flask integration: no PostgreSQL, SQLite, camera, or serial access."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from types import SimpleNamespace
from flask import render_template

from sqlalchemy.engine import Engine
from sorting_lifecycle import LifecycleError, SQLAlchemySessionStore
from test_sorting_lifecycle import Component, FakeLease, MemoryStore, SortingLifecycle

with patch.dict(os.environ, {
    "LOCAL_TEST_MODE": "1", "SECRET_KEY": "s" * 32,
    "LOCAL_TEST_DATABASE_URL": "postgresql://postgres@127.0.0.1:55432/eggsor_restore_test",
}), patch("database.verify_local_test_schema"), \
     patch.object(Engine, "connect", side_effect=AssertionError("Unexpected database connection")):
    import app as backend


class BackendLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.no_db = patch.object(Engine, "connect", side_effect=AssertionError("Unexpected database connection"))
        self.no_db.start()
        self.addCleanup(self.no_db.stop)
        self.store = MemoryStore()
        self.lifecycle = SortingLifecycle(self.store, Component(self.store, "camera"),
            Component(self.store, "bridge"), lease=FakeLease(),
            incomplete=lambda: bool(backend.EGG_FLOW_PENDING))
        self.context = self.lifecycle.start(7, "operator").to_dict()
        self.lifecycle.ready(self.context)
        self.context.update(capture_id=1, connection_epoch=1)
        self.lifecycle_patch = patch.object(backend, "LIFECYCLE", self.lifecycle)
        self.lifecycle_patch.start()
        self.addCleanup(self.lifecycle_patch.stop)
        backend.EGG_FLOW_PENDING = None
        backend.AUTOMATIC_EGG_CYCLES.clear()
        backend.AUTOMATIC_RECORD_WORKER = None
        backend.AUTOMATIC_LAST_CYCLE_ID = 0
        backend.reset_egg_flow()
        backend.BRIDGE_EPOCH, backend.BRIDGE_READY = 1, True
        backend.BRIDGE_AUTO_RELEASE = False
        backend.EGG_FLOW_CONTEXT = dict(self.context)
        self.publish = patch.object(backend.ESP32_BRIDGE, "publish_status")
        self.publish.start()
        self.addCleanup(self.publish.stop)

    def tearDown(self):
        backend.AUTOMATIC_EGG_CYCLES.clear()
        backend.AUTOMATIC_RECORD_WORKER = None
        backend.EGG_FLOW_PENDING = None
        backend.reset_egg_flow()
        backend.BRIDGE_EPOCH, backend.BRIDGE_READY = None, False
        backend.BRIDGE_AUTO_RELEASE = False

    def pending(self):
        backend.EGG_FLOW_STAGE = "measuring"
        backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9}
        backend.queue_sort_after_measurement({"weight_grams": 52, "size": "Small"})

    def automatic_cycle(self, cycle_id=1):
        with patch.object(backend, "Thread"):
            for kind, fields in (
                ("egg_detected", {}),
                ("egg_complete", {"weight_grams": 59, "size": "Medium"}),
                ("sort_complete", {"size": "Medium"}),
                ("egg_left", {}),
            ):
                backend.handle_esp32_event(self.event(kind=kind, cycle_id=cycle_id, **fields))

    def test_automatic_release_records_later_without_measure_authorization(self):
        self.automatic_cycle()
        capture = {**self.context, "capture_id": 7, "label": "good", "confidence": 0.9}
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality", return_value=capture), \
             patch.object(backend, "persist_capture", return_value=101) as save, \
             patch.object(backend.ESP32_BRIDGE, "measure_egg") as authorize:
            backend._save_automatic_egg_cycles()
        authorize.assert_not_called()
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0]["capture_id"], 7)
        self.assertEqual(save.call_args.args[1]["weight_grams"], 59)
        self.assertFalse(backend.AUTOMATIC_EGG_CYCLES)
        with patch.object(backend, "Thread") as thread:
            self.automatic_cycle()
        self.assertFalse(backend.AUTOMATIC_EGG_CYCLES)

    def test_automatic_weight_without_camera_is_retained_without_invented_quality(self):
        self.automatic_cycle()
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality", return_value=None), \
             patch.object(backend.AUTOMATIC_RECORD_WAKE, "wait",
                          side_effect=lambda _: setattr(self.lifecycle, "state", "interrupted")), \
             patch.object(backend, "persist_capture") as save:
            backend._save_automatic_egg_cycles()
        save.assert_not_called()
        self.assertEqual(backend.AUTOMATIC_EGG_CYCLES[(1, 1)]["egg_complete"]["weight_grams"], 59)

    def test_stable_weight_is_not_recorded_before_size_sort_confirmation(self):
        with patch.object(backend, "Thread"):
            backend.handle_esp32_event(self.event(kind="egg_detected", cycle_id=1))
            backend.handle_esp32_event(self.event(kind="egg_complete", cycle_id=1, weight_grams=59, size="Medium"))
        capture = {**self.context, "capture_id": 7, "label": "good", "confidence": 0.9}
        def wait_for_sort(_):
            save.assert_not_called()
            with patch.object(backend, "Thread"):
                backend.handle_esp32_event(self.event(kind="sort_complete", cycle_id=1, size="Medium"))
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality", return_value=capture), \
             patch.object(backend.AUTOMATIC_RECORD_WAKE, "wait", side_effect=wait_for_sort), \
             patch.object(backend, "persist_capture", return_value=101) as save:
            backend._save_automatic_egg_cycles()
        save.assert_called_once()
        self.assertEqual(save.call_args.args[1]["weight_grams"], 59)

    def test_automatic_cycles_match_camera_captures_in_arrival_order(self):
        self.automatic_cycle(1)
        self.automatic_cycle(2)
        captures = [{**self.context, "capture_id": i, "label": "good", "confidence": 0.9}
                    for i in (7, 9)]
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality", side_effect=captures), \
             patch.object(backend, "persist_capture", return_value=101) as save:
            backend._save_automatic_egg_cycles()
        self.assertEqual([call.args[0]["capture_id"] for call in save.call_args_list], [7, 9])
        self.assertEqual([call.args[0]["cycle_id"] for call in save.call_args_list], [1, 2])

    def event(self, kind="sort_complete", **fields):
        return {**self.context, "type": kind, "size": "Medium", **fields}

    def test_automatic_handshake_disables_obsolete_camera_authorization(self):
        with patch.object(backend, "Thread") as worker:
            backend.handle_esp32_event(self.event(kind="controller_protocol", protocol="EGGSORT_AUTO_V1"))
            backend.handle_esp32_event(self.event(kind="controller_state", state="WAITING FOR CAMERA"))
        self.assertTrue(backend.BRIDGE_AUTO_RELEASE)
        worker.assert_not_called()

    def test_measurement_failure_displays_exact_controller_reason(self):
        for tagged in (False, True):
            with self.subTest(tagged=tagged):
                self.lifecycle.state = "running"
                fields = {"cycle_id": 1} if tagged else {}
                backend.handle_esp32_event(self.event(kind="measurement_failed",
                    reason="LOAD CELL GATE UNAVAILABLE; PCA9685 NOT FOUND AT 0x40", **fields))
                self.assertEqual(self.lifecycle.state, "failed")
                self.assertIn("PCA9685 NOT FOUND AT 0x40", backend.ESP32_BRIDGE.publish_status.call_args.args[0])

    def test_default_write_gate_remains_closed(self):
        with self.assertRaises(backend.CameraSessionError):
            backend.assert_lifecycle_writes_allowed()

    def test_write_flag_alone_cannot_enable_public_writes(self):
        with patch.dict(backend.app.config, {"LIFECYCLE_WRITES_ENABLED": True}):
            with self.assertRaises(backend.CameraSessionError):
                backend.assert_lifecycle_writes_allowed()

    def test_disposable_write_guard_requires_exact_role_database_and_schema(self):
        from config import build_app_config
        from flask import Flask
        with patch.dict(os.environ, {
            "LOCAL_TEST_MODE": "0", "PHASE2B_WRITE_TEST_MODE": "1",
            "PHASE2B_WRITE_TEST_DATABASE_URL":
                "postgresql://eggsor_phase2b_runner:dummy@127.0.0.1:55432/eggsor_p2b_disposable",
            "SECRET_KEY": "s" * 32,
        }, clear=True):
            config = build_app_config()
        config["LIFECYCLE_WRITES_ENABLED"] = True
        with patch.dict(backend.app.config, config):
            # The existing engine still points to the read-only restored DB.
            with self.assertRaises(backend.CameraSessionError):
                backend.assert_lifecycle_writes_allowed()
        disposable = Flask("offline-disposable-guard")
        disposable.config.from_mapping(config)
        backend.db.init_app(disposable)
        with patch.object(backend, "app", disposable):
            backend.assert_lifecycle_writes_allowed()
            disposable.config["SQLALCHEMY_DATABASE_URI"] = (
                "postgresql://eggsor_phase2b_runner:dummy@127.0.0.1:55432/eggsor_restore_test"
            )
            with self.assertRaises(backend.CameraSessionError):
                backend.assert_lifecycle_writes_allowed()

    def test_production_write_guard_checks_active_engine_and_explicit_switch(self):
        from config import build_app_config
        from flask import Flask
        with patch.dict(os.environ, {
            "LOCAL_TEST_MODE": "0", "PHASE2B_WRITE_TEST_MODE": "0",
            "EGGSOR_PRODUCTION_DB_MODE": "1",
            "DATABASE_URL": "postgresql://postgres.gtsymkfcoslnobitdheu:dummy@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres",
            "SECRET_KEY": "s" * 32,
        }, clear=True):
            config = build_app_config()
        production = Flask("offline-production-guard")
        production.config.from_mapping(config)
        backend.db.init_app(production)
        with patch.object(backend, "app", production):
            with self.assertRaises(backend.CameraSessionError):
                backend.assert_lifecycle_writes_allowed()
            production.config["LIFECYCLE_WRITES_ENABLED"] = True
            backend.assert_lifecycle_writes_allowed()
            production.config["SQLALCHEMY_DATABASE_URI"] = (
                "postgresql://postgres.gtsymkfcoslnobitdheu:dummy@127.0.0.1:55432/eggsor_restore_test"
            )
            with self.assertRaises(backend.CameraSessionError):
                backend.assert_lifecycle_writes_allowed()

    def test_pending_egg_uses_capture_reference_not_global_camera(self):
        with patch.object(backend.CAMERA_SESSION, "status", side_effect=AssertionError("Global session read")):
            self.pending()
        self.assertEqual(backend.EGG_FLOW_PENDING["session_ref"], self.context["session_ref"])
        self.assertEqual(backend.EGG_FLOW_PENDING["_context"]["operator_user_id"], 7)

    def test_delayed_previous_session_and_previous_connection_ignored(self):
        self.pending()
        with patch.object(backend, "persist_capture") as persist:
            backend.handle_esp32_event(self.event(session_ref="old"))
            backend.handle_esp32_event(self.event(connection_epoch=0))
            persist.assert_not_called()
        self.assertIsNotNone(backend.EGG_FLOW_PENDING)

    def test_confirmation_saves_original_identity_once(self):
        self.pending()
        with patch.object(backend, "persist_capture", return_value=101) as persist:
            backend.handle_esp32_event(self.event())
            backend.handle_esp32_event(self.event())
        persist.assert_called_once()
        context, payload = persist.call_args.args
        self.assertEqual(context["session_ref"], self.context["session_ref"])
        self.assertEqual(payload["session_ref"], self.context["session_ref"])
        self.assertNotIn("_context", payload)
        self.assertIsNone(backend.EGG_FLOW_PENDING)

    def test_failed_save_and_egg_left_preserve_confirmed_work(self):
        self.pending()
        with patch.object(backend, "persist_capture", side_effect=RuntimeError("offline")), \
             patch.object(backend.app.logger, "exception"):
            backend.handle_esp32_event(self.event())
        backend.handle_esp32_event(self.event(kind="egg_left"))
        self.assertTrue(backend.EGG_FLOW_PENDING["_confirmed"])
        self.assertEqual(backend.EGG_FLOW_STAGE, "sorting")
        with self.assertRaises(LifecycleError):
            self.lifecycle.stop()
            self.lifecycle.start(7, "operator")

    def test_old_save_completion_cannot_clear_new_pending_state(self):
        self.pending()
        def save(context, payload):
            backend.EGG_FLOW_SEQUENCE += 1
            backend.EGG_FLOW_PENDING = {"new": True}
            backend.EGG_FLOW_STAGE = "sorting"
            return 101
        with patch.object(backend, "persist_capture", side_effect=save):
            backend.handle_esp32_event(self.event())
        self.assertEqual(backend.EGG_FLOW_PENDING, {"new": True})

    def test_mismatched_size_is_not_saved(self):
        self.pending()
        with patch.object(backend, "persist_capture") as persist:
            backend.handle_esp32_event(self.event(size="Large"))
            persist.assert_not_called()
        self.assertFalse(backend.EGG_FLOW_PENDING["_confirmed"])

    def test_reconnect_fails_session_instead_of_reusing_untagged_cycle(self):
        self.pending()
        backend.handle_esp32_event(self.event(kind="connection_open", connection_epoch=2))
        self.assertEqual(self.lifecycle.state, "failed")
        self.assertFalse(backend.BRIDGE_READY)
        self.assertIsNotNone(backend.EGG_FLOW_PENDING)

    def test_rejected_capture_keeps_original_context(self):
        capture = {**self.context, "label": "crack", "confidence": 0.8}
        with patch.object(backend, "persist_capture", return_value=101) as persist, \
             patch.object(backend.ESP32_BRIDGE, "publish_camera_capture"), \
             patch.object(backend, "Thread"):
            backend.save_camera_capture(capture)
            backend._save_rejected_captures()
        self.assertEqual(persist.call_args.args[1]["session_ref"], self.context["session_ref"])
        self.assertTrue(persist.call_args.kwargs["rejected"])

    def test_removal_before_confirmation_allows_next_egg_after_save(self):
        self.pending()
        backend.handle_esp32_event(self.event(kind="egg_left"))
        self.assertIsNotNone(backend.EGG_FLOW_PENDING)
        with patch.object(backend, "persist_capture", return_value=101) as persist:
            backend.handle_esp32_event(self.event())
        persist.assert_called_once()
        self.assertEqual(backend.EGG_FLOW_STAGE, "idle")
        self.assertIsNone(backend.EGG_FLOW_CONTEXT)

    def test_early_removal_stops_capture_matching(self):
        backend.EGG_FLOW_STAGE = "inspecting"
        backend.handle_esp32_event(self.event(kind="egg_left"))
        self.assertEqual(self.lifecycle.state, "failed")
        self.assertFalse(backend._flow_is_current(backend.EGG_FLOW_SEQUENCE))

    def test_invalid_weight_or_size_never_creates_pending_record(self):
        for weight, size in ((-1, "Small"), (0, "Small"), (True, "Small"),
                             (float("nan"), "Jumbo"), (52, "Large"), (52, None)):
            with self.subTest(weight=weight, size=size):
                backend.EGG_FLOW_STAGE = "measuring"
                backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9}
                backend.queue_sort_after_measurement({"weight_grams": weight, "size": size})
                self.assertIsNone(backend.EGG_FLOW_PENDING)

    def test_rejected_save_failure_retries_same_capture_without_duplicate(self):
        capture = {**self.context, "label": "crack", "confidence": 0.8}
        with patch.object(backend, "Thread"), \
             patch.object(backend, "persist_capture", side_effect=[RuntimeError("offline"), 101]) as persist, \
             patch.object(backend, "sleep"), \
             patch.object(backend.app.logger, "exception"):
            backend.save_camera_capture(capture)
            backend.save_camera_capture(capture)
            backend._save_rejected_captures()
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(persist.call_args_list[0], persist.call_args_list[1])
        self.assertFalse(backend.REJECTED_CAPTURES)

    def test_good_camera_capture_does_not_write_before_sort(self):
        with patch.object(backend, "persist_capture") as persist:
            backend.save_camera_capture({**self.context, "label": "good", "confidence": 0.8})
        persist.assert_not_called()

    def test_measurement_stage_is_set_before_fast_response(self):
        backend.EGG_FLOW_STAGE = "inspecting"
        capture = {**self.context, "label": "good", "confidence": 0.8}
        def measure(*args, **kwargs):
            self.assertEqual(backend.EGG_FLOW_STAGE, "measuring")
            backend.queue_sort_after_measurement({"weight_grams": 52, "size": "Small", "capture_id": 1})
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality", return_value=capture), \
             patch.object(backend.ESP32_BRIDGE, "measure_egg", side_effect=measure):
            backend._inspect_then_measure(backend.EGG_FLOW_SEQUENCE)
        self.assertEqual(backend.EGG_FLOW_STAGE, "sorting")

    def test_already_loaded_controller_status_recovers_missed_arrival(self):
        backend.BRIDGE_READY = False
        backend.EGG_FLOW_STAGE = "idle"
        with patch.object(backend, "Thread") as worker:
            backend.handle_esp32_event(self.event(kind="controller_state", state="WAITING FOR CAMERA"))
            backend.handle_esp32_event(self.event(kind="controller_state", state="WAITING FOR CAMERA"))
        self.assertTrue(backend.BRIDGE_READY)
        self.assertEqual(backend.EGG_FLOW_STAGE, "inspecting")
        worker.assert_called_once()

    def test_visible_good_egg_sends_measure_and_records_only_after_sort(self):
        from camera_session import CameraDetectionSession
        from esp32_bridge import Esp32Bridge
        camera = CameraDetectionSession()
        camera._runtime_context = dict(self.context)
        camera._running = True
        camera.capture_min_samples = 1
        camera._process_auto_capture("good", 0.9)
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        bridge._serial.write.side_effect = lambda _: backend.queue_sort_after_measurement(
            {"weight_grams": 52, "size": "Small", "capture_id": 1})
        backend.EGG_FLOW_STAGE = "inspecting"
        with patch.object(backend, "CAMERA_SESSION", camera), \
             patch.object(backend, "ESP32_BRIDGE", bridge), \
             patch.object(backend, "persist_capture", return_value=101) as persist:
            backend._inspect_then_measure(backend.EGG_FLOW_SEQUENCE)
            bridge._serial.write.assert_called_once_with(b"MEASURE:GOOD:1\n")
            persist.assert_not_called()
            self.assertEqual(backend.EGG_FLOW_STAGE, "sorting")
            backend.handle_esp32_event(self.event(capture_id=1))
            backend.handle_esp32_event(self.event(capture_id=1))
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1]["quality"], "Good")
        self.assertEqual(persist.call_args.args[1]["weight_grams"], 52)
        self.assertEqual(camera.status()["egg_count"], 1)

    def test_stopped_inspection_worker_retries_same_locked_capture(self):
        backend.EGG_FLOW_STAGE = "inspecting"
        backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9}
        worker = Mock()
        worker.is_alive.return_value = False
        backend.EGG_FLOW_INSPECTION_THREAD = worker
        sequence = backend.EGG_FLOW_SEQUENCE
        with patch.object(backend, "Thread") as thread:
            backend.handle_esp32_event(self.event(kind="controller_state", state="WAITING FOR CAMERA"))
        thread.assert_called_once()
        self.assertEqual(backend.EGG_FLOW_SEQUENCE, sequence)
        self.assertEqual(backend.EGG_FLOW_QUALITY["capture_id"], 1)

    def test_retry_uses_locked_capture_instead_of_consuming_next_camera_egg(self):
        backend.EGG_FLOW_STAGE = "inspecting"
        backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9}
        with patch.object(backend.CAMERA_SESSION, "wait_for_captured_quality") as camera, \
             patch.object(backend.ESP32_BRIDGE, "measure_egg") as measure:
            backend._inspect_then_measure(backend.EGG_FLOW_SEQUENCE)
        camera.assert_not_called()
        measure.assert_called_once_with("Good", capture_id=1)

    def test_authorization_exception_keeps_capture_available_for_retry(self):
        backend.EGG_FLOW_STAGE = "measuring"
        backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9}
        with patch.object(backend, "_inspect_then_measure", side_effect=ValueError("failed")), \
             patch.object(backend.app.logger, "exception"):
            backend._run_egg_inspection(backend.EGG_FLOW_SEQUENCE)
        self.assertEqual(backend.EGG_FLOW_STAGE, "inspecting")
        self.assertEqual(backend.EGG_FLOW_QUALITY["capture_id"], 1)

    def test_other_eggs_weight_and_sort_cannot_complete_pending_capture(self):
        backend.EGG_FLOW_STAGE = "measuring"
        backend.EGG_FLOW_QUALITY = {**self.context, "label": "Good", "confidence": 0.9,
                                    "_requires_capture_id": True}
        for capture_id in (None, 2):
            backend.queue_sort_after_measurement({"weight_grams": 52, "size": "Small",
                                                 "capture_id": capture_id})
            self.assertIsNone(backend.EGG_FLOW_PENDING)
        backend.queue_sort_after_measurement({"weight_grams": 52, "size": "Small", "capture_id": 1})
        with patch.object(backend, "persist_capture", return_value=101) as persist:
            backend.handle_esp32_event(self.event(capture_id=2))
            persist.assert_not_called()
            self.assertEqual(backend.EGG_FLOW_PENDING["_capture_id"], 1)
            backend.handle_esp32_event(self.event(capture_id=1))
            backend.handle_esp32_event(self.event(capture_id=1))
        persist.assert_called_once()


    def test_explicit_unknown_audit_does_not_inherit_request_user(self):
        session = Mock()
        with backend.app.test_request_context(), patch.object(backend, "db", SimpleNamespace(session=session)):
            backend.session["user_id"] = 7
            backend.session["username"] = "operator"
            backend.write_audit_log("historical", "test", actor="System", user_id=None, commit=False)
        log = session.add.call_args.args[0]
        self.assertIsNone(log.user_id)
        self.assertEqual(log.actor, "System")
        session.get.assert_not_called()

    def test_inactive_user_cannot_auto_start(self):
        with backend.app.test_request_context(), \
             patch.dict(backend.app.config, {"LOCAL_HARDWARE_ENABLED": True, "AUTO_START_SORTING_ON_LOGIN": True}), \
             patch.object(backend.db.session, "get", return_value=SimpleNamespace(is_active=False)), \
             patch.object(backend, "start_sorting_runtime") as start:
            backend.session["user_id"] = 7
            backend.ensure_authenticated_sorting_runtime()
            start.assert_not_called()

    def test_stop_pauses_automatic_restart_before_status_poll(self):
        with patch.object(backend, "assert_lifecycle_writes_allowed"), \
             patch.dict(backend.app.config, {"LOCAL_HARDWARE_ENABLED": True,
                                             "AUTO_START_SORTING_ON_LOGIN": True}):
            backend.stop_sorting_runtime(manual=True)
            self.assertEqual(self.lifecycle.state, "completed")
            with backend.app.test_request_context("/api/camera/status"), \
                 patch.object(backend, "start_sorting_runtime") as start:
                backend.session["user_id"] = 7
                backend.ensure_authenticated_sorting_runtime()
                start.assert_not_called()

    def test_staff_dashboard_omits_audit_panel_and_api_data(self):
        egg_query = Mock()
        egg_query.count.return_value = 0
        egg_query.order_by.return_value.first.return_value = None
        egg_query.filter.return_value.all.return_value = []
        egg_query.filter_by.return_value.count.return_value = 0
        tray_query = Mock()
        tray_query.filter_by.return_value.count.return_value = 0
        audit_query = Mock()
        audit_query.order_by.return_value.limit.return_value.all.return_value = [
            SimpleNamespace(to_dict=lambda: {"event_type": "login"})
        ]
        with backend.app.test_request_context("/api/dashboard/stats"), \
             patch.object(backend.EggRecord, "query", egg_query), \
             patch.object(backend.TraySizeAlert, "query", tray_query), \
             patch.object(backend.AuditLog, "query", audit_query), \
             patch.object(backend.db.session, "query") as aggregate_query, \
             patch.object(backend.ESP32_BRIDGE, "status", return_value={}):
            aggregate_query.return_value.group_by.return_value.all.return_value = []
            backend.session["role"] = "staff"
            html = render_template("dashboard.html", username="staff")
            data = backend.dashboard_stats.__wrapped__().get_json()
            self.assertNotIn('<div id="dashboard-audit-logs"', html)
            self.assertNotIn("audit_logs", data)
            audit_query.order_by.assert_not_called()
            backend.session["role"] = "admin"
            admin_html = render_template("dashboard.html", username="admin")
            admin_data = backend.dashboard_stats.__wrapped__().get_json()
            self.assertIn('<div id="dashboard-audit-logs"', admin_html)
            self.assertEqual(admin_data["audit_logs"], [{"event_type": "login"}])

    def test_egg_history_filters_browser_local_date_boundaries(self):
        query = Mock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = []
        with backend.app.test_request_context("/api/egg-records", query_string={
            "start_at": "2026-10-16T00:00:00+08:00",
            "end_before": "2026-10-17T00:00:00+08:00",
        }), patch.object(backend.EggRecord, "query", query):
            self.assertEqual(backend.egg_records_data.__wrapped__().get_json()["records"], [])
        boundaries = [call.args[0].right.value for call in query.filter.call_args_list[1:]]
        self.assertEqual(boundaries, [
            datetime(2026, 10, 15, 16, tzinfo=timezone.utc),
            datetime(2026, 10, 16, 16, tzinfo=timezone.utc),
        ])
        with backend.app.test_request_context("/api/egg-records", query_string={
            "start_at": "2026-10-17T00:00:00+08:00",
            "end_before": "2026-10-16T00:00:00+08:00",
        }), patch.object(backend.EggRecord, "query", query):
            self.assertEqual(backend.egg_records_data.__wrapped__()[1], 400)

    def test_egg_history_page_has_date_controls(self):
        with backend.app.test_request_context("/egg-records"):
            html = render_template("egg_records.html", username="staff")
        self.assertIn('id="start-date-filter"', html)
        self.assertIn('id="end-date-filter"', html)

    def test_store_parent_commit_and_rejected_owner_are_atomic(self):
        fake_session = Mock()
        fake_session.get.return_value = SimpleNamespace(id=7, is_active=True)
        model = Mock()
        store = SQLAlchemySessionStore(backend.app, Mock())
        with patch('extensions.db', SimpleNamespace(session=fake_session)), patch('models.SortingSession', model):
            store.create(self.lifecycle.context)
            self.assertEqual(model.call_args.kwargs['user_id'], 7)
            self.assertEqual(model.call_args.kwargs['status'], 'starting')
            fake_session.commit.assert_called_once()
            fake_session.get.return_value = SimpleNamespace(id=7, is_active=False)
            with self.assertRaises(LifecycleError):
                store.create(self.lifecycle.context)
            fake_session.rollback.assert_called_once()

    def test_store_write_guard_runs_before_database_access(self):
        guard = Mock(side_effect=LifecycleError('writes disabled'))
        store = SQLAlchemySessionStore(backend.app, guard)
        for operation in (lambda: store.create(self.lifecycle.context),
                          lambda: store.transition(self.lifecycle.context, 'running'),
                          store.reconcile):
            with self.assertRaises(LifecycleError):
                operation()


if __name__ == "__main__":
    unittest.main()
