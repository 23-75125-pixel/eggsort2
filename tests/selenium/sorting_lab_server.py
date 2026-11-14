"""Loopback-only Sorting Sessions lab using the actual app routes and fake station workers."""
import base64
import json
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Keep one lifecycle module identity across the auth harness's temporary
# sys.modules patch, so fake-worker exceptions match the application's catches.
from sorting_lifecycle import LifecycleError, SQLAlchemySessionStore, SortingLifecycle
import auth_test_support as lab
from flask import abort, jsonify, request, session
from sqlalchemy import CheckConstraint, MetaData, create_engine
from sqlalchemy.engine import URL
from werkzeug.serving import make_server
from werkzeug.security import generate_password_hash
from models import AuditLog, EggRecord, SortingSession, TraySizeProgress, User


app = lab.app
auth = lab.auth
password = secrets.token_urlsafe(18)
control = secrets.token_urlsafe(24)
run_mode = {"value": "normal", "audit_fail": False}
JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/AP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAT8Af//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQIBAT8Af//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQMBAT8Af//Z")


class FakeLease:
    def acquire(self):
        return None


class FakeCamera:
    def __init__(self):
        self.running = False
        self.frame_ready = False
        self.fail_start = False
        self.fail_stop = False
        self.status_error = False
        self.start_count = 0
        self.stop_count = 0
        self.captures = []
        self.expired_seen = Event()
        self.expired_block = Event()
        self.expired_wait_count = 0
        self.sequence = 0
        self.egg_count = 0
        self.last_capture = None

    def start(self, *, context):
        self.start_count += 1
        self.context = dict(context)
        if self.fail_start:
            raise LifecycleError("Simulated camera startup failure.")
        self.running = True

    def stop(self):
        self.stop_count += 1
        if self.fail_stop:
            raise LifecycleError("Simulated camera worker cleanup failure.")
        self.running = False

    def status(self):
        if self.status_error:
            raise RuntimeError("Simulated camera status transport failure.")
        return {
            "running": self.running, "frame_ready": self.frame_ready,
            "detection_ready": self.frame_ready, "stream_fps": 20,
            "detection_fps": 0, "inference_ms": 0,
            "counts": ({self.last_capture["label"]: 1} if self.frame_ready and self.last_capture else {}),
            "detections": [], "total": 1 if self.frame_ready and self.last_capture else 0,
            "zone_quality": self.last_capture["label"] if self.frame_ready and self.last_capture else None,
            "zone_confidence": 0.96 if self.frame_ready and self.last_capture else 0.0,
            "egg_count": self.egg_count,
            "last_capture": self.last_capture, "model": None, "error": None,
        }

    def wait_for_frame(self, sequence):
        time.sleep(0.04)
        self.sequence = max(self.sequence, sequence) + 1
        return self.sequence, JPEG if self.frame_ready else None, self.running

    def wait_for_captured_quality(self, timeout=0.25, **kwargs):
        if self.captures:
            capture = self.captures[0]
            if isinstance(capture, BaseException):
                if "expired" in str(capture).lower():
                    self.expired_wait_count += 1
                    self.expired_seen.set()
                    if self.expired_wait_count > 1:
                        self.expired_block.wait(60)
                else:
                    self.captures.pop(0)
                raise capture
            return self.captures.pop(0)
        return None

    def set_lifecycle_handler(self, handler):
        self.lifecycle_handler = handler

    def set_capture_handler(self, handler):
        self.capture_handler = handler

    def set_reject_handler(self, handler):
        self.reject_handler = handler


