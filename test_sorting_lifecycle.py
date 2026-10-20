"""Lifecycle tests use memory stores and simulated components, never a database."""
import unittest
from unittest.mock import patch
from threading import Event, Thread
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
        self.stuck = False

    def start(self, *, context):
        assert context["session_ref"] in self.store.rows
        self.store.events.append("start-" + self.name)
        self.starts += 1
        self.running = True
        if self.fail_start:
            raise RuntimeError("simulated startup failure")

    def stop(self):
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

    def test_normal_stop_and_new_reference(self):
        first = self.start()
        self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "completed")
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
        with self.assertRaises(LifecycleError):
            self.lifecycle.start(7, "operator")

    def test_workers_still_alive_cannot_mark_completed(self):
        self.start()
        self.camera.stuck = True
        with self.assertRaises(LifecycleError):
            self.lifecycle.stop()
        self.assertEqual(self.lifecycle.state, "stopping")

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
