"""Station lifecycle; injected persistence/hardware allow database-free testing."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock, RLock, Timer
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo
import os
import tempfile
import logging


class LifecycleError(RuntimeError):
    pass


class AutomaticStartSuppressed(LifecycleError):
    pass


class SessionReferenceConflict(LifecycleError):
    """The database already contains a candidate session reference."""
    pass


MANILA_TIME_ZONE = ZoneInfo("Asia/Manila")
SESSION_REFERENCE_ATTEMPTS = 100
_LIFECYCLE_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionContext:
    session_ref: str
    runtime_generation: str
    operator_user_id: int
    operator_actor: str

    def to_dict(self):
        return asdict(self)

    def matches(self, event):
        return (event.get("session_ref") == self.session_ref
                and event.get("runtime_generation") == self.runtime_generation)


class StationLease:
    """Nonblocking OS lock, outside the repo; held for the owning process lifetime.

    One physical station on one computer is supported. No distributed ownership.
    """
    def __init__(self):
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return
        path = Path(tempfile.gettempdir()) / "eggsor-station-lifecycle.lock"
        handle = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise LifecycleError("Another process owns the sorting station.") from None
        self.handle = handle


class SortingLifecycle:
    def __init__(self, store, camera, bridge, *, lease=None, incomplete=lambda: False,
                 prepare=lambda: None, readiness_timeout_seconds=45,
                 readiness_missing=None):
        self.store, self.camera, self.bridge = store, camera, bridge
        self.lease = lease or StationLease()
        self.incomplete = incomplete
        self.prepare = prepare
        self.operation_lock = Lock()
        self.condition = Condition(RLock())
        self.write_lock = RLock()
        self.context = None
        self.state = "idle"
        self.inflight = 0
        self.reconciled = False
        self.finalization_pending = False
        self.auto_start_paused = False
        self.failure_reason = None
        self.readiness_timeout_seconds = max(0.01, float(readiness_timeout_seconds))
        self.readiness_missing = readiness_missing or self._default_readiness_missing
        self._readiness_timer = None

    def _default_readiness_missing(self):
        missing = []
        if not self.camera.status().get("frame_ready"):
            missing.append("first camera frame")
        bridge = self.bridge.status()
        if not bridge.get("connected"):
            missing.append("ESP32 bridge connection")
        if not bridge.get("ready"):
            missing.append("ESP32 controller readiness")
        return missing

    def _cancel_readiness_timer_locked(self):
        timer, self._readiness_timer = self._readiness_timer, None
        if timer is not None:
            timer.cancel()

    def _expire_startup_readiness(self, metadata):
        with self.operation_lock:
            with self.condition:
                if not (self.context and self.context.matches(metadata)
                        and self.state == "starting"):
                    return
            try:
                missing = list(self.readiness_missing() or [])
            except Exception as exc:
                _LIFECYCLE_LOG.warning(
                    "Startup readiness status check failed (%s).",
                    type(exc).__name__,
                )
                missing = ["readiness status check error"]
            detail = ", ".join(missing) if missing else "camera and ESP32 readiness confirmation"
            reason = f"Startup readiness timed out waiting for {detail}."
            errors = []
            with self.condition:
                if not (self.context and self.context.matches(metadata)
                        and self.state == "starting"):
                    return
                context = self.context
                self._readiness_timer = None
                self.state = "failed"
                self.finalization_pending = True
                self.failure_reason = reason
                try:
                    self.store.transition(context, "failed", ended_at=datetime.now(timezone.utc))
                except Exception as exc:
                    errors.append(f"Unable to persist startup timeout: {exc}")
                else:
                    self.finalization_pending = False

            # A failed database update or one worker stop must not skip the
            # other worker. Worker shutdown does not prove physical motion stopped.
            for component in (self.camera, self.bridge):
                try:
                    component.stop()
                except Exception as exc:
                    errors.append(f"Worker cleanup failed: {exc}")
            for component in (self.camera, self.bridge):
                try:
                    if component.status().get("running"):
                        errors.append("A software worker remains running after startup timeout cleanup.")
                except Exception as exc:
                    errors.append(f"Unable to verify software worker state: {exc}")
            if errors:
                with self.condition:
                    self.finalization_pending = True
                    self.failure_reason = "; ".join([reason, *errors])
    @staticmethod
    def _session_ref_for_attempt(started_at, attempt):
        """Keep normal references readable; suffix only proven collisions."""
        base = "SES-" + started_at.strftime("%Y%m%d-%H%M%S")
        return base if attempt == 0 else f"{base}-{attempt:02d}"

    def auto_start_allowed(self):
        with self.operation_lock:
            return not self.auto_start_paused

    def pause_auto_start(self):
        with self.operation_lock:
            self.auto_start_paused = True

    def snapshot(self):
        with self.condition:
            return {
                "status": self.state,
                "recovery_required": self.finalization_pending or self.state in {"failed", "stopping", "interrupted"},
                "finalization_pending": self.finalization_pending,
                "failure_reason": self.failure_reason,
                **(self.context.to_dict() if self.context else {}),
            }

    def accepts(self, metadata, *, saving=False):
        with self.condition:
            # Finish writes already tied to this runtime even if hardware has
            # failed or the operator stopped the station. This lets queued egg
            # records drain into their original sorting session before a new
            # session can start.
            states = {"running", "stopping", "failed", "interrupted"} if saving else {"running"}
            return bool(self.context and self.context.matches(metadata) and self.state in states)

    def start(self, user_id, actor, *, automatic=False):
        with self.operation_lock:
            if automatic and self.auto_start_paused:
                raise AutomaticStartSuppressed("Automatic sorting startup is paused after a manual stop.")
            with self.condition:
                if self.state in {"starting", "running"}:
                    if not automatic:
                        self.auto_start_paused = False
                    return self.context
                if self.state == "stopping" or self.incomplete():
                    raise LifecycleError("Incomplete sorting work requires recovery before restart.")
                if self.finalization_pending:
                    raise LifecycleError("Session finalization requires recovery before restart.")
                if self.camera.status().get("running") or self.bridge.status().get("running"):
                    raise LifecycleError("Previous workers must stop before a new session.")
            self.lease.acquire()
            if not self.reconciled:
                self.store.reconcile()
                self.reconciled = True
            # The primary-key insert is the final uniqueness authority. This
            # remains correct after an application restart, when no in-memory
            # counter from the previous process is available.
            started_at = datetime.now(MANILA_TIME_ZONE)
            context = None
            for attempt in range(SESSION_REFERENCE_ATTEMPTS):
                context = SessionContext(
                    self._session_ref_for_attempt(started_at, attempt),
                    uuid4().hex,
                    user_id,
                    actor,
                )
                try:
                    # Parent commit precedes any component startup.
                    self.store.create(context)
                    break
                except SessionReferenceConflict:
                    context = None
            if context is None:
                raise LifecycleError(
                    "Unable to allocate a unique session reference; try again."
                )
            with self.condition:
                self.context, self.state = context, "starting"
                self.failure_reason = None
            try:
                self.prepare()
                self.bridge.start(context=context.to_dict())
                self.camera.start(context=context.to_dict())
                if self.camera.status().get("error"):
                    raise LifecycleError("Camera startup failed.")
                with self.condition:
                    if self.state == "starting" and self.context.matches(context.to_dict()):
                        self._readiness_timer = Timer(
                            self.readiness_timeout_seconds,
                            self._expire_startup_readiness,
                            args=(context.to_dict(),),
                        )
                        self._readiness_timer.daemon = True
                        self._readiness_timer.start()
                if not automatic:
                    self.auto_start_paused = False
                return context
            except Exception as startup_error:
                with self.condition:
                    self._cancel_readiness_timer_locked()
                cleanup_errors = []
                try:
                    self.fail(context.to_dict(), reason=str(startup_error))
                except Exception as exc:
                    cleanup_errors.append(exc)
                # Persistence and each worker cleanup are independent. A
                # failed DB transition must never skip attempts to stop either
                # software worker. This does not prove physical hardware state.
                for component in (self.camera, self.bridge):
                    try:
                        component.stop()
                    except Exception as exc:
                        cleanup_errors.append(exc)
                for component in (self.camera, self.bridge):
                    try:
                        if component.status().get("running"):
                            cleanup_errors.append(LifecycleError(
                                "A software worker remains running after startup cleanup."
                            ))
                    except Exception as exc:
                        cleanup_errors.append(exc)
                if cleanup_errors:
                    with self.condition:
                        self.finalization_pending = True
                        self.failure_reason = "; ".join(
                            [str(startup_error), *(str(error) for error in cleanup_errors)]
                        )
                raise

    def ready(self, metadata):
        with self.condition:
            if self.context and self.context.matches(metadata) and self.state == "starting":
                try:
                    self.store.transition(self.context, "running")
                except Exception as exc:
                    self.state = "failed"
                    self.finalization_pending = True
                    self.failure_reason = f"Unable to persist session readiness: {exc}"
                    self._cancel_readiness_timer_locked()
                    return
                self.state = "running"
                self._cancel_readiness_timer_locked()

    def fail(self, metadata, reason=None):
        with self.condition:
            if self.context and self.context.matches(metadata) and self.state in {"starting", "running", "stopping"}:
                # Keep finalization pending if storage is unavailable.
                self.state = "failed"
                self.finalization_pending = True
                self.failure_reason = str(reason) if reason else self.failure_reason
                self._cancel_readiness_timer_locked()
                try:
                    self.store.transition(self.context, "failed", ended_at=datetime.now(timezone.utc))
                except Exception:
                    raise
                else:
                    self.finalization_pending = False

    @contextmanager
    def persistence(self, metadata):
        # Serialize capture deduplication; no stop/start state lock during DB work.
        with self.write_lock:
            with self.condition:
                if not self.accepts(metadata, saving=True):
                    raise LifecycleError("Stale or inactive egg-session context.")
                self.inflight += 1
            try:
                yield
            finally:
                with self.condition:
                    self.inflight -= 1
                    self.condition.notify_all()

    def stop(self, timeout=10):
        with self.operation_lock:
            with self.condition:
                if not self.context:
                    return
                self._cancel_readiness_timer_locked()
                context = self.context
                if self.state in {"completed", "interrupted"}:
                    return
                failed = self.state == "failed"
                if not failed:
                    self.state = "stopping"
                self.finalization_pending = True
            errors = []
            if not failed:
                try:
                    self.store.transition(context, "stopping")
                except Exception as exc:
                    errors.append(exc)
            # Always attempt both software cleanups, even if persistence or
            # the other component fails. Software disconnect is not proof
            # that motors or servos have physically stopped.
            for component in (self.camera, self.bridge):
                try:
                    component.stop()
                except Exception as exc:
                    errors.append(exc)
            deadline = monotonic() + timeout
            with self.condition:
                while self.inflight:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        errors.append(LifecycleError("Persistence is still running; shutdown is incomplete."))
                        break
                    self.condition.wait(remaining)
                for component in (self.camera, self.bridge):
                    try:
                        if component.status().get("running"):
                            errors.append(LifecycleError("Workers are still running; shutdown is incomplete."))
                    except Exception as exc:
                        errors.append(exc)
                if errors:
                    self.finalization_pending = True
                    detail = "; ".join(str(error) for error in errors)
                    self.failure_reason = detail
                    raise LifecycleError(detail)
                terminal = "failed" if self.state == "failed" else "interrupted" if self.incomplete() else "completed"
                self.finalization_pending = True
                try:
                    self.store.transition(context, terminal, ended_at=datetime.now(timezone.utc))
                except Exception as exc:
                    self.failure_reason = f"Unable to persist session finalization: {exc}"
                    raise LifecycleError(self.failure_reason) from exc
                self.finalization_pending = False
                self.state = terminal


class SQLAlchemySessionStore:
    """No connection at construction. Each call has its own application context."""
    def __init__(self, app, write_guard):
        self.app, self.write_guard = app, write_guard

    def create(self, context):
        from extensions import db
        from models import SortingSession, User
        from sqlalchemy.exc import IntegrityError
        self.write_guard()
        with self.app.app_context():
            try:
                user = db.session.get(User, context.operator_user_id)
                if not user or not user.is_active:
                    raise LifecycleError("An active authenticated operator is required.")
                db.session.add(SortingSession(session_ref=context.session_ref,
                    user_id=user.id, started_at=datetime.now(timezone.utc), status="starting"))
                db.session.commit()
            except IntegrityError as exc:
                db.session.rollback()
                # This insert only introduces a new sorting_session row, so a
                # unique violation is a session-ref collision, not a reason
                # to overwrite or modify an existing record.
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise SessionReferenceConflict(
                        "The session reference already exists."
                    ) from None
                raise
            except Exception:
                db.session.rollback()
                raise

    def transition(self, context, status, ended_at=None):
        from extensions import db
        from models import SortingSession
        self.write_guard()
        with self.app.app_context():
            try:
                row = db.session.query(SortingSession).filter_by(session_ref=context.session_ref).with_for_update().one()
                if row.status in {"completed", "interrupted", "historical", "unknown"}:
                    return
                if row.status == "failed" and status != "failed":
                    return
                row.status = status
                if ended_at is not None and row.ended_at is None:
                    row.ended_at = ended_at
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise

    def reconcile(self):
        from extensions import db
        from models import SortingSession
        self.write_guard()
        with self.app.app_context():
            try:
                # Only after exclusive station ownership. Exact crash time unknown.
                for row in SortingSession.query.filter(SortingSession.status.in_(("starting", "running", "stopping"))).all():
                    row.status = "interrupted"
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