class FakeBridge:
    def __init__(self):
        self.running = False
        self.connected = False
        self.fail_start = False
        self.fail_stop = False
        self.start_count = 0
        self.stop_count = 0
        self.latest_event = {"message": "Waiting for simulated controller."}
        self.diagnostics = {}
        self.reject_commands = []

    def start(self, *, context):
        self.start_count += 1
        self.context = dict(context)
        if self.fail_start:
            raise LifecycleError("Simulated ESP32 bridge startup failure.")
        self.running = True

    def stop(self):
        self.stop_count += 1
        if self.fail_stop:
            raise LifecycleError("Simulated ESP32 bridge cleanup failure.")
        self.running = False
        self.connected = False

    def status(self):
        return {
            "running": self.running, "connected": self.connected,
            "port": "COM-FAKE", "baud_rate": 115200,
            "controller": "FAKE ONLY", "firmware": "SIMULATION",
            "diagnostics": dict(self.diagnostics), "latest_event": self.latest_event,
            "error": None,
        }

    def publish_status(self, message, event_type=None):
        self.latest_event = {"message": str(message), "event_type": event_type}
        if event_type == "flow_error":
            self.diagnostics["flow_error"] = str(message)
            self.diagnostics["latest_sensor_event"] = str(message)
        else:
            self.diagnostics["flow_error"] = None
            self.diagnostics["latest_sensor_event"] = str(message)

    def publish_camera_capture(self, capture):
        self.latest_event = {"message": "Fake camera passage queued.", "capture_id": capture.get("capture_id")}

    def reject_egg(self, capture_id, quality):
        self.reject_commands.append((capture_id, quality))

    def set_event_handler(self, handler):
        self.event_handler = handler


camera = FakeCamera()
bridge = FakeBridge()

# Build the complete application schema in this subprocess's private in-memory
# SQLite engine. PostgreSQL-specific check expressions are not needed here.
metadata = MetaData()
for table in lab.db.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    for constraint in list(copied.constraints):
        if isinstance(constraint, CheckConstraint):
            copied.constraints.remove(constraint)
app.config.update(
    TESTING=False, SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME="eggsort_sorting_lab",
    LOCAL_HARDWARE_ENABLED=True, AUTO_START_SORTING_ON_LOGIN=False,
)
auth.CAMERA_SESSION = camera
auth.ESP32_BRIDGE = bridge
auth.assert_lifecycle_writes_allowed = lambda: None
auth.write_audit_log_original = auth.write_audit_log
auth.write_audit_log = auth.write_audit_log_original

def incomplete_work():
    return bool(auth.AUTOMATIC_EGG_CYCLES or auth.REJECTED_CAPTURES or auth.EGG_FLOW_PENDING)

auth.LIFECYCLE = SortingLifecycle(
    SQLAlchemySessionStore(app, lambda: None), camera, bridge, lease=FakeLease(),
    incomplete=incomplete_work, prepare=auth.prepare_station_generation,
    readiness_timeout_seconds=45,
    readiness_missing=auth._startup_readiness_missing,
)
camera.set_lifecycle_handler(auth.camera_lifecycle_event)
bridge.set_event_handler(auth.handle_esp32_event)

with app.app_context():
    # Flask serves concurrent browser polls on separate request threads. Use a
    # private temporary SQLite file so request threads share the same disposable
    # dataset without relying on thread-local in-memory SQLite connections.
    database_dir = tempfile.TemporaryDirectory(prefix="eggsort-sorting-lab-")
    database_path = Path(database_dir.name) / "sorting-lab.sqlite3"
    lab.db.engines[None].dispose()
    lab.db._app_engines[app][None] = create_engine(
        URL.create("sqlite", database=str(database_path)),
        connect_args={"check_same_thread": False},
    )
    if (lab.db.engine.url.get_backend_name() != "sqlite"
            or Path(lab.db.engine.url.database).resolve().parent != Path(database_dir.name).resolve()):
        raise RuntimeError("Sorting Selenium lab requires private temporary SQLite.")
    metadata.create_all(lab.db.engine)
    operator = User(
        username="sortadmin", email="sortadmin@example.invalid", role="admin",
        display_name="Sorting Lab Admin", is_active=True, password_set=True,
        password=generate_password_hash(password),
    )
    lab.db.session.add(operator)
    lab.db.session.flush()
    for size in ("Small", "Medium", "Large", "Extra Large", "Jumbo"):
        lab.db.session.add(TraySizeProgress(
            egg_size=size, opening_balance=0, confirmed_good_eggs_since_opening=0,
            completed_milestones=0, eggs_toward_next_milestone=0,
            opening_balance_set_at=auth.datetime.now(auth.timezone.utc),
            opening_balance_set_by_user_id=operator.id,
        ))
    lab.db.session.commit()


@app.before_request
def sorting_lab_isolation():
    if request.remote_addr != "127.0.0.1":
        abort(403)
    allowed = {
        "static", "login", "logout", "dashboard", "sorting_sessions",
        "start_camera", "stop_camera", "camera_status", "camera_feed",
        "hardware_status", "egg_records_data", "control_state", "control_action",
    }
    if request.endpoint not in allowed:
        abort(403)


