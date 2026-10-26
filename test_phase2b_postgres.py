"""Opt-in PostgreSQL writes against one disposable local DB; never auto-run."""
from concurrent.futures import ThreadPoolExecutor
from getpass import getpass
from threading import Lock
from unittest import SkipTest, TestCase, main
from unittest.mock import patch
from uuid import uuid4
import os
import sys

from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from sorting_lifecycle import LifecycleError, SQLAlchemySessionStore, SortingLifecycle


class FakeLease:
    def acquire(self):
        pass


class DatabaseAwareComponent:
    """Mock hardware; verifies the session parent committed before startup."""
    def __init__(self, backend, name, *, fail_start=False):
        self.backend, self.name, self.fail_start = backend, name, fail_start
        self.running = False
        self.starts = 0
        self.lock = Lock()

    def start(self, *, context):
        with self.backend.app.app_context():
            parent = self.backend.db.session.get(
                self.backend.SortingSession, context["session_ref"])
            assert parent is not None and parent.status == "starting"
        with self.lock:
            self.starts += 1
            self.running = True
        if self.fail_start:
            raise RuntimeError("synthetic camera startup failure")

    def stop(self):
        with self.lock:
            self.running = False

    def status(self):
        with self.lock:
            return {"running": self.running}


class Phase2BPostgresTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("PHASE2B_RUN_POSTGRES_WRITE_TESTS") != "I_APPROVE_DISPOSABLE_WRITES":
            raise SkipTest("PostgreSQL write tests require explicit disposable-test execution")
        if "app" in sys.modules:
            raise RuntimeError("Run this test module alone in a fresh Python process")
        password = getpass("Disposable PostgreSQL runner password (hidden): ")
        if not password:
            raise RuntimeError("A private disposable runner password is required")
        url = URL.create(
            "postgresql+psycopg", username="eggsor_phase2b_runner",
            password=password, host="127.0.0.1", port=55432,
            database="eggsor_p2b_disposable",
        )
        password = ""
        environment = {
            "LOCAL_TEST_MODE": "0", "PHASE2B_WRITE_TEST_MODE": "1",
            "PHASE2B_WRITE_TEST_DATABASE_URL": url.render_as_string(hide_password=False),
            "SECRET_KEY": "phase2b-disposable-only-secret-000000000000",
        }
        context = patch.dict(os.environ, environment)
        context.start()
        cls.addClassCleanup(context.stop)
        import app as backend  # Import only after the disposable target is pinned.
        cls.backend = backend
        with backend.app.app_context():
            counts = [backend.db.session.query(model).count() for model in
                      (backend.User, backend.SortingSession, backend.EggRecord,
                       backend.AuditLog, backend.TraySizeAlert)]
        if counts != [0, 0, 0, 0, 0]:
            raise RuntimeError("Disposable fixtures are not empty; stop and inspect")
        backend.app.config["LIFECYCLE_WRITES_ENABLED"] = True
        backend.assert_lifecycle_writes_allowed()
        cls.addClassCleanup(backend.app.config.__setitem__, "LIFECYCLE_WRITES_ENABLED", False)

    def setUp(self):
        self.backend = type(self).backend
        with self.backend.app.app_context():
            user = self.backend.User(
                username="p2b_" + uuid4().hex,
                password="!synthetic-account-has-no-login-password",
                role="staff", is_active=True, password_set=False,
            )
            self.backend.db.session.add(user)
            self.backend.db.session.commit()
            self.user_id = user.id

    def coordinator(self, *, camera_failure=False):
        backend = self.backend
        store = SQLAlchemySessionStore(backend.app, backend.assert_lifecycle_writes_allowed)
        camera = DatabaseAwareComponent(backend, "camera", fail_start=camera_failure)
        bridge = DatabaseAwareComponent(backend, "bridge")
        lifecycle = SortingLifecycle(store, camera, bridge, lease=FakeLease())
        return lifecycle, camera, bridge

    def user(self):
        with self.backend.app.app_context():
            user = self.backend.User(
                username="p2b_" + uuid4().hex,
                password="!synthetic-account-has-no-login-password",
                role="staff", is_active=True, password_set=False,
            )
            self.backend.db.session.add(user)
            self.backend.db.session.commit()
            return user.id

    def test_01_session_owner_and_normal_stop(self):
        other_id = self.user()
        lifecycle, camera, bridge = self.coordinator()
        context = lifecycle.start(self.user_id, "synthetic operator")
        same = lifecycle.start(other_id, "another operator")
        self.assertEqual(same.session_ref, context.session_ref)
        self.assertEqual(camera.starts, 1)
        self.assertEqual(bridge.starts, 1)
        lifecycle.ready(context.to_dict())
        with self.backend.app.app_context():
            row = self.backend.db.session.get(self.backend.SortingSession, context.session_ref)
            self.assertEqual((row.user_id, row.status), (self.user_id, "running"))
            self.assertIsNotNone(row.started_at)
        lifecycle.stop()
        with self.backend.app.app_context():
            row = self.backend.db.session.get(self.backend.SortingSession, context.session_ref)
            self.assertEqual(row.status, "completed")
            self.assertIsNotNone(row.ended_at)

    def test_02_original_egg_session_and_atomic_audit(self):
        backend = self.backend
        lifecycle, _, _ = self.coordinator()
        first = lifecycle.start(self.user_id, "synthetic operator")
        lifecycle.ready(first.to_dict())
        capture = {**first.to_dict(), "capture_id": uuid4().hex}
        payload = dict(weight_grams=52, size="Medium", quality="Good",
                       confidence=0.91, session_ref=first.session_ref)
        with lifecycle.persistence(capture):
            egg_id = backend.persist_capture(capture, payload)
        with lifecycle.persistence(capture):
            self.assertIsNone(backend.persist_capture(capture, payload))
        lifecycle.stop()
        second = lifecycle.start(self.user_id, "synthetic operator")
        lifecycle.ready(second.to_dict())
        with self.assertRaises(LifecycleError):
            with lifecycle.persistence(capture):
                backend.persist_capture(capture, payload)
        with backend.app.app_context():
            egg = backend.db.session.get(backend.EggRecord, egg_id)
            key = f"egg-capture:{first.session_ref}:{capture['capture_id']}"
            audits = backend.AuditLog.query.filter_by(event_key=key).all()
            self.assertEqual(egg.session_ref, first.session_ref)
            self.assertEqual(len(audits), 1)
            self.assertEqual((audits[0].user_id, audits[0].actor), (self.user_id, "System"))
        lifecycle.stop()

    def test_03_failed_start_and_atomic_egg_failure(self):
        backend = self.backend
        failed, camera, bridge = self.coordinator(camera_failure=True)
        with self.assertRaisesRegex(RuntimeError, "synthetic camera"):
            failed.start(self.user_id, "synthetic operator")
        with backend.app.app_context():
            row = backend.SortingSession.query.filter_by(status="failed", user_id=self.user_id).one()
            self.assertIsNotNone(row.ended_at)
            self.assertFalse(camera.running or bridge.running)

        lifecycle, _, _ = self.coordinator()
        active = lifecycle.start(self.user_id, "synthetic operator")
        lifecycle.ready(active.to_dict())
        capture = {**active.to_dict(), "capture_id": uuid4().hex}
        key = f"egg-capture:{active.session_ref}:{capture['capture_id']}"
        bad_payload = dict(weight_grams=52, size="Medium", quality="Good",
                           confidence=0.91, session_ref="missing-session")
        with self.assertRaises(IntegrityError):
            with lifecycle.persistence(capture):
                backend.persist_capture(capture, bad_payload)
        with backend.app.app_context():
            self.assertEqual(backend.AuditLog.query.filter_by(event_key=key).count(), 0)
            self.assertEqual(backend.EggRecord.query.filter_by(session_ref="missing-session").count(), 0)
        lifecycle.stop()

    def test_04_concurrent_starts_commit_one_parent(self):
        backend = self.backend
        other_id = self.user()
        lifecycle, camera, bridge = self.coordinator()
        with ThreadPoolExecutor(max_workers=8) as executor:
            contexts = list(executor.map(
                lambda user_id: lifecycle.start(user_id, "synthetic operator"),
                [self.user_id, other_id] * 4,
            ))
        self.assertEqual(len({context.session_ref for context in contexts}), 1)
        self.assertEqual((camera.starts, bridge.starts), (1, 1))
        with backend.app.app_context():
            row = backend.db.session.get(backend.SortingSession, contexts[0].session_ref)
            self.assertEqual(row.user_id, contexts[0].operator_user_id)
        lifecycle.ready(contexts[0].to_dict())
        lifecycle.stop()

    def test_05_deletion_rules_preserve_egg_and_audit(self):
        backend = self.backend
        lifecycle, _, _ = self.coordinator()
        context = lifecycle.start(self.user_id, "synthetic operator")
        lifecycle.ready(context.to_dict())
        capture = {**context.to_dict(), "capture_id": uuid4().hex}
        with lifecycle.persistence(capture):
            egg_id = backend.persist_capture(capture, dict(
                weight_grams=51, size="Medium", quality="Good",
                confidence=0.9, session_ref=context.session_ref))
        lifecycle.stop()
        with backend.app.app_context():
            backend.db.session.delete(backend.db.session.get(backend.User, self.user_id))
            backend.db.session.commit()
            row = backend.db.session.get(backend.SortingSession, context.session_ref)
            egg = backend.db.session.get(backend.EggRecord, egg_id)
            audit = backend.AuditLog.query.filter_by(
                event_key=f"egg-capture:{context.session_ref}:{capture['capture_id']}").one()
            backend.db.session.refresh(row)
            backend.db.session.refresh(audit)
            self.assertIsNone(row.user_id)
            self.assertIsNone(audit.user_id)
            self.assertEqual(egg.session_ref, context.session_ref)
            backend.db.session.delete(row)
            with self.assertRaises(IntegrityError) as caught:
                backend.db.session.commit()
            self.assertEqual(caught.exception.orig.sqlstate, "23001")
            backend.db.session.rollback()

    def test_06_reconcile_only_incomplete_sessions(self):
        backend = self.backend
        lifecycle, camera, bridge = self.coordinator()
        context = lifecycle.start(self.user_id, "synthetic operator")
        lifecycle.ready(context.to_dict())
        camera.stop()
        bridge.stop()
        restarted, _, _ = self.coordinator()
        restarted.start(self.user_id, "synthetic operator")
        with backend.app.app_context():
            row = backend.db.session.get(backend.SortingSession, context.session_ref)
            self.assertEqual(row.status, "interrupted")
            self.assertIsNone(row.ended_at)  # Crash time is unknown.
        restarted.stop()

    def test_07_login_logout_audits_keep_actor_and_user_id(self):
        backend = self.backend
        with backend.app.app_context():
            for event_type in ("login", "logout"):
                backend.write_audit_log(
                    event_type, "synthetic authentication activity",
                    actor="synthetic operator", user_id=self.user_id,
                    event_key=f"synthetic-{event_type}:{uuid4().hex}",
                )
            rows = backend.AuditLog.query.filter_by(user_id=self.user_id).filter(
                backend.AuditLog.event_type.in_(("login", "logout"))).all()
            self.assertEqual({row.event_type for row in rows}, {"login", "logout"})
            self.assertTrue(all(row.actor == "synthetic operator" for row in rows))


if __name__ == "__main__":
    main()
