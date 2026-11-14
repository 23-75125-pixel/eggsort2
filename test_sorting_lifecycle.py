"""Lifecycle tests use memory stores and simulated components, never a database."""
import unittest
from unittest.mock import Mock, patch
from threading import Event, Thread
from time import monotonic
from datetime import datetime, timezone

from sorting_lifecycle import (
    AutomaticStartSuppressed,
    LifecycleError,
    SessionReferenceConflict,
    SortingLifecycle,
)


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.events = []
        self.fail_create = False

    def reconcile(self):
        self.events.append("reconcile")
        for row in self.rows.values():
            if row["status"] in {"starting", "running", "stopping"}:
                row["status"] = "interrupted"

    def create(self, context):
        if self.fail_create:
            raise RuntimeError("simulated database failure")
        if context.session_ref in self.rows:
            raise SessionReferenceConflict("simulated duplicate primary key")
        self.rows[context.session_ref] = {"user_id": context.operator_user_id, "status": "starting", "ended_at": None}
        self.events.append("commit-parent")

    def transition(self, context, status, ended_at=None):
        row = self.rows[context.session_ref]
        row["status"] = status
        if ended_at is not None:
            row["ended_at"] = ended_at


class Component:
    def __init__(self, store, name):
        self.running = False
        self.store, self.name = store, name
        self.starts = 0
        self.fail_start = False
        self.fail_stop = False
        self.stuck = False

    def start(self, *, context):
        assert context["session_ref"] in self.store.rows
        self.store.events.append("start-" + self.name)
        self.starts += 1
        self.running = True
        if self.fail_start:
            raise RuntimeError("simulated startup failure")

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("simulated stop failure")
        if not self.stuck:
            self.running = False

    def status(self):
        return {"running": self.running}


