"""Loopback User Management lab with a disposable SQLite file and fake mail."""
import json
import os
import secrets
import socket
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import auth_test_support as loader
from flask import abort, jsonify, redirect, request, url_for
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
from models import AuditLog, PasswordResetAttempt, User
from security import csrf_token

auth = loader.auth
app = loader.app
db = auth.db
directory = Path(os.environ["EGGSOR_USER_MANAGEMENT_LAB_DIR"]).resolve()
assert directory.parent == Path(tempfile.gettempdir()).resolve()
assert directory.name.startswith("eggsort-user-management-lab-")
control_key = secrets.token_urlsafe(32)
admin_password = secrets.token_urlsafe(24)
staff_password = secrets.token_urlsafe(24)
mode = {"users": "normal"}
mail_attempts = []
app.config.update(
    TESTING=False,
    SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME="eggsort_user_management_lab",
    LOCAL_HARDWARE_ENABLED=False,
    AUTO_START_SORTING_ON_LOGIN=False,
    GOOGLE_CLIENT_ID="",
    GOOGLE_CLIENT_SECRET="",
    PUBLIC_BASE_URL="",
)

with app.app_context():
    db.engines[None].dispose()
    engine = create_engine(
        URL.create("sqlite", database=str(directory / "user-management.sqlite3")),
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    db._app_engines[app][None] = engine
    assert engine.url.get_backend_name() == "sqlite"
    assert Path(engine.url.database).resolve().parent == directory
    db.metadata.create_all(engine, tables=[User.__table__, AuditLog.__table__, PasswordResetAttempt.__table__])
    db.session.add_all([
        User(username="umadmin", password=generate_password_hash(admin_password),
             email="admin.um@example.invalid", display_name="Management Admin",
             role="admin", is_active=True, password_set=True),
        User(username="umstaff", password=generate_password_hash(staff_password),
             email="staff.um@example.invalid", display_name="Existing Staff",
             role="staff", is_active=True, password_set=True),
        User(username="umtarget", password=generate_password_hash(secrets.token_urlsafe(24)),
             email="target.um@example.invalid", display_name="Target Staff",
             avatar_url="https://images.example.invalid/target.png",
             role="staff", is_active=True, password_set=True),
    ])
    db.session.commit()


def fake_invitation_delivery(user, invite_url):
    mail_attempts.append({"username": user.username, "url_host": request.host})
    raise auth.InvitationDeliveryError("Test lab: external email delivery disabled.")


auth.send_staff_invitation = fake_invitation_delivery


def user_snapshot(user):
    password_scheme = user.password.split("$", 1)[0]
    if password_scheme.startswith("scrypt:"):
        password_scheme = "scrypt"
    elif password_scheme.startswith("pbkdf2:"):
        password_scheme = "pbkdf2:sha256"
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "is_active": user.is_active,
        "password_set": user.password_set,
        "invite_hash_present": bool(user.invite_token_hash),
        "invite_expires_at": user.invite_expires_at.isoformat() if user.invite_expires_at else None,
        "password_hash_scheme": password_scheme,
        "password_hash_length": len(user.password),
        "google_connected": bool(user.google_sub),
    }


@app.before_request
def isolate_user_management_lab():
    if request.remote_addr != "127.0.0.1":
        abort(403)
    allowed = {
        "static", "login", "logout", "dashboard", "user_management", "users_data",
        "create_user", "update_user", "regenerate_user_invite", "delete_user",
        "accept_invite", "lab_auth", "lab_logout", "lab_state", "lab_control",
    }
    if request.endpoint not in allowed:
        abort(403)
    if request.endpoint == "users_data" and mode["users"] == "error":
        return jsonify(error="Simulated disposable user-list outage."), 503


@app.get("/__um_auth/<role>")
def lab_auth(role):
    if request.args.get("key") != control_key or role not in {"admin", "staff"}:
        abort(403)
    username = "umadmin" if role == "admin" else "umstaff"
    user = User.query.filter_by(username=username).one()
    auth.sign_in_user(user, remember=False)
    return redirect(url_for("user_management"))


@app.get("/__um_logout")
def lab_logout():
    if request.args.get("key") != control_key:
        abort(403)
    from flask import session
    session.clear()
    return redirect(url_for("login"))


@app.get("/__um_state")
def lab_state():
    if request.headers.get("X-Lab-Secret") != control_key:
        abort(403)
    return jsonify(
        users=[user_snapshot(user) for user in User.query.order_by(User.id).all()],
        audit=[row.to_dict() for row in AuditLog.query.order_by(AuditLog.id).all()],
        mail_attempts=mail_attempts,
        mode=mode,
        csrf_token=csrf_token(),
    )


@app.post("/__um_control")
def lab_control():
    if request.headers.get("X-Lab-Secret") != control_key:
        abort(403)
    payload = request.get_json(silent=True) or {}
    command = payload.get("command")
    if command == "mode":
        assert payload.get("value") in {"normal", "error"}
        mode["users"] = payload["value"]
    elif command == "add_admin":
        db.session.add(User(
            username="umadmin2", password=generate_password_hash(secrets.token_urlsafe(24)),
            email="admin2.um@example.invalid", display_name="Second Admin",
            role="admin", is_active=True, password_set=True,
        ))
        db.session.commit()
    elif command == "expire_invite":
        user = User.query.filter_by(username=payload["username"]).one()
        user.invite_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()
    elif command == "disable_user":
        user = User.query.filter_by(username=payload["username"]).one()
        user.is_active = False
        db.session.commit()
    else:
        abort(400)
    return jsonify(ok=True)


server = make_server("127.0.0.1", 0, app, threaded=True)
print(json.dumps({
    "port": server.server_port,
    "control_key": control_key,
    "admin_username": "umadmin",
    "admin_password": admin_password,
    "staff_username": "umstaff",
    "staff_password": staff_password,
}), flush=True)
try:
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Outbound network forbidden in User Management lab")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Outbound network forbidden in User Management lab")),
    ):
        server.serve_forever()
finally:
    server.server_close()
    with app.app_context():
        db.engine.dispose()