@app.get("/__sorting_state")
def control_state():
    if request.headers.get("X-Lab-Secret") != control:
        abort(403)
    with app.app_context():
        sessions = [{"session_ref": row.session_ref, "status": row.status}
                    for row in SortingSession.query.order_by(SortingSession.started_at).all()]
        capture_markers = [row.event_key for row in AuditLog.query
                           .filter(AuditLog.event_key.like("egg-capture:%"))
                           .order_by(AuditLog.id).all()]
        eggs = [{"id": row.id, "capture_id": row.id, "session_ref": row.session_ref,
                 "weight_grams": row.weight_grams, "size": row.size,
                 "quality": row.quality, "quality_verified": row.quality_verified,
                 "confidence": row.confidence}
                for row in EggRecord.query.order_by(EggRecord.id).all()]
    return jsonify(
        lifecycle=auth.LIFECYCLE.snapshot(), camera=camera.status(), bridge=bridge.status(),
        sessions=sessions, eggs=eggs, capture_markers=capture_markers,
        sessions_count=len(sessions), camera_start_count=camera.start_count,
        bridge_start_count=bridge.start_count, camera_stop_count=camera.stop_count,
        bridge_stop_count=bridge.stop_count, reject_commands=bridge.reject_commands,
    )


class DeferredWorker:
    def __init__(self, target, args=(), kwargs=None, **_options):
        self.target, self.args, self.kwargs = target, args, kwargs or {}
        self.started = False
        self.finished = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and not self.finished

    def run_now(self):
        self.finished = True
        return self.target(*self.args, **self.kwargs)


