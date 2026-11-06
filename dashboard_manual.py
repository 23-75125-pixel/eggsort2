"""Loopback-only disposable browser lab using the actual Dashboard routes.

No .env, camera, serial, Supabase, Google or SMTP. PostgreSQL-only CHECK
constraints are omitted from a separate SQLite metadata copy, never models.py.
"""
import secrets
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flask import abort, redirect, render_template_string, request, session
from sqlalchemy import CheckConstraint, MetaData, event
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from auth_test_support import auth, app, db, User, AuditLog
from models import EggRecord, SortingSession, TraySizeProgress, TraySizeAlert

MANILA = timezone(timedelta(hours=8))
PASSWORD = "DashboardTest123!"
mode = {"value": "normal"}


def prepare():
    app.config.update(TESTING=False, SECRET_KEY=secrets.token_hex(32),
                      SESSION_COOKIE_NAME="eggsort_dashboard_lab",
                      LOCAL_HARDWARE_ENABLED=False, AUTO_START_SORTING_ON_LOGIN=False)
    auth.ESP32_BRIDGE.status.return_value = {"running": False, "connected": False,
                                            "controller": "DISPOSABLE TEST ONLY"}
    auth.stop_sorting_runtime = lambda: ({"running": False}, {"running": False})
    auth.send_password_reset = lambda *_a, **_k: abort(403)
    auth.send_staff_invitation = lambda *_a, **_k: abort(403)
    today = datetime.now(MANILA).date()
    with app.app_context():
        assert db.engine.url.get_backend_name() == "sqlite"
        assert db.engine.url.database in (None, "", ":memory:")
        metadata = MetaData()
        for table in db.metadata.sorted_tables:
            copied = table.to_metadata(metadata)
            for constraint in list(copied.constraints):
                if isinstance(constraint, CheckConstraint):
                    copied.constraints.remove(constraint)
        metadata.create_all(db.engine)
        # This process is always fresh. Refuse reseeding instead of deleting data.
        assert User.query.count() == 0
        admin = User(username="dashadmin", email="dashadmin@example.invalid", role="admin",
                     display_name="Dashboard Admin", is_active=True, password_set=True,
                     password=generate_password_hash(PASSWORD))
        staff = User(username="dashstaff", email="dashstaff@example.invalid", role="staff",
                     display_name="Dashboard Staff", is_active=True, password_set=True,
                     password=generate_password_hash(PASSWORD))
        db.session.add_all([admin, staff]); db.session.flush()
        station = SortingSession(session_ref="LAB-SESSION", user_id=admin.id,
                                 status="completed", started_at=datetime.combine(today-timedelta(days=8), datetime.min.time(), MANILA),
                                 ended_at=datetime.combine(today, datetime.max.time(), MANILA))
        db.session.add(station); db.session.flush()
        # (days before Manila today, Manila time, quality, size, weight)
        samples = [
            (7, "23:59:59", "Good", "Small", 50),
            (6, "00:00:00", "Good", "Small", 52),
            (6, "12:00:00", "Crack", "N/A", 0),
            (3, "10:00:00", "Good", "Medium", 58),
            (3, "10:01:00", "Good", "Large", 63),
            (3, "10:02:00", "Rotten", "N/A", 0),
            (1, "09:00:00", "Crack", "N/A", 0),
            (1, "09:01:00", "Rotten", "N/A", 0),
            (0, "00:00:00", "Good", "Extra Large", 68),
            (0, "12:00:00", "Good", "Jumbo", 74),
            (0, "23:59:59", "Good", "Medium", 59),
        ]
        for offset, clock, quality, size, weight in samples:
            stamp = datetime.fromisoformat(f"{today-timedelta(days=offset)}T{clock}").replace(tzinfo=MANILA)
            egg = EggRecord(weight_grams=weight, quality=quality, size=size, confidence=.95,
                            quality_verified=True, session_ref=station.session_ref,
                            sorted_at=stamp.astimezone(timezone.utc))
            db.session.add(egg); db.session.flush()
            db.session.add(AuditLog(event_type="egg_sorted" if quality=="Good" else "egg_rejected",
                actor="Lab seed", user_id=admin.id, description="Disposable fixture",
                event_key=f"egg-capture:LAB-SESSION:{egg.id}"))
        # Historical cumulative balances intentionally differ from filtered totals.
        for size, opening, count in [("Small",28,3),("Medium",0,2),("Large",29,2),
                                     ("Extra Large",0,1),("Jumbo",None,0)]:
            total = (opening or 0) + count
            db.session.add(TraySizeProgress(egg_size=size, opening_balance=opening,
                opening_balance_set_at=datetime.now(timezone.utc) if opening is not None else None,
                confirmed_good_eggs_since_opening=count, completed_milestones=total//30,
                eggs_toward_next_milestone=total%30 if opening is not None else None))
        db.session.add(AuditLog(event_type="lab_seed", actor="Lab", description="Disposable Dashboard dataset"))
        db.session.commit()
        # Permit auth audit inserts but reject deletion of operational history.
        @event.listens_for(db.engine, "before_cursor_execute")
        def forbid_deletion(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith(("DELETE", "DROP", "ALTER", "TRUNCATE")):
                raise AssertionError("Destructive SQL is forbidden in the Dashboard lab")

    @app.before_request
    def lab_isolation():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)
        if request.endpoint not in {"static", "login", "logout", "dashboard", "dashboard_stats",
                                    "clear_dashboard_audit_logs", "alerts_data", "egg_records",
                                    "egg_records_data", "dashboard_lab_controls"}:
            abort(403)
        if request.endpoint == "dashboard_stats" and "user_id" in session:
            if mode["value"] == "fail":
                raise SQLAlchemyError("Simulated Dashboard database failure")
            if mode["value"] == "slow": time.sleep(2)

    @app.route("/__dashboard_lab", methods=["GET", "POST"])
    @auth.admin_required
    def dashboard_lab_controls():
        if request.method == "POST":
            selected = request.form.get("mode")
            if selected not in {"normal", "fail", "slow"}: abort(400)
            mode["value"] = selected
            return redirect("/__dashboard_lab")
        markers = AuditLog.query.filter(AuditLog.event_key.like("egg-capture:%")).count()
        return render_template_string('''<!doctype html><title>Dashboard lab controls</title>
<h1>Disposable Dashboard lab — no hardware</h1><p>Mode: {{ mode }}. Capture markers: {{ markers }} (expected 11).</p>
<p>Manila today: {{ today }}. Default start: {{ start }}. Wider start: {{ wider }}. Empty date: {{ empty }}.</p>
<form method="post"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
<select name="mode"><option>normal</option><option>fail</option><option>slow</option></select><button>Set mode</button></form>
<p><a href="/dashboard">Actual Dashboard</a> | <a href="/egg-records">Actual Egg Records</a></p>''',
            mode=mode["value"], markers=markers, today=today, start=today-timedelta(days=6),
            wider=today-timedelta(days=7), empty=today-timedelta(days=2))
    return app


if __name__ == "__main__":
    prepare()
    print("Disposable Dashboard: http://127.0.0.1:5012/login")
    print("Admin: dashadmin | Staff: dashstaff | Password:", PASSWORD)
    print("Admin lab controls: http://127.0.0.1:5012/__dashboard_lab")
    # Incoming loopback connections use accept(); all Python outbound connects fail.
    with patch("socket.socket.connect", side_effect=AssertionError("Outbound network disabled in lab")):
        app.run(host="127.0.0.1", port=5012, debug=False, use_reloader=False, threaded=False)
