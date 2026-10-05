import hashlib
import secrets
import smtplib
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from threading import RLock, Thread
from time import sleep
from typing import Callable, Any
from authlib.integrations.base_client.errors import OAuthError
from joserfc.errors import JoseError

from dotenv import load_dotenv
from flask import (
    abort,
    Flask,
    Response,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from sqlalchemy import String, cast, func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

from camera_session import CAMERA_SESSION, CameraSessionError
from config import build_app_config, env_bool, env_int
from database import initialize_database
from egg_standards import SIZE_ORDER, classify_egg_size
from esp32_bridge import ESP32_BRIDGE
from detection_service import (
    DetectorUnavailableError,
    InvalidFrameError,
    detect_frame,
)
from extensions import db, oauth
from models import AuditLog, EggRecord, TrayAlert, TRAY_CAPACITY, User
from security import AttemptLimiter, init_web_security
from validation import (
    ValidationError,
    is_valid_email,
    normalize_avatar_url,
    parse_staff_profile,
)

app = Flask(__name__)
app.config.from_mapping(build_app_config())
init_web_security(app)
SORTING_RUNTIME_INSTANCE = secrets.token_hex(8)
LOGIN_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=300)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


db.init_app(app)
oauth.init_app(app)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    client_kwargs={"scope": "openid email profile"},
)


@app.get("/health")
def health_check() -> Any:
    """Check the web process and its configured database without hardware."""
    db.session.execute(text("SELECT 1"))
    return jsonify(status="ok", database="connected")


@app.errorhandler(SQLAlchemyError)
def handle_database_error(error: SQLAlchemyError) -> Any:
    db.session.rollback()
    app.logger.exception("Database operation failed", exc_info=error)
    message = "The database is temporarily unavailable. Please try again."
    if request.path.startswith("/api/"):
        return jsonify(error=message), 503
    return message, 503


INITIAL_ADMIN_EMAIL = app.config["ADMIN_EMAIL"]
INITIAL_ADMIN_GOOGLE_SUB = app.config["ADMIN_GOOGLE_SUB"]
INVITE_LIFETIME_HOURS = 24
INVITE_LIFETIME = timedelta(hours=INVITE_LIFETIME_HOURS)
PASSWORD_RESET_LIFETIME = timedelta(minutes=30)
MAX_RECORD_RESULTS = 500
MAX_ALERT_RESULTS = 200


def user_display_label(user: "User") -> str:
    if (
        user.display_name
        and user.display_name.casefold() != (user.email or "").casefold()
    ):
        return user.display_name
    if user.email:
        return user.email.split("@", 1)[0]
    return user.username


def invite_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitation_is_valid(user: "User") -> bool:
    expires_at = user.invite_expires_at
    if not user.invite_token_hash or expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def create_staff_invitation(user: "User") -> str:
    token = secrets.token_urlsafe(32)
    user.invite_token_hash = invite_token_hash(token)
    user.invite_expires_at = datetime.now(timezone.utc) + INVITE_LIFETIME
    return token


class InvitationDeliveryError(RuntimeError):
    pass


def external_url(endpoint: str, **values: Any) -> str:
    path = url_for(endpoint, **values)
    if app.config["PUBLIC_BASE_URL"]:
        return f"{app.config['PUBLIC_BASE_URL']}{path}"
    return url_for(endpoint, _external=True, **values)


def google_is_configured() -> bool:
    return bool(
        app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]
    )