@app.post("/__sorting_control")
def control_action():
    if request.headers.get("X-Lab-Secret") != control:
        abort(403)
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if command == "diagnostics":
        bridge.diagnostics = dict(data.get("diagnostics", {}))
        bridge.latest_event = {"message": data.get("message", "Simulated sensor event")}
        return jsonify(ok=True)
    if command == "role":
        user = lab.db.session.get(User, session["user_id"])
        user.role = data.get("role", "staff")
        session["role"] = user.role
        lab.db.session.commit()
        return jsonify(ok=True)
    if command == "hardware_enabled":
        app.config["LOCAL_HARDWARE_ENABLED"] = bool(data.get("enabled"))
        return jsonify(ok=True)
    if command == "configure":
        camera.fail_start = bool(data.get("camera_fail_start"))
        bridge.fail_start = bool(data.get("bridge_fail_start"))
        camera.fail_stop = bool(data.get("camera_fail_stop"))
        bridge.fail_stop = bool(data.get("bridge_fail_stop"))
        camera.status_error = bool(data.get("camera_status_error"))
        return jsonify(ok=True)
    if command == "ready":
        context = auth.LIFECYCLE.context
        if context is None:
            return jsonify(error="No active test lifecycle."), 409
        camera.frame_ready = bool(data.get("camera_frame", True))
        bridge.connected = bool(data.get("bridge_connected", True))
        metadata = context.to_dict()
        epoch = auth.BRIDGE_EPOCH or 1
        if auth.BRIDGE_EPOCH is None:
            auth.handle_esp32_event({**metadata, "type": "connection_open", "connection_epoch": epoch})
        auth.handle_esp32_event({**metadata, "type": "ready", "connection_epoch": epoch})
        auth.camera_lifecycle_event("ready", metadata)
        return jsonify(state=auth.LIFECYCLE.snapshot())
    if command == "timeout":
        context = auth.LIFECYCLE.context
        if context is None:
            return jsonify(error="No active test lifecycle."), 409
        camera.frame_ready = bool(data.get("camera_frame", False))
        bridge.connected = bool(data.get("bridge_connected", True))
        auth.BRIDGE_READY = bool(data.get("controller_ready", bridge.connected))
        auth.LIFECYCLE._expire_startup_readiness(context.to_dict())
        return jsonify(state=auth.LIFECYCLE.snapshot())
    if command == "connection_lost":
        context = auth.LIFECYCLE.context
        if context is None:
            return jsonify(error="No active test lifecycle."), 409
        bridge.connected = False
        auth.BRIDGE_READY = False
        auth.handle_esp32_event({**context.to_dict(), "type": "connection_lost",
                                 "connection_epoch": auth.BRIDGE_EPOCH, "reason": "Simulated USB loss."})
        return jsonify(state=auth.LIFECYCLE.snapshot())
    if command == "reconnect_ready":
        context = auth.LIFECYCLE.context
        bridge.connected = True
        camera.frame_ready = True
        auth.handle_esp32_event({**context.to_dict(), "type": "ready",
                                 "connection_epoch": auth.BRIDGE_EPOCH})
        auth.camera_lifecycle_event("ready", context.to_dict())
        return jsonify(state=auth.LIFECYCLE.snapshot())
    if command == "camera_display":
        camera.egg_count = int(data.get("egg_count", 1))
        camera.last_capture = {"capture_id": int(data.get("capture_id", 41)),
                               "label": data.get("quality", "good")}
        camera.frame_ready = True
        return jsonify(ok=True)
    if command == "audit_fail":
        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("Simulated audit store failure.")
        auth.write_audit_log = fail_audit
        return jsonify(ok=True)
    if command == "audit_restore":
        auth.write_audit_log = auth.write_audit_log_original
        return jsonify(ok=True)
    if command in {"weight_only", "fresh_good", "stale_good", "expired_capture", "duplicate_cycle"}:
        context = auth.LIFECYCLE.context
        if context is None or auth.LIFECYCLE.snapshot()["status"] != "running":
            return jsonify(error="A ready test session is required."), 409
        if command in {"fresh_good", "stale_good"}:
            camera_capture_id = 77 if command == "fresh_good" else 501
            camera.captures.append({**context.to_dict(), "capture_id": camera_capture_id,
                                    "label": "good", "confidence": 0.94})
        elif command == "expired_capture":
            camera.captures.append(auth.CameraSessionError(
                "Unmatched camera capture expired; clear the station before restarting."
            ))
        original_thread = auth.Thread
        if command != "expired_capture":
            auth.Thread = DeferredWorker
        try:
            cycle_id = 1
            event_types = ["egg_detected", "egg_complete", "sort_complete"]
            if command == "duplicate_cycle":
                event_types = ["egg_detected", "egg_complete", "sort_complete",
                               "egg_detected", "egg_complete", "sort_complete"]
            for kind in event_types:
                fields = {"weight_grams": 59, "size": "Medium"} if kind == "egg_complete" else {}
                auth.handle_esp32_event({**context.to_dict(), "type": kind,
                    "connection_epoch": auth.BRIDGE_EPOCH or 1, "cycle_id": cycle_id, **fields})
            worker = auth.AUTOMATIC_RECORD_WORKER
        finally:
            auth.Thread = original_thread
        if command == "expired_capture":
            camera.expired_seen.wait(3)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and auth.LIFECYCLE.snapshot()["status"] != "failed":
                time.sleep(0.01)
        elif worker is not None and hasattr(worker, "run_now"):
            try:
                worker.run_now()
            except Exception:
                app.logger.exception("Simulated automatic record worker failed")
        with app.app_context():
            rows = [{"id": row.id, "session_ref": row.session_ref,
                     "weight_grams": row.weight_grams, "size": row.size,
                     "quality": row.quality, "quality_verified": row.quality_verified,
                     "confidence": row.confidence}
                    for row in EggRecord.query.order_by(EggRecord.id).all()]
        with app.app_context():
            capture_markers = [row.event_key for row in AuditLog.query
                               .filter(AuditLog.event_key.like("egg-capture:%"))
                               .order_by(AuditLog.id).all()]
        return jsonify(lifecycle=auth.LIFECYCLE.snapshot(), eggs=rows,
                       capture_markers=capture_markers,
                       bridge_status=bridge.status(),
                       cycle_count=len(auth.AUTOMATIC_EGG_CYCLES),
                       camera_queue_count=len(camera.captures))
    return jsonify(error="Unknown test command."), 400


server = make_server("127.0.0.1", 0, app, threaded=True)
print(json.dumps({"port": server.server_port, "username": "sortadmin",
                  "password": password, "control": control}), flush=True)
try:
    with patch("socket.socket.connect", side_effect=AssertionError("Outbound network forbidden in Sorting lab")):
        server.serve_forever()
finally:
    server.server_close()
    with app.app_context():
        lab.db.engine.dispose()
    database_dir.cleanup()
