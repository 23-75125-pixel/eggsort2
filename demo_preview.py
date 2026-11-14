"""Isolated, in-memory browser preview for the EGGSor+ tray tracking UI.

Run with: .venv\\Scripts\\python.exe demo_preview.py

This module intentionally does not import app.py, models.py, database.py, or
read database-related environment variables. All API data exists only in this
process and disappears when it stops.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for


ROOT = Path(__file__).resolve().parent
SIZES = ("Small", "Medium", "Large", "Extra Large", "Jumbo")
TARGET = 30

# Seed examples cover a near milestone, an overflow, multiple milestones, and
# an uninitialized balance. These counts are synthetic and never persisted.
_tray_state = {
    "Small": {"opening_balance": 0, "sorted_good": 29},
    "Medium": {"opening_balance": 12, "sorted_good": 23},
    "Large": {"opening_balance": 29, "sorted_good": 32},
    "Extra Large": {"opening_balance": None, "sorted_good": 0},
    "Jumbo": {"opening_balance": 10, "sorted_good": 20},
}
_alerts = [
    {
        "id": "demo-size-1", "egg_size": "Jumbo", "milestone_number": 1,
        "title": "Jumbo tray completed", "message": "Jumbo reached 30 verified Good eggs.",
        "session_ref": "DEMO-SESSION", "is_read": False,
        "created_at": "2026-10-21T09:15:00+08:00",
    },
    {
        "id": "demo-size-2", "egg_size": "Medium", "milestone_number": 1,
        "title": "Medium tray completed", "message": "Medium reached 30 verified Good eggs.",
        "session_ref": "DEMO-SESSION", "is_read": True,
        "created_at": "2026-10-20T15:30:00+08:00",
    },
]
_audit = []
_lock = RLock()

app = Flask(
    "eggsor_local_demo",
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)
app.secret_key = "local-preview-only"
app.config.update(TESTING=False, DEBUG=False)


def _csrf_token() -> str:
    return "demo-csrf-token"


@app.context_processor
def _demo_template_context():
    return {
        "csrf_token": _csrf_token,
        "current_user_role": "admin",
        "current_user_avatar_url": None,
        "username": "Demo Admin",
        "error": None,
        "notice": None,
    }


def _progress(size: str) -> dict:
    state = _tray_state[size]
    opening = state["opening_balance"]
    configured = opening is not None
    total = (opening or 0) + state["sorted_good"]
    return {
        "egg_size": size,
        "opening_balance_configured": configured,
        "opening_balance": opening,
        "eggs_toward_next_milestone": total % TARGET if configured else None,
        "completed_milestones": total // TARGET if configured else None,
        "eggs_remaining": TARGET - (total % TARGET) if configured else None,
        "verified_good_eggs": state["sorted_good"],
        "target_per_tray": TARGET,
    }


def _unread_count() -> int:
    return sum(not row["is_read"] for row in _alerts)


@app.get("/")
def index():
    return redirect(url_for("dashboard_preview"))


@app.get("/dashboard")
def dashboard_preview():
    return render_template("dashboard.html")


@app.get("/alerts")
def alerts_preview():
    return render_template("alerts.html")


@app.get("/user-management")
def admin_preview():
    return render_template("user_management.html")


@app.post("/logout")
def logout():
    return redirect(url_for("dashboard_preview"))


@app.get("/sorting-sessions")
@app.get("/egg-records")
@app.get("/reports")
def preview_placeholder():
    return redirect(url_for("dashboard_preview"))


@app.get("/api/dashboard/stats")
def dashboard_stats():
    today = date.today()
    trend = [
        {"date": (today - timedelta(days=offset)).isoformat(), "count": [0, 3, 0, 8, 2, 0, 5][6-offset]}
        for offset in range(6, -1, -1)
    ]
    initialized = [_progress(size) for size in SIZES if _tray_state[size]["opening_balance"] is not None]
    total = sum(row["verified_good_eggs"] for row in initialized)
    return jsonify(
        total_sorted=total,
        trays_completed=sum(row["completed_milestones"] or 0 for row in initialized),
        quality_rate=92.5,
        non_good_count=3,
        quality_counts={"Good": total, "Crack": 2, "Rotten": 1, "NoEgg": 0},
        size_counts={size: _progress(size)["verified_good_eggs"] for size in SIZES},
        latest_record={"weight_grams": 62, "size": "Jumbo"},
        daily_trend=trend,
        selected_start_date=trend[0]["date"],
        selected_end_date=today.isoformat(),
        audit_logs=deepcopy(_audit[-12:]),
        tray_progress=[_progress(size) for size in SIZES],
        unread_alerts=_unread_count(),
    )


@app.get("/api/dashboard/audit-logs")
def dashboard_audit_logs():
    return jsonify(logs=deepcopy(_audit[-100:]))


@app.post("/api/dashboard/audit-logs/clear")
def clear_demo_audit():
    _audit.clear()
    return jsonify(ok=True)


@app.get("/api/tray-progress")
def tray_progress():
    rows = [_progress(size) for size in SIZES]
    return jsonify(sizes=rows, ready=all(row["opening_balance_configured"] for row in rows))


@app.post("/api/admin/tray-progress/<path:egg_size>/opening-balance")
def set_opening_balance(egg_size: str):
    if egg_size not in SIZES:
        return jsonify(error="Unknown egg size."), 404
    payload = request.get_json(silent=True) or {}
    value = payload.get("opening_balance")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 29:
        return jsonify(error="Opening balance must be a whole number from 0 to 29."), 400
    with _lock:
        state = _tray_state[egg_size]
        if state["opening_balance"] is not None:
            return jsonify(error="Opening balance is already initialized in this demo."), 409
        state["opening_balance"] = value
        _audit.append({
            "description": f"Demo Admin set {egg_size} opening balance to {value} eggs.",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": "Demo Admin",
            "event_type": "TRAY_BALANCE_SET",
        })
    return jsonify(ok=True, size=_progress(egg_size))


@app.get("/api/alerts")
def alerts_data():
    selected = request.args.get("filter", "unread")
    if selected not in {"read", "unread"}:
        return jsonify(error="Use the read or unread filter."), 400
    rows = [row for row in _alerts if row["is_read"] == (selected == "read")]
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return jsonify(alerts=deepcopy(rows), unread_count=_unread_count())


@app.post("/api/alerts/<path:alert_id>/read")
def mark_alert_read(alert_id: str):
    with _lock:
        row = next((item for item in _alerts if item["id"] == alert_id), None)
        if row is None:
            return jsonify(error="Demo alert not found."), 404
        row["is_read"] = True
    return jsonify(ok=True, unread_count=_unread_count())


@app.post("/api/alerts/read-all")
def mark_all_alerts_read():
    with _lock:
        for row in _alerts:
            row["is_read"] = True
    return jsonify(ok=True, unread_count=_unread_count())


@app.get("/api/users")
def demo_users():
    return jsonify(users=[])


@app.post("/api/demo/add-egg")
def add_demo_egg():
    payload = request.get_json(silent=True) or {}
    size, quality = payload.get("egg_size"), payload.get("quality")
    verified = payload.get("verified") is True
    if size not in SIZES or quality not in {"Good", "Crack", "Rotten"}:
        return jsonify(error="Select a supported size and quality."), 400
    if quality != "Good":
        return jsonify(ok=True, counted=False, message=f"{quality} eggs are not counted.", tray_progress=_progress(size))
    if not verified:
        return jsonify(ok=True, counted=False, message="Unverified Good eggs are not counted.", tray_progress=_progress(size))

    with _lock:
        state = _tray_state[size]
        if state["opening_balance"] is None:
            return jsonify(error=f"Initialize the {size} opening balance in Admin first."), 409
        before = _progress(size)["completed_milestones"]
        state["sorted_good"] += 1
        after = _progress(size)["completed_milestones"]
        if after > before:
            for milestone in range(before + 1, after + 1):
                _alerts.append({
                    "id": f"demo-{size.lower().replace(' ', '-')}-{milestone}-{state['sorted_good']}",
                    "egg_size": size,
                    "milestone_number": milestone,
                    "title": f"{size} tray completed",
                    "message": f"{size} reached {milestone * TARGET} verified Good eggs.",
                    "session_ref": "DEMO-SESSION",
                    "is_read": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
    return jsonify(
        ok=True, counted=True,
        message=f"Verified Good {size} egg added to temporary demo counts.",
        tray_progress=_progress(size), unread_count=_unread_count(),
        milestone_created=after > before,
    )


@app.after_request
def add_demo_ribbon(response):
    if response.mimetype != "text/html":
        return response
    body = response.get_data(as_text=True)
    banner = (
        '<div style="position:fixed;z-index:1000;top:0;left:0;right:0;'
        'padding:7px 12px;text-align:center;background:#9b2c2c;color:white;'
        'font:700 13px system-ui;letter-spacing:.06em">DEMO / DUMMY DATA — '
        'temporary in-memory preview only</div>'
        '<div style="height:34px"></div>'
    )
    body = body.replace("<body", "<body style=\"padding-top:34px\"", 1)
    if request.path == "/dashboard":
        panel = r'''<section id="demo-controls" style="position:fixed;z-index:900;right:14px;bottom:14px;max-width:360px;background:#fff;border:2px solid #9b2c2c;border-radius:14px;padding:12px;box-shadow:0 4px 20px #0003;font:14px system-ui">
<strong>Demo egg simulator</strong><div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
<select id="demo-size" aria-label="Egg size"><option>Small</option><option>Medium</option><option>Large</option><option>Extra Large</option><option>Jumbo</option></select>
<select id="demo-quality" aria-label="Egg quality"><option>Good</option><option>Crack</option><option>Rotten</option></select>
<label><input id="demo-verified" type="checkbox" checked> Camera verified</label>
<button id="demo-add" type="button">Add simulated egg</button></div><p id="demo-result" role="status" style="margin:7px 0 0"></p>
<script>
document.getElementById('demo-add').addEventListener('click', async () => {
 const result = document.getElementById('demo-result');
 try { const response = await fetch('/api/demo/add-egg', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({egg_size:document.getElementById('demo-size').value,quality:document.getElementById('demo-quality').value,verified:document.getElementById('demo-verified').checked})});
 const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Demo request failed');
 result.textContent = data.message; if (data.milestone_created) result.textContent += ' Size-specific unread alert created.';
 if (data.counted) { document.getElementById('refresh-dashboard').click(); }
 } catch (error) { result.textContent = error.message; }
});
</script></section>'''
        body = body.replace("</body>", panel + "</body>", 1)
    body = body.replace("<body", "<body", 1)
    body = body.replace("</body>", banner + "</body>", 1)
    response.set_data(body)
    return response


if __name__ == "__main__":
    print("EGGSor+ dummy preview only. No production app or database is imported.")
    print("Open http://127.0.0.1:5001/dashboard")
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