def send_staff_invitation(user: "User", invite_url: str) -> None:
    required_settings = {
        "MAIL_SERVER": app.config["MAIL_SERVER"],
        "MAIL_FROM": app.config["MAIL_FROM"],
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise InvitationDeliveryError(
            f"Email is not configured ({', '.join(missing)} is missing). "
            "Set it in .env, then verify with: flask check-mail"
        )

    message = EmailMessage()
    message["Subject"] = "You are invited to EggSort+"
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = user.email
    message.set_content(
        f"""Hello {user_display_label(user)},

An administrator invited you to EggSort+.

Create your password using this single-use link:
{invite_url}

The link expires in {INVITE_LIFETIME_HOURS} hours. If you were not expecting
this invitation, you
can ignore this email.
"""
    )

    try:
        if app.config["MAIL_USE_SSL"]:
            smtp = smtplib.SMTP_SSL(
                app.config["MAIL_SERVER"],
                app.config["MAIL_PORT"],
                timeout=15,
            )
        else:
            smtp = smtplib.SMTP(
                app.config["MAIL_SERVER"],
                app.config["MAIL_PORT"],
                timeout=15,
            )
        with smtp:
            if app.config["MAIL_USE_TLS"] and not app.config["MAIL_USE_SSL"]:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(
                    app.config["MAIL_USERNAME"],
                    app.config["MAIL_PASSWORD"],
                )
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise InvitationDeliveryError(
            "The invitation was created, but the email could not be sent."
        ) from exc


def send_password_reset(user: "User", reset_url: str) -> None:
    if not app.config["MAIL_SERVER"] or not app.config["MAIL_FROM"]:
        raise InvitationDeliveryError("Password reset email is not configured.")
    message = EmailMessage()
    message["Subject"] = "Reset your EggSort+ password"
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = user.email
    message.set_content(
        f"""Hello {user_display_label(user)},

Use this single-use link to reset your EggSort+ password:
{reset_url}

The link expires in 30 minutes. If you did not request this, ignore this email.
"""
    )
    try:
        smtp_class = smtplib.SMTP_SSL if app.config["MAIL_USE_SSL"] else smtplib.SMTP
        with smtp_class(app.config["MAIL_SERVER"], app.config["MAIL_PORT"], timeout=15) as smtp:
            if app.config["MAIL_USE_TLS"] and not app.config["MAIL_USE_SSL"]:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise InvitationDeliveryError("Password reset email could not be sent.") from exc


def start_sorting_runtime() -> tuple[dict[str, Any], dict[str, Any]]:
    """Start camera inference and the ESP32 link as one station runtime."""
    hardware_state = ESP32_BRIDGE.start()
    try:
        camera_state = CAMERA_SESSION.start()
    except Exception:
        ESP32_BRIDGE.stop()
        raise
    return camera_state, hardware_state


def stop_sorting_runtime() -> tuple[dict[str, Any], dict[str, Any]]:
    """Stop the ESP32 link before releasing the camera and detector."""
    reset_egg_flow()
    hardware_state = ESP32_BRIDGE.stop()
    camera_state = CAMERA_SESSION.stop()
    return camera_state, hardware_state


def sign_in_user(user: "User", remember: bool = False) -> None:
    session.clear()
    session["user_id"] = user.id
    session["username"] = user_display_label(user)
    session["email"] = user.email or ""
    session["role"] = user.role
    session["avatar_url"] = user.avatar_url or ""
    session.permanent = remember
    if app.config["AUTO_START_SORTING_ON_LOGIN"]:
        try:
            start_sorting_runtime()
            session["sorting_runtime_started"] = True
            session.pop("sorting_runtime_error", None)
        except Exception as exc:
            app.logger.exception("Unable to auto-start the sorting runtime")
            session["sorting_runtime_started"] = False
            session["sorting_runtime_error"] = str(exc)
        finally:
            session["sorting_runtime_instance"] = SORTING_RUNTIME_INSTANCE



initialize_database(
    app,
    admin_email=INITIAL_ADMIN_EMAIL,
    admin_google_sub=INITIAL_ADMIN_GOOGLE_SUB,
)


QUALITY_NAMES = {
    "crack": "Crack",
    "good": "Good",
    "rotten": "Rotten",
}
CAMERA_QUALITY_SAMPLES = env_int(
    "CAMERA_QUALITY_SAMPLES", 1, minimum=1, maximum=120
)

def parse_date_boundary(value: str | None, end: bool = False) -> datetime | None:
    if not value:
        return None
    parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    boundary = time.max if end else time.min
    return datetime.combine(parsed_date, boundary, tzinfo=timezone.utc)


def create_tray_alert_if_needed(
    total_sorted: int,
    session_ref: str,
) -> TrayAlert | None:
    if total_sorted <= 0 or total_sorted % TRAY_CAPACITY != 0:
        return None
    tray_number = total_sorted // TRAY_CAPACITY
    existing = TrayAlert.query.filter_by(tray_number=tray_number).first()
    if existing is not None:
        return None
    alert = TrayAlert(
        tray_number=tray_number,
        egg_count=total_sorted,
        session_ref=session_ref,
    )
    db.session.add(alert)
    return alert


def write_audit_log(
    event_type: str,
    description: str,
    *,
    actor: str | None = None,
    event_key: str | None = None,
    created_at: datetime | None = None,
    commit: bool = True,
) -> AuditLog | None:
    if event_key and AuditLog.query.filter_by(event_key=event_key).first():
        return None
    resolved_actor = actor
    if resolved_actor is None and has_request_context():
        resolved_actor = session.get("username")
    log = AuditLog(
        event_type=event_type,
        actor=resolved_actor or "System",
        description=description,
        event_key=event_key,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


def backfill_completed_tray_alerts() -> None:
    total_sorted = EggRecord.query.count()
    created = False
    for tray_number in range(1, (total_sorted // TRAY_CAPACITY) + 1):
        boundary_record = (
            EggRecord.query
            .order_by(EggRecord.id.asc())
            .offset((tray_number * TRAY_CAPACITY) - 1)
            .first()
        )
        session_ref = (
            boundary_record.session_ref
            if boundary_record is not None
            else "HISTORICAL"
        )
        alert = create_tray_alert_if_needed(
            tray_number * TRAY_CAPACITY, session_ref
        )
        created = created or alert is not None
    if created:
        db.session.commit()


def backfill_sorting_audit_logs() -> None:
    created = False
    for record in EggRecord.query.order_by(EggRecord.id.asc()).all():
        log = write_audit_log(
            "egg_sorted",
            (
                f"{record.to_dict()['egg_id']} sorted at {record.weight_grams} g "
                f"as {record.size}, quality {record.quality}."
            ),
            event_key=f"egg-sorted:{record.id}",
            created_at=record.sorted_at,
            commit=False,
        )
        created = created or log is not None
    if created:
        db.session.commit()


with app.app_context():
    backfill_completed_tray_alerts()
    backfill_sorting_audit_logs()


EGG_FLOW_LOCK = RLock()
EGG_FLOW_SEQUENCE = 0
EGG_FLOW_STAGE = "idle"
EGG_FLOW_QUALITY: dict[str, Any] | None = None
EGG_FLOW_PENDING: dict[str, Any] | None = None


def reset_egg_flow() -> None:
    global EGG_FLOW_SEQUENCE, EGG_FLOW_STAGE
    global EGG_FLOW_QUALITY, EGG_FLOW_PENDING
    with EGG_FLOW_LOCK:
        EGG_FLOW_SEQUENCE += 1
        EGG_FLOW_STAGE = "idle"
        EGG_FLOW_QUALITY = None
        EGG_FLOW_PENDING = None


def _flow_is_current(
    sequence: int,
    allowed_stages: set[str] | None = None,
) -> bool:
    with EGG_FLOW_LOCK:
        return (
            sequence == EGG_FLOW_SEQUENCE
            and (
                allowed_stages is None
                or EGG_FLOW_STAGE in allowed_stages
            )
        )


def _inspect_then_measure(sequence: int) -> None:
    """Match the scale egg to the oldest one-shot zone exit capture."""
    global EGG_FLOW_STAGE, EGG_FLOW_QUALITY
    while _flow_is_current(sequence, {"inspecting"}):
        quality_result = CAMERA_SESSION.wait_for_captured_quality(timeout=1.0)
        if quality_result is None:
            camera_state = CAMERA_SESSION.status()
            if not camera_state["running"] or camera_state.get("error"):
                ESP32_BRIDGE.publish_status(
                    "Egg held: camera/YOLO is not available for quality inspection.",
                    "flow_error",
                )
                return
            continue

        raw_quality = str(quality_result["label"]).lower()
        quality = QUALITY_NAMES.get(raw_quality)
        if quality is None:
            continue
        captured = {
            "label": quality,
            "confidence": float(quality_result["confidence"]),
            "capture_id": quality_result["capture_id"],
        }
        with EGG_FLOW_LOCK:
            if (
                sequence != EGG_FLOW_SEQUENCE
                or EGG_FLOW_STAGE != "inspecting"
            ):
                return
            EGG_FLOW_QUALITY = captured
        ESP32_BRIDGE.publish_status(
            f"Egg #{captured['capture_id']} matched: {quality}. "
            "Starting weight measurement."
        )

        reported_wait = False
        while _flow_is_current(sequence, {"inspecting"}):
            try:
                ESP32_BRIDGE.measure_egg(
                    quality, capture_id=captured["capture_id"]
                )
                with EGG_FLOW_LOCK:
                    if (
                        sequence == EGG_FLOW_SEQUENCE
                        and EGG_FLOW_STAGE == "inspecting"
                    ):
                        EGG_FLOW_STAGE = "measuring"
                return
            except RuntimeError:
                if not reported_wait:
                    ESP32_BRIDGE.publish_status(
                        "Egg held: waiting for the ESP32 connection before weighing.",
                        "flow_error",
                    )
                    reported_wait = True
                sleep(1)


def begin_egg_flow() -> None:
    """Match a scale arrival to its already captured rolling inspection."""
    global EGG_FLOW_SEQUENCE, EGG_FLOW_STAGE
    global EGG_FLOW_QUALITY, EGG_FLOW_PENDING
    with EGG_FLOW_LOCK:
        if EGG_FLOW_STAGE != "idle":
            return
        EGG_FLOW_SEQUENCE += 1
        sequence = EGG_FLOW_SEQUENCE
        EGG_FLOW_STAGE = "inspecting"
        EGG_FLOW_QUALITY = None
        EGG_FLOW_PENDING = None
    ESP32_BRIDGE.publish_status(
        "Egg on load cell. Waiting for its one-shot zone exit capture."
    )
    Thread(
        target=_inspect_then_measure,
        args=(sequence,),
        name=f"eggsort-inspection-{sequence}",
        daemon=True,
    ).start()


def queue_sort_after_measurement(event: dict[str, Any]) -> None:
    """Remember the result before the controller confirms its automatic route."""
    global EGG_FLOW_STAGE, EGG_FLOW_PENDING
    weight = event.get("weight_grams")
    if weight is None:
        ESP32_BRIDGE.publish_status(
            "Egg held: ESP32 did not provide a final weight.",
            "flow_error",
        )
        return

    with EGG_FLOW_LOCK:
        if EGG_FLOW_STAGE != "measuring":
            return
        quality_result = dict(EGG_FLOW_QUALITY) if EGG_FLOW_QUALITY else None
        sequence = EGG_FLOW_SEQUENCE
    if quality_result is None:
        ESP32_BRIDGE.publish_status(
            "Egg held: no camera quality is locked for this measurement.",
            "flow_error",
        )
        return

    size = classify_egg_size(int(weight))
    pending = {
        "_capture_id": quality_result["capture_id"],
        "weight_grams": int(weight),
        "size": size,
        "quality": quality_result["label"],
        "confidence": quality_result["confidence"],
        "session_ref": (
            CAMERA_SESSION.status().get("session_ref")
            or "NO-ACTIVE-SESSION"
        ),
    }
    with EGG_FLOW_LOCK:
        if (
            sequence != EGG_FLOW_SEQUENCE
            or EGG_FLOW_STAGE != "measuring"
        ):
            return
        EGG_FLOW_PENDING = pending
        EGG_FLOW_STAGE = "sorting"

    ESP32_BRIDGE.publish_status(
        f"Egg #{quality_result['capture_id']}: {weight} g ({size}). "
        "ESP32 is routing automatically."
    )


def save_sorted_egg(event: dict[str, Any]) -> None:
    """Persist only after the ESP32 confirms that its servo route completed."""
    global EGG_FLOW_STAGE, EGG_FLOW_PENDING
    with EGG_FLOW_LOCK:
        # Moving to "saving" before touching the database makes repeated
        # SERVO SORTED lines idempotent: one physical egg can create one row.
        if EGG_FLOW_STAGE != "sorting":
            return
        pending = dict(EGG_FLOW_PENDING) if EGG_FLOW_PENDING else None
        sequence = EGG_FLOW_SEQUENCE
        if pending is not None:
            EGG_FLOW_STAGE = "saving"
    if pending is None:
        ESP32_BRIDGE.publish_status(
            "Sort confirmation received without a pending egg record.",
            "flow_error",
        )
        return

    confirmed_size = event.get("size")
    if confirmed_size and confirmed_size != pending["size"]:
        ESP32_BRIDGE.publish_status(
            "ESP32 route confirmation did not match the measured size; "
            "saving the load-cell classification.",
            "flow_error",
        )

    capture_id = pending.pop("_capture_id")
    try:
        with app.app_context():
            record = EggRecord(**pending)
            db.session.add(record)
            db.session.flush()
            total_sorted = EggRecord.query.count()
            create_tray_alert_if_needed(total_sorted, pending["session_ref"])
            write_audit_log(
                "egg_sorted",
                (
                    f"EGG-{record.id:06d} sorted at {record.weight_grams} g "
                    f"as {record.size}, quality {record.quality}."
                ),
                event_key=f"egg-sorted:{record.id}",
                commit=False,
            )
            db.session.commit()
            egg_id = record.id
    except Exception:
        with app.app_context():
            db.session.rollback()
        with EGG_FLOW_LOCK:
            if sequence == EGG_FLOW_SEQUENCE and EGG_FLOW_STAGE == "saving":
                EGG_FLOW_STAGE = "sorting"
        app.logger.exception("Unable to persist the completed egg sort")
        ESP32_BRIDGE.publish_status(
            "Sort completed, but the database save failed. The egg remains "
            "pending for recovery.",
            "flow_error",
        )
        return
    with EGG_FLOW_LOCK:
        EGG_FLOW_PENDING = None
        EGG_FLOW_STAGE = "waiting_removal"
    ESP32_BRIDGE.publish_status(
        f"Egg #{capture_id} saved as EGG-{egg_id:06d}."
    )


def handle_esp32_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type == "egg_detected":
        begin_egg_flow()
    elif event_type == "egg_complete":
        queue_sort_after_measurement(event)
    elif event_type == "sort_complete":
        save_sorted_egg(event)
    elif event_type == "egg_left":
        reset_egg_flow()


ESP32_BRIDGE.set_event_handler(handle_esp32_event)
CAMERA_SESSION.set_reject_handler(ESP32_BRIDGE.reject_egg)
CAMERA_SESSION.set_capture_handler(ESP32_BRIDGE.publish_camera_capture)


@app.before_request
def ensure_authenticated_sorting_runtime() -> None:
    """Restore the station runtime for authenticated sessions after restart."""
    if not app.config["AUTO_START_SORTING_ON_LOGIN"]:
        return
    if "user_id" not in session:
        return
    if request.endpoint in {"logout", "static"}:
        return
    if session.get("sorting_runtime_auto_start_suppressed"):
        return
    same_instance = (
        session.get("sorting_runtime_instance") == SORTING_RUNTIME_INSTANCE
    )
    if same_instance:
        camera_running = CAMERA_SESSION.status().get("running", False)
        hardware_running = ESP32_BRIDGE.status().get("running", False)
        if camera_running and hardware_running:
            return
        retry_after = float(session.get("sorting_runtime_retry_after", 0))
        if datetime.now(timezone.utc).timestamp() < retry_after:
            return

    try:
        start_sorting_runtime()
        session["sorting_runtime_started"] = True
        session.pop("sorting_runtime_error", None)
        session.pop("sorting_runtime_retry_after", None)
    except Exception as exc:
        app.logger.exception("Unable to restore the sorting runtime")
        session["sorting_runtime_started"] = False
        session["sorting_runtime_error"] = str(exc)
        session["sorting_runtime_retry_after"] = (
            datetime.now(timezone.utc).timestamp() + 5
        )
    finally:
        session["sorting_runtime_instance"] = SORTING_RUNTIME_INSTANCE



# Home
@app.route("/")
def home() -> Any:
    return redirect(url_for("login"))



# Public self-registration is intentionally disabled. Admins allowlist staff
# Google accounts from User Management.
@app.route("/register", methods=["GET", "POST"])
def register() -> Any:
    session["login_error"] = (
        "Public registration is disabled. Staff accounts require an "
        "invitation from the administrator."
    )
    return redirect(url_for("login"))


# Invited staff can use a password or bind their verified Google account.
@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    error = session.pop("login_error", None)
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter(
            or_(
                func.lower(User.username) == identifier,
                func.lower(User.email) == identifier,
            )
        ).first()
        limiter_key = f"user:{user.id}" if user is not None else identifier
        if LOGIN_LIMITER.is_limited(limiter_key):
            error = "Too many failed sign-in attempts. Try again in a few minutes."
            return render_template(
                "login.html",
                error=error,
                notice=None,
                google_ready=bool(
                    app.config["GOOGLE_CLIENT_ID"]
                    and app.config["GOOGLE_CLIENT_SECRET"]
                ),
            ), 429
        password_matches = check_password_hash(
            user.password if user is not None else DUMMY_PASSWORD_HASH,
            password,
        )
        if (
            user is None
            or not user.is_active
            or not user.password_set
            or not password_matches
        ):
            LOGIN_LIMITER.record_failure(limiter_key)
            error = "Invalid username/email or password."
            write_audit_log(
                "login_failed",
                "Failed staff sign-in attempt.",
                actor=identifier or "Unknown",
            )
        else:
            LOGIN_LIMITER.reset(limiter_key)
            sign_in_user(
                user,
                remember=request.form.get("remember-me") == "on",
            )
            write_audit_log(
                "login",
                f"{user.role.title()} signed in successfully with a password.",
                actor=user.email or user.username,
            )
            return redirect(url_for("dashboard"))

    return render_template(
        "login.html",
        error=error,
        notice=session.pop("login_notice", None),
        google_ready=bool(
            app.config["GOOGLE_CLIENT_ID"]
            and app.config["GOOGLE_CLIENT_SECRET"]
        ),
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password() -> Any:
    notice = session.pop("password_reset_notice", None)
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter(func.lower(User.email) == email).first() if email else None
        if user is not None and user.is_active and user.password_set:
            token = secrets.token_urlsafe(32)
            user.password_reset_token_hash = invite_token_hash(token)
            user.password_reset_expires_at = datetime.now(timezone.utc) + PASSWORD_RESET_LIFETIME
            db.session.commit()
            try:
                send_password_reset(
                    user,
                    external_url("reset_password", token=token),
                )
            except InvitationDeliveryError:
                app.logger.exception("Password reset email delivery failed")
        session["password_reset_notice"] = (
            "If an active account uses that email, a password reset link has been sent."
        )
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html", notice=notice, error=error)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str) -> Any:
    user = User.query.filter_by(
        password_reset_token_hash=invite_token_hash(token),
        is_active=True,
        password_set=True,
    ).first()
    expires_at = user.password_reset_expires_at if user else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if user is None or expires_at is None or expires_at <= datetime.now(timezone.utc):
        return render_template(
            "reset_password.html", error="This reset link is invalid or has expired.",
            valid_token=False, notice=None,
        ), 400

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif len(password) > 128:
            error = "Password must be 128 characters or fewer."
        elif password != confirmation:
            error = "Passwords do not match."
        else:
            user.password = generate_password_hash(password)
            user.password_set = True
            user.password_reset_token_hash = None
            user.password_reset_expires_at = None
            user.invite_token_hash = None
            user.invite_expires_at = None
            db.session.commit()
            write_audit_log("password_reset", "User reset their password.", actor=user.email or user.username)
            session["login_notice"] = "Password updated. Sign in with your new password."
            return redirect(url_for("login"))
    return render_template("reset_password.html", error=error, valid_token=True, notice=None)


@app.route("/accept-invite/<token>", methods=["GET", "POST"])
def accept_invite(token: str) -> Any:
    user = User.query.filter_by(
        invite_token_hash=invite_token_hash(token),
        role="staff",
        is_active=True,
    ).first()
    valid_invite = user is not None and invitation_is_valid(user)
    error = session.pop("invite_error", None)

    # Keep the raw token only in this browser's signed session while it moves
    # through Google OAuth. The database continues to hold only its hash.
    if valid_invite:
        session["pending_invite_token"] = token
    elif session.get("pending_invite_token") == token:
        session.pop("pending_invite_token", None)
        session.pop("invite_google_verified_user_id", None)

    if request.method == "POST":
        if not valid_invite or user is None:
            error = "This invitation is invalid, expired, or already used."
        elif (
            google_is_configured()
            and session.get("invite_google_verified_user_id") != user.id
        ):
            error = (
                "Continue with the invited Google account first so we can verify "
                "the invitation and use its profile photo."
            )
        else:
            password = request.form.get("password", "")
            password_confirmation = request.form.get(
                "password_confirmation",
                "",
            )
            if len(password) < 8:
                error = "Password must contain at least 8 characters."
            elif len(password) > 128:
                error = "Password must not exceed 128 characters."
            elif password != password_confirmation:
                error = "Passwords do not match."
            else:
                user.password = generate_password_hash(password)
                user.password_set = True
                user.invite_token_hash = None
                user.invite_expires_at = None
                write_audit_log(
                    "staff_activated",
                    f"Staff account '{user.username}' accepted its invitation.",
                    actor=user.email or user.username,
                    commit=False,
                )
                db.session.commit()
                session.pop("pending_invite_token", None)
                session.pop("invite_google_verified_user_id", None)
                session["login_notice"] = (
                    "Account activated. You can now sign in with your staff password "
                    "or the verified Google account."
                )
                return redirect(url_for("login"))

    return render_template(
        "accept_invite.html",
        error=error,
        valid_invite=valid_invite,
        invited_user=user,
        google_ready=google_is_configured(),
        google_verified=(
            valid_invite
            and user is not None
            and session.get("invite_google_verified_user_id") == user.id
        ),
    )


@app.get("/auth/google")
def google_login() -> Any:
    if not google_is_configured():
        session["login_error"] = (
            "Google sign-in is not configured yet. Add the Google OAuth "
            "client ID and secret, then restart EggSort+."
        )
        return redirect(url_for("login"))
    redirect_uri = external_url("google_callback")
    return oauth.google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback() -> Any:
    try:
        token = oauth.google.authorize_access_token(
            leeway=app.config["GOOGLE_AUTH_CLOCK_SKEW_SECONDS"]
        )
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.google.userinfo(token=token).json()
    except OAuthError:
        session["login_error"] = (
            "Google sign-in was cancelled or could not be verified."
        )
        return redirect(url_for("login"))
    except JoseError as exc:
        app.logger.warning("Google ID token validation failed: %s", exc)
        session["login_error"] = (
            "Google sign-in could not be verified. This computer's date and "
            "time may be incorrect. Synchronize Windows Time and try again."
        )
        return redirect(url_for("login"))
    except Exception:
        app.logger.exception("Google sign-in failed unexpectedly")
        session["login_error"] = (
            "Google sign-in is temporarily unavailable. Please try again."
        )
        return redirect(url_for("login"))

    email = str(userinfo.get("email", "")).strip().lower()
    google_sub = str(userinfo.get("sub", "")).strip()
    if not email or not google_sub or userinfo.get("email_verified") is not True:
        session["login_error"] = (
            "Google did not provide a verified email address."
        )
        return redirect(url_for("login"))

    # An invitation is tied to its recipient's verified Google email. This is
    # the only safe way to collect a Google profile photo: Google will not
    # disclose a person's profile from an email address supplied by an admin.
    pending_token = session.get("pending_invite_token")
    if pending_token:
        invited_user = User.query.filter_by(
            invite_token_hash=invite_token_hash(pending_token),
            role="staff",
            is_active=True,
        ).first()
        if invited_user is None or not invitation_is_valid(invited_user):
            session.pop("pending_invite_token", None)
            session.pop("invite_google_verified_user_id", None)
            session["login_error"] = "This staff invitation is no longer valid."
            return redirect(url_for("login"))
        if (
            not invited_user.email
            or invited_user.email.casefold() != email.casefold()
        ):
            session["invite_error"] = (
                "Use the Google account for the invited email address "
                f"({invited_user.email})."
            )
            return redirect(url_for("accept_invite", token=pending_token))
        bound_user = User.query.filter_by(google_sub=google_sub).first()
        if bound_user is not None and bound_user.id != invited_user.id:
            session["invite_error"] = (
                "That Google account is already connected to another EggSort+ account."
            )
            return redirect(url_for("accept_invite", token=pending_token))

        invited_user.google_sub = google_sub
        invited_user.display_name = (
            str(userinfo.get("name", "")).strip()[:120]
            or invited_user.display_name
            or email.split("@", 1)[0]
        )
        avatar_url = normalize_avatar_url(userinfo.get("picture"))
        if avatar_url:
            invited_user.avatar_url = avatar_url
        db.session.commit()
        session["invite_google_verified_user_id"] = invited_user.id
        return redirect(url_for("accept_invite", token=pending_token))

    user = User.query.filter_by(google_sub=google_sub).first()
    if user is None:
        user = User.query.filter(func.lower(User.email) == email).first()

    if user is None:
        write_audit_log(
            "login_denied",
            f"Google account '{email}' is not authorized.",
            actor=email,
        )
        session["login_error"] = (
            "This Google account is not authorized. Ask the administrator "
            "to add your email in User Management."
        )
        return redirect(url_for("login"))

    if not user.is_active:
        write_audit_log(
            "login_denied",
            f"Disabled Google account '{email}' attempted to sign in.",
            actor=email,
        )
        session["login_error"] = "This account has been disabled."
        return redirect(url_for("login"))

    if user.google_sub and user.google_sub != google_sub:
        write_audit_log(
            "login_denied",
            f"Google account '{email}' does not match the bound account ID.",
            actor=email,
        )
        session["login_error"] = (
            "A different Google account is already connected to this user."
        )
        return redirect(url_for("login"))

    if (
        user.role == "admin"
        and INITIAL_ADMIN_GOOGLE_SUB
        and google_sub != INITIAL_ADMIN_GOOGLE_SUB
    ):
        write_audit_log(
            "login_denied",
            f"Google account '{email}' has the wrong administrator account ID.",
            actor=email,
        )
        session["login_error"] = "This Google account is not authorized."
        return redirect(url_for("login"))

    if user.role == "staff" and not user.password_set:
        session["login_error"] = (
            "Accept your staff invitation and create a password before "
            "using Google sign-in."
        )
        return redirect(url_for("login"))

    user.google_sub = google_sub
    user.display_name = (
        str(userinfo.get("name", "")).strip()[:120]
        or email.split("@", 1)[0]
    )
    avatar_url = str(userinfo.get("picture", "")).strip()[:1024]
    user.avatar_url = avatar_url if avatar_url.startswith("https://") else None
    db.session.commit()

    sign_in_user(user, remember=True)
    write_audit_log(
        "login",
        f"{user.role.title()} signed in successfully with Google.",
        actor=user.email or user.username,
    )
    if user.role == "admin":
        session["google_verified_for_password_setup"] = True
        if not user.password_set:
            return redirect(url_for("setup_admin_password"))
    session.pop("google_verified_for_password_setup", None)
    return redirect(url_for("dashboard"))



def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication is required."), 401
            return redirect(url_for("login"))
        user = db.session.get(User, session["user_id"])
        if user is None or not user.is_active:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication is required."), 401
            return redirect(url_for("login"))
        session["username"] = user_display_label(user)
        session["email"] = user.email or ""
        session["role"] = user.role
        session["avatar_url"] = user.avatar_url or ""
        if (
            user.role == "admin"
            and not user.password_set
            and request.endpoint != "setup_admin_password"
        ):
            if session.get("google_verified_for_password_setup"):
                return redirect(url_for("setup_admin_password"))
            session.clear()
            session["login_error"] = (
                "Continue with Google to finish the one-time admin setup."
            )
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    @login_required
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="Administrator access is required."), 403
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_current_user() -> dict[str, str | None]:
    return {
        "current_user_role": session.get("role"),
        "current_user_email": session.get("email"),
        "current_user_avatar_url": session.get("avatar_url"),
    }


@app.route("/setup-admin-password", methods=["GET", "POST"])
@admin_required
def setup_admin_password() -> Any:
    user = db.session.get(User, session["user_id"])
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    if user.password_set:
        session.pop("google_verified_for_password_setup", None)
        return redirect(url_for("dashboard"))
    if not session.get("google_verified_for_password_setup"):
        session.clear()
        session["login_error"] = (
            "Sign in with Google first to create the administrator password."
        )
        return redirect(url_for("login"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirmation = request.form.get(
            "password_confirmation",
            "",
        )
        if len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif len(password) > 128:
            error = "Password must not exceed 128 characters."
        elif password != password_confirmation:
            error = "Passwords do not match."
        else:
            user.password = generate_password_hash(password)
            user.password_set = True
            session.pop("google_verified_for_password_setup", None)
            write_audit_log(
                "admin_password_created",
                "Administrator created a password after Google verification.",
                actor=user.email or user.username,
                commit=False,
            )
            db.session.commit()
            return redirect(url_for("dashboard"))

    return render_template(
        "setup_admin_password.html",
        error=error,
        email=user.email,
    )


# Dashboard
@app.route("/dashboard")
@login_required
def dashboard() -> Any:
    return render_template(
        "dashboard.html",
        username=session["username"]
    )



# Sorting Sessions
@app.route("/sorting-sessions")
@login_required
def sorting_sessions() -> Any:
    return render_template(
        "sorting_session.html",
        username=session["username"]
    )


@app.post("/api/detect")
@login_required
def detect() -> Any:
    frame = request.files.get("frame")
    if frame is None:
        return jsonify(error="A camera frame is required."), 400

    if frame.mimetype not in {"image/jpeg", "image/png"}:
        return jsonify(error="Only JPEG and PNG camera frames are supported."), 415

    try:
        return jsonify(detect_frame(frame.read()))
    except InvalidFrameError as exc:
        return jsonify(error=str(exc)), 400
    except DetectorUnavailableError as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/camera/start")
@login_required
def start_camera() -> Any:
    try:
        camera_state, hardware_state = start_sorting_runtime()
        session.pop("sorting_runtime_auto_start_suppressed", None)
        session.pop("sorting_runtime_retry_after", None)
        write_audit_log(
            "camera_started",
            f"Sorting camera session {camera_state.get('session_ref')} started.",
        )
        return jsonify(camera=camera_state, hardware=hardware_state)
    except CameraSessionError as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/camera/stop")
@login_required
def stop_camera() -> Any:
    camera_state, hardware_state = stop_sorting_runtime()
    session["sorting_runtime_auto_start_suppressed"] = True
    write_audit_log(
        "camera_stopped",
        "Sorting camera and hardware session stopped manually.",
    )
    return jsonify(camera=camera_state, hardware=hardware_state)


@app.get("/api/camera/status")
@login_required
def camera_status() -> Any:
    response = jsonify(CAMERA_SESSION.status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/camera/feed")
@login_required
def camera_feed() -> Any:
    if not CAMERA_SESSION.status()["running"]:
        return jsonify(error="No camera session is running."), 409

    def generate_frames() -> Any:
        sequence = 0
        while True:
            next_sequence, jpeg, running = CAMERA_SESSION.wait_for_frame(
                sequence
            )
            if jpeg is not None and next_sequence != sequence:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                    + jpeg
                    + b"\r\n"
                )
            sequence = next_sequence
            if not running:
                break

    return Response(
        stream_with_context(generate_frames()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/hardware/status")
@login_required
def hardware_status() -> Any:
    return jsonify(ESP32_BRIDGE.status())


@app.post("/api/hardware/gate/advance")
@login_required
def advance_load_cell_gate() -> Any:
    try:
        ESP32_BRIDGE.advance_gate()
        write_audit_log(
            "gate_advanced",
            "Operator manually advanced the load-cell gate.",
        )
        return jsonify(ok=True, message="Load-cell gate command sent.")
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503


@app.get("/api/egg-records")
@login_required
def egg_records_data() -> Any:
    after_id = request.args.get("after_id", default=0, type=int)
    limit = max(
        1,
        min(
            request.args.get("limit", default=100, type=int),
            MAX_RECORD_RESULTS,
        ),
    )
    query = EggRecord.query.filter(EggRecord.id > after_id)
    search = request.args.get("q", "").strip()
    size = request.args.get("size", "").strip()
    quality = request.args.get("quality", "").strip()
    try:
        start_date = parse_date_boundary(request.args.get("start_date"))
        end_date = parse_date_boundary(request.args.get("end_date"), end=True)
    except ValueError:
        return jsonify(error="Dates must use YYYY-MM-DD format."), 400

    if search:
        numeric = "".join(character for character in search if character.isdigit())
        conditions = [
            EggRecord.session_ref.ilike(f"%{search}%"),
            cast(EggRecord.weight_grams, String).ilike(f"%{search}%"),
        ]
        if numeric:
            conditions.append(EggRecord.id == int(numeric))
        query = query.filter(or_(*conditions))
    if size and size != "All Sizes":
        query = query.filter(EggRecord.size == size)
    if quality and quality != "All Qualities":
        query = query.filter(EggRecord.quality == quality)
    if start_date is not None:
        query = query.filter(EggRecord.sorted_at >= start_date)
    if end_date is not None:
        query = query.filter(EggRecord.sorted_at <= end_date)

    records = query.order_by(EggRecord.id.desc()).limit(limit).all()
    return jsonify(
        records=[record.to_dict() for record in records],
        latest_id=max((record.id for record in records), default=after_id),
    )


@app.get("/api/dashboard/stats")
@login_required
def dashboard_stats() -> Any:
    total_sorted = EggRecord.query.count()
    size_rows = (
        db.session.query(EggRecord.size, func.count(EggRecord.id))
        .group_by(EggRecord.size)
        .all()
    )
    quality_rows = (
        db.session.query(EggRecord.quality, func.count(EggRecord.id))
        .group_by(EggRecord.quality)
        .all()
    )
    size_counts = {size: 0 for size in SIZE_ORDER}
    size_counts.update({size: count for size, count in size_rows})
    quality_counts = {quality: count for quality, count in quality_rows}
    good_count = quality_counts.get("Good", 0)
    latest_record = EggRecord.query.order_by(EggRecord.id.desc()).first()
    today = datetime.now(timezone.utc).date()
    trend_days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    trend_counts = {day.isoformat(): 0 for day in trend_days}
    trend_start = datetime.combine(trend_days[0], time.min, tzinfo=timezone.utc)
    recent_records = EggRecord.query.filter(EggRecord.sorted_at >= trend_start).all()
    for record in recent_records:
        sorted_at = record.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        day_key = sorted_at.date().isoformat()
        if day_key in trend_counts:
            trend_counts[day_key] += 1
    recent_audits = (
        AuditLog.query
        .order_by(AuditLog.id.desc())
        .limit(12)
        .all()
    )

    return jsonify(
        total_sorted=total_sorted,
        trays_completed=total_sorted // TRAY_CAPACITY,
        quality_rate=round(
            (good_count / total_sorted * 100) if total_sorted else 0,
            1,
        ),
        non_good_count=total_sorted - good_count,
        size_counts=size_counts,
        quality_counts=quality_counts,
        latest_record=latest_record.to_dict() if latest_record else None,
        hardware=ESP32_BRIDGE.status(),
        unread_alerts=TrayAlert.query.filter_by(is_read=False).count(),
        daily_trend=[
            {
                "date": day.isoformat(),
                "label": day.strftime("%a"),
                "count": trend_counts[day.isoformat()],
            }
            for day in trend_days
        ],
        audit_logs=[log.to_dict() for log in recent_audits],
    )


@app.get("/api/alerts")
@login_required
def alerts_data() -> Any:
    unread_only = request.args.get("filter") == "unread"
    query = TrayAlert.query
    if unread_only:
        query = query.filter_by(is_read=False)
    alerts_list = (
        query.order_by(TrayAlert.id.desc()).limit(MAX_ALERT_RESULTS).all()
    )
    return jsonify(
        alerts=[alert.to_dict() for alert in alerts_list],
        unread_count=TrayAlert.query.filter_by(is_read=False).count(),
    )


@app.post("/api/alerts/read-all")
@login_required
def mark_all_alerts_read() -> Any:
    TrayAlert.query.filter_by(is_read=False).update(
        {"is_read": True},
        synchronize_session=False,
    )
    db.session.commit()
    return jsonify(ok=True, unread_count=0)


@app.get("/api/reports")
@login_required
def reports_data() -> Any:
    sampling = request.args.get("sampling", "daily").lower()
    if sampling not in {"daily", "weekly", "monthly"}:
        return jsonify(error="Invalid sampling period."), 400
    try:
        start_date = parse_date_boundary(request.args.get("start_date"))
        end_date = parse_date_boundary(request.args.get("end_date"), end=True)
    except ValueError:
        return jsonify(error="Dates must use YYYY-MM-DD format."), 400
    if start_date and end_date and start_date > end_date:
        return jsonify(error="Start date cannot be after end date."), 400

    query = EggRecord.query
    if start_date:
        query = query.filter(EggRecord.sorted_at >= start_date)
    if end_date:
        query = query.filter(EggRecord.sorted_at <= end_date)
    records = query.order_by(EggRecord.sorted_at.asc()).all()

    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        sorted_at = record.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        if sampling == "daily":
            key = sorted_at.strftime("%Y-%m-%d")
        elif sampling == "weekly":
            iso_year, iso_week, _ = sorted_at.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        else:
            key = sorted_at.strftime("%Y-%m")
        row = groups.setdefault(
            key,
            {
                "period": key,
                "total": 0,
                "good": 0,
                "crack": 0,
                "rotten": 0,
                "other": 0,
            },
        )
        row["total"] += 1
        quality_key = record.quality.lower()
        if quality_key in {"good", "crack", "rotten"}:
            row[quality_key] += 1
        else:
            # Retain visibility of records made with an older model without
            # relabeling those historical predictions as a new class.
            row["other"] += 1

    total = len(records)
    good = sum(1 for record in records if record.quality == "Good")
    crack = sum(1 for record in records if record.quality == "Crack")
    rotten = sum(1 for record in records if record.quality == "Rotten")
    other = total - good - crack - rotten
    return jsonify(
        rows=list(groups.values()),
        summary={
            "total": total,
            "good": good,
            "crack": crack,
            "rotten": rotten,
            "other": other,
            "defects": total - good,
            "quality_rate": round((good / total * 100) if total else 0, 1),
        },
    )


def gravatar_url_for(email: str | None) -> str | None:
    """Return a Gravatar URL for a Google-hosted mailbox, or None.

    Google registers Gmail addresses with Gravatar, so this lets an invited
    operator show a real profile photo before they have ever signed in. The
    ``d=404`` parameter makes Gravatar answer with a 404 instead of a generic
    silhouette when the person has no Gravatar image, so the browser can fall
    back to the initial on the image error event.
    """
    address = (email or "").strip().lower()
    if not is_valid_email(address):
        return None
    if not address.endswith(("@gmail.com", "@googlemail.com")):
        return None
    digest = hashlib.md5(address.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"https://www.gravatar.com/avatar/{digest}?s=160&d=404"


@app.get("/api/users")
@admin_required
def users_data() -> Any:
    users_list = User.query.order_by(User.role.asc(), User.email.asc()).all()
    return jsonify(
        users=[
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
                "gravatar_url": gravatar_url_for(user.email),
                "role": user.role,
                "is_active": user.is_active,
                "password_set": user.password_set,
                "invite_pending": invitation_is_valid(user),
                "google_connected": bool(user.google_sub),
                "is_current": user.id == session["user_id"],
            }
            for user in users_list
        ]
    )


@app.post("/api/users")
@admin_required
def create_user() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error="Request body must be a JSON object."), 400
    try:
        profile = parse_staff_profile(payload)
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    existing_user = User.query.filter(
        or_(
            func.lower(User.email) == profile.email,
            func.lower(User.username) == profile.username,
        )
    ).first()
    if existing_user:
        return jsonify(error="That email or username is already registered."), 409
    user = User(
        username=profile.username,
        password=generate_password_hash(secrets.token_urlsafe(32)),
        email=profile.email,
        display_name=profile.display_name,
        avatar_url=profile.avatar_url,
        role="staff",
        is_active=True,
        password_set=False,
    )
    token = create_staff_invitation(user)
    db.session.add(user)
    db.session.flush()
    write_audit_log(
        "staff_invited",
        f"Staff account '{profile.username}' was invited.",
        event_key=f"user-created:{user.id}",
        commit=False,
    )
    db.session.commit()
    invite_url = external_url("accept_invite", token=token)
    email_sent = True
    warning = None
    try:
        send_staff_invitation(user, invite_url)
    except InvitationDeliveryError as exc:
        email_sent = False
        warning = str(exc)
    return jsonify(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        invite_url=invite_url,
        email_sent=email_sent,
        warning=warning,
        expires_in_hours=INVITE_LIFETIME_HOURS,
    ), 201


@app.patch("/api/users/<int:user_id>")
@admin_required
def update_user(user_id: int) -> Any:
    user = db.get_or_404(User, user_id)
    if user.role == "admin":
        return jsonify(
            error="The Google administrator profile is managed by sign-in."
        ), 409
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error="Request body must be a JSON object."), 400
    try:
        profile = parse_staff_profile(payload)
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    duplicate = User.query.filter(
        or_(
            func.lower(User.email) == profile.email,
            func.lower(User.username) == profile.username,
        ),
        User.id != user_id,
    ).first()
    if duplicate:
        return jsonify(error="That email or username is already registered."), 409
    user.email = profile.email
    user.username = profile.username
    user.display_name = profile.display_name
    # A blank field leaves the existing photo alone, so renaming an operator
    # never wipes the picture captured at Google sign-in.
    if profile.avatar_url:
        user.avatar_url = profile.avatar_url
    write_audit_log(
        "user_updated",
        f"Staff account '{profile.username}' was updated.",
        commit=False,
    )
    db.session.commit()
    return jsonify(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        role=user.role,
    )


@app.post("/api/users/<int:user_id>/invite")
@admin_required
def regenerate_user_invite(user_id: int) -> Any:
    user = db.get_or_404(User, user_id)
    if user.role != "staff":
        return jsonify(error="Only staff accounts use invitations."), 409
    token = create_staff_invitation(user)
    write_audit_log(
        "staff_reinvited",
        f"A new invitation was created for staff account '{user.username}'.",
        commit=False,
    )
    db.session.commit()
    invite_url = external_url("accept_invite", token=token)
    email_sent = True
    warning = None
    try:
        send_staff_invitation(user, invite_url)
    except InvitationDeliveryError as exc:
        email_sent = False
        warning = str(exc)
    return jsonify(
        invite_url=invite_url,
        email_sent=email_sent,
        warning=warning,
        expires_in_hours=INVITE_LIFETIME_HOURS,
    )


@app.delete("/api/users/<int:user_id>")
@admin_required
def delete_user(user_id: int) -> Any:
    if user_id == session["user_id"]:
        return jsonify(error="You cannot delete the account currently signed in."), 409
    user = db.get_or_404(User, user_id)
    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        return jsonify(error="At least one administrator must remain."), 409
    deleted_identity = user.email or user.username
    db.session.delete(user)
    write_audit_log(
        "user_deleted",
        f"Account '{deleted_identity}' was removed.",
        commit=False,
    )
    db.session.commit()
    return jsonify(ok=True)



# Egg Records
@app.route("/egg-records")
@login_required
def egg_records() -> Any:
    return render_template(
        "egg_records.html",
        username=session["username"]
    )



# Alerts
@app.route("/alerts")
@login_required
def alerts() -> Any:
    return render_template(
        "alerts.html",
        username=session["username"]
    )



# Reports
@app.route("/reports")
@login_required
def reports() -> Any:
    return render_template(
        "reports.html",
        username=session["username"]
    )



# User Management
@app.route("/user-management")
@admin_required
def user_management() -> Any:
    return render_template(
        "user_management.html",
        username=session["username"]
    )



@app.post("/logout")
@login_required
def logout() -> Any:
    write_audit_log(
        "logout",
        "Operator signed out.",
    )
    stop_sorting_runtime()
    session.clear()
    return redirect(url_for("login"))


@app.cli.command("show-admin-google-id")
def show_admin_google_id() -> None:
    """Print the Google subject ID captured for the administrator."""
    admin = User.query.filter(
        func.lower(User.email) == INITIAL_ADMIN_EMAIL
    ).first()
    if admin is None or not admin.google_sub:
        print(
            "No Google account ID has been captured. Sign in once with the "
            "configured ADMIN_EMAIL, then run this command again."
        )
        return
    print(f"ADMIN_GOOGLE_SUB={admin.google_sub}")


@app.cli.command("check-mail")
def check_mail() -> None:
    """Verify the SMTP settings used to deliver staff invitations."""
    server = app.config["MAIL_SERVER"]
    username = app.config["MAIL_USERNAME"]
    print(f"MAIL_SERVER   = {server or '<empty>'}")
    print(f"MAIL_PORT     = {app.config['MAIL_PORT']}")
    print(f"MAIL_USE_TLS  = {app.config['MAIL_USE_TLS']}")
    print(f"MAIL_USE_SSL  = {app.config['MAIL_USE_SSL']}")
    print(f"MAIL_FROM     = {app.config['MAIL_FROM'] or '<empty>'}")
    print(f"MAIL_USERNAME = {username or '<empty>'}")
    # Never print the password, only whether one is present.
    print(f"MAIL_PASSWORD = {'set' if app.config['MAIL_PASSWORD'] else '<empty>'}")

    if not server:
        print("\nFAIL: MAIL_SERVER is empty. Set it in .env (e.g. smtp.gmail.com).")
        return
    if not app.config["MAIL_FROM"] and not username:
        print("\nFAIL: MAIL_FROM and MAIL_USERNAME are both empty.")
        return
    if not username:
        print(
            "\nWARN: MAIL_USERNAME is empty, so delivery is attempted "
            "unauthenticated."
        )
        return

    try:
        if app.config["MAIL_USE_SSL"]:
            smtp = smtplib.SMTP_SSL(server, app.config["MAIL_PORT"], timeout=15)
        else:
            smtp = smtplib.SMTP(server, app.config["MAIL_PORT"], timeout=15)
        with smtp:
            if app.config["MAIL_USE_TLS"] and not app.config["MAIL_USE_SSL"]:
                smtp.starttls()
            smtp.login(username, app.config["MAIL_PASSWORD"])
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        print(f"\nFAIL: the server was reached but rejected the credentials "
              f"({exc.smtp_code} {detail}).")
        if "outlook" in server or "office365" in server:
            print("Microsoft 365: MAIL_PASSWORD must be this mailbox's real password,")
            print("or a Microsoft app password registered in Entra ID. An app password")
            print("issued by Google can never authenticate here.")
        else:
            print("For Gmail/Google, the app password must belong to the account in")
            print("MAIL_USERNAME, and 2-Step Verification must be enabled on it.")
    except (OSError, smtplib.SMTPException) as exc:
        print(f"\nFAIL: could not complete the connection "
              f"({type(exc).__name__}: {exc}).")
    else:
        print("\nOK: SMTP login succeeded, invitation emails should send.")



if __name__ == "__main__":
    app.run(
        debug=env_bool("FLASK_DEBUG", False),
        threaded=True,
        # Keep the server in the process owned by the current terminal.
        # Werkzeug's reloader starts a second process which can survive after
        # its original VS Code terminal is closed and keep port 5000 occupied.
        use_reloader=False,
    )
