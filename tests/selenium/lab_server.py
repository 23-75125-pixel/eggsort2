"""Fresh subprocess per browser case. Never imports the production app directly."""
import json
import os
import secrets
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import dashboard_manual as lab
from flask import abort, jsonify, request
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

app = lab.prepare()
password = secrets.token_urlsafe(16)
control = secrets.token_urlsafe(24)
mail = []
stats_requests = []
with app.app_context():
    for user in lab.User.query.all():
        user.password = generate_password_hash(password)
    lab.db.session.add(lab.User(username="disabled", email="disabled@example.invalid",
        display_name="Disabled", role="staff", is_active=False, password_set=True,
        password=generate_password_hash(password)))
    lab.db.session.commit()
lab.auth.send_password_reset = lambda user, url: mail.append(url)
# Replace only the lab whitelist; retain real auth/CSRF and real application routes.
app.before_request_funcs[None] = [f for f in app.before_request_funcs[None] if f.__name__ != "lab_isolation"]

@app.before_request
def isolate():
    if request.remote_addr != "127.0.0.1": abort(403)
    if request.endpoint not in {"static", "login", "logout", "dashboard", "dashboard_stats",
        "clear_dashboard_audit_logs", "alerts_data", "egg_records", "egg_records_data",
        "forgot_password", "reset_password", "control_state"}: abort(403)
    if request.endpoint == "dashboard_stats":
        stats_requests.append(dict(request.args))
        if lab.mode["value"] == "fail":
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError("Simulated browser test outage")
        if lab.mode["value"] == "slow": lab.time.sleep(1)

@app.get("/__selenium_state")
def control_state():
    if request.headers.get("X-Lab-Secret") != control: abort(403)
    mode = request.args.get("mode")
    if mode:
        if mode not in {"normal", "fail", "slow"}: abort(400)
        lab.mode["value"] = mode
    with app.app_context():
        markers = lab.AuditLog.query.filter(lab.AuditLog.event_key.like("egg-capture:%")).count()
    return jsonify(reset_url=mail[-1] if mail else None, markers=markers,
                   stats_requests=stats_requests[-30:], mode=lab.mode["value"])

server = make_server("127.0.0.1", 0, app, threaded=False)
print(json.dumps({"port": server.server_port, "password": password, "control": control}), flush=True)
try:
    with patch("socket.socket.connect", side_effect=AssertionError("Outbound network forbidden")):
        server.serve_forever()
finally:
    server.server_close()