class FakeLease:
    def acquire(self):
        pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.camera = Component(self.store, "camera")
        self.bridge = Component(self.store, "bridge")
        self.incomplete = False
        self.lifecycle = SortingLifecycle(self.store, self.camera, self.bridge,
            lease=FakeLease(), incomplete=lambda: self.incomplete)

    def start(self):
        context = self.lifecycle.start(7, "operator")
        self.lifecycle.ready(context.to_dict())
        return context

    def wait_for_state(self, state, timeout=1):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self.lifecycle.snapshot()["status"] == state:
                return
            Event().wait(0.005)
        self.fail(f"Lifecycle did not reach {state!r} before timeout")

    def test_startup_readiness_just_before_deadline_cancels_timeout(self):
        self.lifecycle.readiness_timeout_seconds = 0.15
        self.lifecycle.readiness_missing = lambda: []
        context = self.lifecycle.start(7, "operator")
        Event().wait(0.11)
        self.lifecycle.ready(context.to_dict())
        Event().wait(0.08)
        self.assertEqual(self.lifecycle.state, "running")
        self.assertEqual(self.store.rows[context.session_ref]["status"], "running")
        self.assertTrue(self.camera.running)
        self.assertTrue(self.bridge.running)

    def test_startup_timeout_identifies_missing_camera_frame_and_ignores_late_ready(self):
        self.lifecycle.readiness_timeout_seconds = 0.03
        self.lifecycle.readiness_missing = lambda: ["first camera frame"]
        context = self.lifecycle.start(7, "operator")
        self.wait_for_state("failed")
        self.assertIn("first camera frame", self.lifecycle.failure_reason)
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])
        self.assertFalse(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.lifecycle.ready(context.to_dict())
        self.assertEqual(self.lifecycle.state, "failed")
        self.assertEqual(self.store.rows[context.session_ref]["status"], "failed")

    def test_startup_timeout_identifies_missing_esp32_readiness(self):
        self.lifecycle.readiness_timeout_seconds = 0.03
        self.lifecycle.readiness_missing = lambda: ["ESP32 controller readiness signal"]
        self.lifecycle.start(7, "operator")
        self.wait_for_state("failed")
        self.assertIn("ESP32 controller readiness signal", self.lifecycle.failure_reason)
        self.assertFalse(self.camera.running)
        self.assertFalse(self.bridge.running)

    def test_startup_timeout_reports_readiness_status_exception(self):
        self.lifecycle.readiness_timeout_seconds = 0.03
        self.lifecycle.readiness_missing = Mock(side_effect=RuntimeError("private device configuration"))
        context = self.lifecycle.start(7, "operator")
        self.wait_for_state("failed")
        self.assertIn("readiness status check error", self.lifecycle.failure_reason)
        self.assertNotIn("private device configuration", self.lifecycle.failure_reason)
        self.assertFalse(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.lifecycle.ready(context.to_dict())
        self.assertEqual(self.lifecycle.state, "failed")

    def test_startup_timeout_preserves_recovery_when_worker_cleanup_fails(self):
        self.lifecycle.readiness_timeout_seconds = 0.03
        self.lifecycle.readiness_missing = lambda: ["first camera frame"]
        self.camera.fail_stop = True
        context = self.lifecycle.start(7, "operator")
        self.wait_for_state("failed")
        self.assertEqual(self.store.rows[context.session_ref]["status"], "failed")
        self.assertTrue(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])
        self.assertIn("Worker cleanup failed", self.lifecycle.failure_reason)
        self.assertIn("remains running", self.lifecycle.failure_reason)

    def test_stale_timeout_from_old_session_cannot_fail_new_session(self):
        self.lifecycle.readiness_timeout_seconds = 10
        first = self.lifecycle.start(7, "operator")
        self.lifecycle.ready(first.to_dict())
        self.lifecycle.stop()
        second = self.lifecycle.start(7, "operator")
        self.lifecycle._expire_startup_readiness(first.to_dict())
        self.assertEqual(self.lifecycle.state, "starting")
        self.lifecycle.ready(second.to_dict())
        self.assertEqual(self.lifecycle.state, "running")

    def test_parent_committed_before_workers_and_correct_owner(self):
        context = self.start()
        self.assertEqual(self.store.events, ["reconcile", "commit-parent", "start-bridge", "start-camera"])
        self.assertEqual(self.store.rows[context.session_ref]["user_id"], 7)
        self.assertRegex(context.session_ref, r"^SES-\d{8}-\d{6}(?:-\d{2})?$")
        self.assertEqual(len(context.session_ref), 19)
        self.assertEqual(self.lifecycle.state, "running")

    def test_concurrent_starts_reuse_original_owner(self):
        contexts = []
        threads = [Thread(target=lambda uid=i: contexts.append(self.lifecycle.start(uid, "staff"))) for i in range(1, 6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len({c.session_ref for c in contexts}), 1)
        self.assertEqual(self.camera.starts, 1)
        self.assertEqual(self.bridge.starts, 1)
        self.assertEqual(self.store.rows[contexts[0].session_ref]["user_id"], contexts[0].operator_user_id)

    def test_parent_failure_does_not_start_components(self):
        self.store.fail_create = True
        with self.assertRaises(RuntimeError):
            self.lifecycle.start(7, "operator")
        self.assertEqual(self.camera.starts + self.bridge.starts, 0)

    def test_startup_failure_finalizes_and_stops_partial_workers(self):
        self.camera.fail_start = True
        with self.assertRaises(RuntimeError):
            self.lifecycle.start(7, "operator")
        self.assertEqual(self.lifecycle.state, "failed")
        self.assertIsNotNone(next(iter(self.store.rows.values()))["ended_at"])
        self.assertFalse(self.bridge.running)

    def test_startup_database_failure_does_not_skip_either_cleanup(self):
        self.camera.fail_start = True
        self.store.transition = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database offline"))
        with self.assertRaisesRegex(RuntimeError, "simulated startup failure"):
            self.lifecycle.start(7, "operator")
        self.assertFalse(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.assertTrue(self.lifecycle.finalization_pending)
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])

    def test_startup_cleanup_attempts_bridge_when_camera_stop_raises(self):
        self.camera.fail_start = True
        self.camera.fail_stop = True
        with self.assertRaisesRegex(RuntimeError, "simulated startup failure"):
            self.lifecycle.start(7, "operator")
        self.assertFalse(self.bridge.running)
        self.assertTrue(self.lifecycle.finalization_pending)

    def test_startup_cleanup_flags_worker_that_stays_running(self):
        self.camera.fail_start = True
        self.camera.stuck = True
        with self.assertRaisesRegex(RuntimeError, "simulated startup failure"):
            self.lifecycle.start(7, "operator")
        self.assertTrue(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])
        self.assertIn("remains running", self.lifecycle.failure_reason)

    def test_normal_stop_and_new_reference(self):
        first = self.start()
        self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "completed")
        self.assertFalse(self.lifecycle.snapshot()["recovery_required"])
        self.assertFalse(self.lifecycle.snapshot()["finalization_pending"])
        self.assertIsNotNone(self.store.rows[first.session_ref]["ended_at"])
        second = self.start()
        self.assertNotEqual(first.session_ref, second.session_ref)

    def test_database_collision_retries_short_numeric_suffix_after_restart(self):
        # A fresh lifecycle simulates an application restart: only the
        # persistent store knows that the unsuffixed timestamp already exists.
        self.store.rows["SES-20261016-112633"] = {"status": "historical"}
        fixed_time = datetime(2026, 10, 16, 11, 26, 33, tzinfo=timezone.utc)
        with patch("sorting_lifecycle.datetime") as clock:
            clock.now.return_value = fixed_time
            context = self.lifecycle.start(7, "operator")
        self.assertEqual(context.session_ref, "SES-20261016-112633-01")
        clock.now.assert_called_once()
        self.assertEqual(clock.now.call_args.args[0].key, "Asia/Manila")

    def test_manual_stop_blocks_stale_auto_start_but_manual_start_resumes(self):
        first = self.start()
        self.lifecycle.pause_auto_start()
        self.lifecycle.stop()
        self.assertFalse(self.lifecycle.auto_start_allowed())
        with self.assertRaises(AutomaticStartSuppressed):
            self.lifecycle.start(7, "operator", automatic=True)
        self.assertEqual(self.camera.starts, 1)
        second = self.start()
        self.assertNotEqual(first.session_ref, second.session_ref)
        self.assertTrue(self.lifecycle.auto_start_allowed())
        self.assertEqual(self.camera.starts, 2)

    def test_delayed_old_ready_and_capture_cannot_change_new_session(self):
        first = self.start()
        self.lifecycle.stop()
        second = self.lifecycle.start(7, "operator")
        self.lifecycle.ready(first.to_dict())
        self.assertEqual(self.lifecycle.state, "starting")
        self.lifecycle.ready(second.to_dict())
        with self.assertRaises(LifecycleError):
            with self.lifecycle.persistence(first.to_dict()):
                self.fail("stale context accepted")

    def test_failed_status_not_overwritten_by_stop(self):
        context = self.start()
        self.lifecycle.fail(context.to_dict())
        self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "failed")

    def test_incomplete_work_blocks_new_session_and_marks_interrupted(self):
        self.start()
        self.incomplete = True
        self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "interrupted")
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])
        with self.assertRaises(LifecycleError):
            self.lifecycle.start(7, "operator")

    def test_late_failure_callback_after_completed_stop_is_ignored(self):
        context = self.start()
        self.lifecycle.stop()
        self.lifecycle.fail(context.to_dict(), reason="late shutdown callback")
        state = self.lifecycle.snapshot()
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["recovery_required"])
        self.assertEqual(self.store.rows[context.session_ref]["status"], "completed")

    def test_workers_still_alive_cannot_mark_completed(self):
        self.start()
        self.camera.stuck = True
        with self.assertRaises(LifecycleError):
            self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "stopping")

    def test_shutdown_database_failure_still_attempts_both_workers(self):
        self.start()
        self.store.transition = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database offline"))
        with self.assertRaises(LifecycleError):
            self.lifecycle.stop()
        self.assertFalse(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.assertEqual(self.lifecycle.state, "stopping")
        self.assertTrue(self.lifecycle.snapshot()["recovery_required"])

    def test_final_status_database_failure_keeps_recovery_state(self):
        self.start()
        transition = self.store.transition
        def fail_terminal(context, status, ended_at=None):
            if status == "completed":
                raise RuntimeError("terminal write failed")
            return transition(context, status, ended_at)
        self.store.transition = fail_terminal
        with self.assertRaisesRegex(LifecycleError, "terminal write failed"):
            self.lifecycle.stop()
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot["status"], "stopping")
        self.assertTrue(snapshot["recovery_required"])
        self.assertIn("terminal write failed", snapshot["failure_reason"])

    def test_shutdown_component_exception_does_not_skip_other_worker(self):
        self.start()
        self.camera.fail_stop = True
        with self.assertRaises(LifecycleError):
            self.lifecycle.stop()
        self.assertTrue(self.camera.running)
        self.assertFalse(self.bridge.running)
        self.assertEqual(self.lifecycle.state, "stopping")

    def test_failed_lifecycle_exposes_reason_and_recovery_state(self):
        context = self.start()
        self.lifecycle.fail(context.to_dict(), reason="simulated link loss")
        state = self.lifecycle.snapshot()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure_reason"], "simulated link loss")
        self.assertTrue(state["recovery_required"])

    def test_readiness_database_failure_is_visible_and_blocks_normal_running_state(self):
        context = self.lifecycle.start(7, "operator").to_dict()
        self.store.transition = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database offline"))
        self.lifecycle.ready(context)
        state = self.lifecycle.snapshot()
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["recovery_required"])
        self.assertIn("database offline", state["failure_reason"])

    def test_restart_reconciliation_preserves_historical_rows(self):
        self.store.rows = {"legacy": {"status": "historical"}, "crashed": {"status": "running"}}
        self.start()
        self.assertEqual(self.store.rows["legacy"]["status"], "historical")
        self.assertEqual(self.store.rows["crashed"]["status"], "interrupted")

    def test_stop_waits_for_confirmed_persistence(self):
        context = self.start()
        entered, release = Event(), Event()
        def save():
            with self.lifecycle.persistence(context.to_dict()):
                entered.set()
                release.wait(2)
        worker = Thread(target=save)
        worker.start()
        self.assertTrue(entered.wait(1))
        stopper = Thread(target=self.lifecycle.stop)
        stopper.start()
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(self.lifecycle.state, "completed")

    def test_failed_finalization_blocks_restart(self):
        context = self.start()
        with patch.object(self.store, "transition", side_effect=RuntimeError("storage offline")):
            with self.assertRaises(RuntimeError):
                self.lifecycle.fail(context.to_dict())
        self.camera.stop()
        self.bridge.stop()
        with self.assertRaises(LifecycleError):
            self.lifecycle.start(7, "operator")

    def test_lease_failure_prevents_reconciliation_and_parent_creation(self):
        with patch.object(self.lifecycle.lease, "acquire", side_effect=LifecycleError("station busy")):
            with self.assertRaises(LifecycleError):
                self.lifecycle.start(7, "operator")
        self.assertFalse(self.store.events)


if __name__ == "__main__":
    unittest.main()
