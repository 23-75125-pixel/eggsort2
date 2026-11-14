"""Loopback Reports lab with synthetic rows and a private SQLite database."""
import json
import secrets
import socket
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
# Preserve the real lifecycle module identity across the auth harness import.
import sorting_lifecycle  # noqa: F401
import auth_test_support as lab
from flask import abort, jsonify, request
from sqlalchemy import create_engine, event, func
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
from models import User, SortingSession, EggRecord, AuditLog, PasswordResetAttempt

app = lab.app
secret = secrets.token_urlsafe(32)
password = secrets.token_urlsafe(20)
temp = tempfile.TemporaryDirectory(prefix="eggsort-reports-lab-")
database = Path(temp.name) / "reports.sqlite3"
anchor = datetime.now(ZoneInfo("Asia/Manila")).date()
test_day = date(2026, 10, 15)
mode = {"value": "normal"}
requests = []
app.config.update(
    TESTING=False, SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME="eggsort_reports_lab", LOCAL_HARDWARE_ENABLED=False,
    AUTO_START_SORTING_ON_LOGIN=False,
)

with app.app_context():
    lab.db.engines[None].dispose()
    engine = create_engine(URL.create("sqlite", database=str(database)), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    lab.db._app_engines[app][None] = engine
    assert engine.url.get_backend_name() == "sqlite"
    assert Path(engine.url.database).resolve().parent == Path(temp.name).resolve()
    lab.db.metadata.create_all(engine, tables=[
        User.__table__, SortingSession.__table__, EggRecord.__table__,
        AuditLog.__table__, PasswordResetAttempt.__table__,
    ])
    for username, role in [("reportadmin", "admin"), ("reportstaff", "staff")]:
        lab.db.session.add(User(
            username=username, email=username + "@example.invalid", role=role,
            is_active=True, password_set=True, password=generate_password_hash(password),
        ))
    lab.db.session.commit()


def utc(day_offset=0, hour=0, minute=0, second=0, microsecond=0):
    day = test_day + timedelta(days=day_offset)
    return datetime.combine(day, time(hour, minute, second, microsecond), tzinfo=timezone.utc)


local_week_start_utc = datetime(2026, 10, 19, tzinfo=ZoneInfo("Asia/Manila")).astimezone(timezone.utc)
local_month_start_utc = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Manila")).astimezone(timezone.utc)


def insert_record(record_id, quality, verified, at, session_ref="REPORT-A", size="Medium"):
    if lab.db.session.get(SortingSession, session_ref) is None:
        lab.db.session.add(SortingSession(session_ref=session_ref, status="historical"))
        lab.db.session.flush()
    lab.db.session.add(EggRecord(
        id=record_id, weight_grams=59, size=size, quality=quality,
        confidence=.94 if verified is True else 0,
        quality_verified=verified, sorted_at=at, session_ref=session_ref,
    ))


with app.app_context():
    # Explicit, independently countable fixture. Base-day rows exercise every
    # quality and verification state; other rows span daily/weekly/monthly bins.
    rows = [
        (1, "Good", True, utc(0, 0), "REPORT-A"),
        (2, "Good", False, utc(0, 8), "REPORT-A"),
        (3, "Good", None, utc(0, 15), "REPORT-A"),
        (4, "Crack", True, utc(0, 17), "REPORT-B"),
        (5, "Crack", False, utc(0, 18), "REPORT-B"),
        (6, "Rotten", True, utc(1, 0), "REPORT-B"),
        (7, "Rotten", None, utc(1, 1), "REPORT-B"),
        (8, "Not Classified", False, utc(1, 2), "REPORT-B"),
        (9, "Not Classified", None, utc(1, 3), "REPORT-B"),
        (10, "Bruised", True, utc(1, 4), "REPORT-B"),
        (11, "Good", True, utc(1, 5), "REPORT-C"),
        (12, "Good", True, local_week_start_utc - timedelta(microseconds=1), "REPORT-C"),
        (13, "Crack", None, local_week_start_utc, "REPORT-C"),
        (14, "Good", True, local_month_start_utc - timedelta(microseconds=1), "REPORT-C"),
        (15, "Good", True, local_month_start_utc, "REPORT-C"),
        (16, "Good", True, utc(-1, 23, 59, 59, 999999), "REPORT-A"),
        (17, "Good", True, utc(1, 23, 59, 59, 999999), "REPORT-C"),
        (18, "Good", True, utc(0, 23, 59, 59, 999999), "REPORT-A"),
    ]
    local_midnight_utc = datetime.combine(test_day, time.min, tzinfo=ZoneInfo("Asia/Manila")).astimezone(timezone.utc)
    rows.extend([
        (19, "Good", True, local_midnight_utc - timedelta(microseconds=1), "REPORT-TZ"),
        (20, "Good", True, local_midnight_utc, "REPORT-TZ"),
        (21, "Good", True, local_midnight_utc + timedelta(days=1) - timedelta(microseconds=1), "REPORT-TZ"),
        (22, "Good", True, local_midnight_utc + timedelta(days=1), "REPORT-TZ"),
    ])
    for record_id, quality, verified, at, session_ref in rows:
        insert_record(record_id, quality, verified, at, session_ref)
    lab.db.session.commit()


@app.before_request
def isolate_reports_lab():
    if request.remote_addr != "127.0.0.1":
        abort(403)
    allowed = {
        "static", "login", "logout", "dashboard", "reports", "reports_data",
        "dashboard_stats", "alerts_data", "lab_control", "lab_state",
    }
    if request.endpoint not in allowed:
        abort(403)
    if request.endpoint == "reports_data":
        requests.append(dict(request.args))
        if mode["value"] == "database_error":
            raise SQLAlchemyError("Simulated private Reports database outage")
        if mode["value"] == "html_error":
            return "<html>Simulated proxy response</html>", 502


def authorized():
    if request.headers.get("X-Lab-Secret") != secret:
        abort(403)


@app.get("/__reports_state")
def lab_state():
    authorized()
    return jsonify(requests=requests[-100:], count=EggRecord.query.count(), mode=mode["value"])


@app.post("/__reports_control")
def lab_control():
    authorized()
    payload = request.get_json()
    command = payload["command"]
    if command == "mode":
        assert payload["value"] in {"normal", "database_error", "html_error"}
        mode["value"] = payload["value"]
    elif command == "clear":
        EggRecord.query.delete()
        lab.db.session.commit()
    elif command == "add":
        next_id = (lab.db.session.query(func.max(EggRecord.id)).scalar() or 0) + 1
        insert_record(next_id, "Good", True, utc(17, 12), "REPORT-LIVE")
        lab.db.session.commit()
    elif command == "disable":
        user = User.query.filter_by(username=payload.get("username", "reportadmin")).one()
        user.is_active = False
        lab.db.session.commit()
    elif command == "hostile_quality":
        record = lab.db.session.get(EggRecord, 10)
        record.quality = "<svg onload=alert(1)>"
        lab.db.session.commit()
    else:
        abort(400)
    return jsonify(ok=True)


server = make_server("127.0.0.1", 0, app, threaded=True)
print(json.dumps({
    "port": server.server_port, "username": "reportadmin", "staff": "reportstaff",
    "password": password, "control": secret, "today": anchor.isoformat(),
    "test_day": test_day.isoformat(), "start": (anchor - timedelta(days=29)).isoformat(),
    "records": 22,
}), flush=True)
try:
    with (patch("socket.socket.connect", side_effect=AssertionError("Outbound network forbidden")),
          patch("socket.socket.connect_ex", side_effect=AssertionError("Outbound network forbidden"))):
        server.serve_forever()
finally:
    server.server_close()
    with app.app_context():
        lab.db.engine.dispose()
    temp.cleanup()
