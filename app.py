import hashlib
import ipaddress
import secrets
import smtplib
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from threading import Event, RLock, Thread
from time import monotonic, sleep
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

from config import build_app_config, env_bool, env_int

# Local inspection never reads the production .env or initializes an engine
# until the dedicated target has passed configuration validation.
if not (env_bool("LOCAL_TEST_MODE", False) or env_bool("PHASE2B_WRITE_TEST_MODE", False)):
    load_dotenv()
application_config = build_app_config()

from camera_session import CAMERA_SESSION, CameraSessionError
from database import initialize_database
from egg_standards import SIZE_ORDER, classify_egg_size
from esp32_bridge import ESP32_BRIDGE
from detection_service import (
    DetectorUnavailableError,
    InvalidFrameError,
    detect_frame,
)
from extensions import db, oauth
from models import (
    AuditLog,
    PasswordResetAttempt,
    EggRecord,
    SortingSession,
    TraySizeAlert,
    TraySizeProgress,
    TraySizeProgressEgg,
    TRAY_CAPACITY,
    TRAY_TRACKED_SIZES,
    User,
)
from sorting_lifecycle import AutomaticStartSuppressed, LifecycleError, SortingLifecycle, SQLAlchemySessionStore
from security import AttemptLimiter, init_web_security
from validation import (
    ValidationError,
    is_valid_email,
    normalize_avatar_url,
    parse_staff_profile,
)

app = Flask(__name__)
app.config.from_mapping(application_config)
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
PASSWORD_RESET_WINDOW = timedelta(
    seconds=env_int("PASSWORD_RESET_WINDOW_SECONDS", 900, minimum=60, maximum=86_400)
)
PASSWORD_RESET_MAX_PER_IDENTIFIER = env_int(
    "PASSWORD_RESET_MAX_PER_IDENTIFIER",
    3,
    minimum=1,
    maximum=50,
)
PASSWORD_RESET_MAX_PER_IP = env_int(
    "PASSWORD_RESET_MAX_PER_IP",
    20,
    minimum=1,
    maximum=500,
)
PASSWORD_RESET_COOLDOWN = timedelta(
    seconds=env_int(
        "PASSWORD_RESET_COOLDOWN_SECONDS",
        60,
        minimum=0,
        maximum=3_600,
    )
)
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


def password_reset_identifier_hash(identifier: str) -> str:
    return invite_token_hash(identifier.strip().lower())


def password_reset_client_ip() -> str:
    # Do not trust client-supplied forwarded headers for throttling.
    try:
        return str(ipaddress.ip_address(request.remote_addr or ""))
    except ValueError:
        return "unknown"


def password_reset_request_is_limited(identifier_hash: str, client_ip: str,
                                      *, now: datetime) -> bool:
    recent = PasswordResetAttempt.query.filter(
        PasswordResetAttempt.created_at > now - PASSWORD_RESET_WINDOW
    )
    identifier_attempts = recent.filter_by(identifier_hash=identifier_hash)
    return (
        identifier_attempts.count() >= PASSWORD_RESET_MAX_PER_IDENTIFIER
        or recent.filter_by(ip_address=client_ip).count() >= PASSWORD_RESET_MAX_PER_IP
        or PasswordResetAttempt.query.filter(
            PasswordResetAttempt.identifier_hash == identifier_hash,
            PasswordResetAttempt.created_at > now - PASSWORD_RESET_COOLDOWN,
        ).first() is not None
    )


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


def open_mail_connection() -> tuple[Any, bool]:
    """Connect using configured SMTP, with Gmail's implicit TLS port as fallback."""
    server = app.config["MAIL_SERVER"]
    port = app.config["MAIL_PORT"]
    # Port 465 is implicit TLS. Accommodate MAIL_PORT=465 even if a local
    # .env still has the former port-587 MAIL_USE_SSL=0 setting.
    use_ssl = app.config["MAIL_USE_SSL"] or port == 465
    if use_ssl:
        return smtplib.SMTP_SSL(server, port, timeout=15), True

    try:
        return smtplib.SMTP(server, port, timeout=15), False
    except (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
        if port != 587 or not app.config["MAIL_USE_TLS"]:
            raise
        app.logger.warning(
            "SMTP connection to %s:587 failed; retrying with implicit TLS on port 465",
            server,
        )
        return smtplib.SMTP_SSL(server, 465, timeout=15), True


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
        smtp, implicit_tls = open_mail_connection()
        with smtp:
            if app.config["MAIL_USE_TLS"] and not implicit_tls:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(
                    app.config["MAIL_USERNAME"],
                    app.config["MAIL_PASSWORD"],
                )
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        app.logger.exception("Staff invitation email delivery failed")
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            if "gmail" in app.config["MAIL_SERVER"].lower():
                detail = (
                    "Gmail rejected the SMTP login. Check that MAIL_USERNAME "
                    "is the sending Gmail account and MAIL_PASSWORD is its "
                    "current Google app password (with 2-Step Verification enabled)."
                )
            else:
                detail = (
                    "The mail server rejected the login. Check MAIL_USERNAME "
                    "and use an app password or SMTP credential supported by "
                    "your mail provider."
                )
        elif isinstance(exc, smtplib.SMTPRecipientsRefused):
            detail = "The recipient address was refused by the mail server. Check the staff email address."
        elif isinstance(exc, (smtplib.SMTPConnectError, OSError)):
            ports = (
                "587 and fallback 465"
                if app.config["MAIL_PORT"] == 587
                and app.config["MAIL_USE_TLS"]
                else str(app.config["MAIL_PORT"])
            )
            detail = (
                f"The app could not connect to the mail server on port(s) {ports}. "
                "Check the internet connection, firewall, and MAIL_SERVER settings."
            )
        elif isinstance(exc, smtplib.SMTPServerDisconnected):
            detail = (
                "The server closed the connection during SMTP login. Check "
                "MAIL_USERNAME and MAIL_PASSWORD; for Gmail, use a current "
                "Google app password for that mailbox with 2-Step Verification enabled."
            )
        else:
            detail = (
                f"The mail server rejected the message ({type(exc).__name__}). "
                "Check the mail settings and server logs."
            )
        raise InvitationDeliveryError(
            f"The invitation was created, but the email could not be sent. {detail}"
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
        smtp, implicit_tls = open_mail_connection()
        with smtp:
            if app.config["MAIL_USE_TLS"] and not implicit_tls:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise InvitationDeliveryError("Password reset email could not be sent.") from exc


def assert_lifecycle_writes_allowed() -> None:
    # Production writes require an explicit switch and verified migrated schema.
    if not app.config.get("LIFECYCLE_WRITES_ENABLED", False):
        raise CameraSessionError("Sorting lifecycle writes are disabled for this database mode.")
    from config import ConfigurationError, disposable_write_database_url, production_database_url
    if app.config.get("EGGSOR_PRODUCTION_DB_MODE"):
        if app.config.get("LOCAL_TEST_MODE") or app.config.get("PHASE2B_WRITE_TEST_MODE"):
            raise CameraSessionError("Database runtime modes cannot be combined.")
        try:
            production_database_url(app.config["SQLALCHEMY_DATABASE_URI"])
        except ConfigurationError:
            raise CameraSessionError("The configured production database target is invalid.") from None
        connection = app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}).get("connect_args", {})
        if (connection.get("host") != "aws-0-ap-northeast-2.pooler.supabase.com"
                or connection.get("port") != 5432 or connection.get("dbname") != "postgres"
                or connection.get("sslmode") != "require"):
            raise CameraSessionError("The production database connection is not pinned.")
        with app.app_context():
            try:
                production_database_url(db.engine.url)
            except ConfigurationError:
                raise CameraSessionError("The active engine is not the EGGSor+ Supabase project.") from None
            if db.engine.get_execution_options().get("schema_translate_map"):
                raise CameraSessionError("Production tables must use the verified public schema.")
        return
    if not app.config.get("PHASE2B_WRITE_TEST_MODE") or app.config.get("LOCAL_TEST_MODE"):
        raise CameraSessionError("Lifecycle writes require the disposable test mode.")
    try:
        url = disposable_write_database_url(app.config["SQLALCHEMY_DATABASE_URI"])
    except ConfigurationError:
        raise CameraSessionError("Lifecycle writes require the isolated disposable database.") from None
    engine_options = app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {})
    mapping = engine_options.get("execution_options", {}).get("schema_translate_map", {})
    connection = engine_options.get("connect_args", {})
    if (url.username != "eggsor_phase2b_runner"
            or mapping != {None: "eggsor_p2b_test"}
            or connection.get("hostaddr") != "127.0.0.1"
            or connection.get("port") != 55432
            or connection.get("dbname") != "eggsor_p2b_disposable"
            or connection.get("options") != "-c search_path=pg_catalog"):
        raise CameraSessionError("Lifecycle writes require the isolated disposable database and restricted runner.")
    # Configuration can change after db.init_app(), while SQLAlchemy keeps its
    # original engine. Check the engine that will execute the write as well.
    with app.app_context():
        try:
            disposable_write_database_url(db.engine.url)
        except ConfigurationError:
            raise CameraSessionError("The active engine is not the disposable database.") from None
        if db.engine.get_execution_options().get("schema_translate_map") != mapping:
            raise CameraSessionError("The active engine does not map to the disposable schema.")


def start_sorting_runtime(user_id: int | None = None, *, automatic: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start camera inference and the ESP32 link as one station runtime."""
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        raise CameraSessionError(
            "Camera and ESP32 controls are available on the sorting computer. "
            "Run EggSort+ there to operate the machine; use this online dashboard "
            "to view records and reports."
        )
    assert_lifecycle_writes_allowed()
    operator_id = user_id if user_id is not None else session.get("user_id") if has_request_context() else None
    user = db.session.get(User, operator_id) if operator_id is not None else None
    if user is None or not user.is_active:
        raise CameraSessionError("An active authenticated operator is required.")
    _require_initialized_tray_sizes()
    try:
        LIFECYCLE.start(user.id, user_display_label(user), automatic=automatic)
    except AutomaticStartSuppressed:
        raise
    except LifecycleError as exc:
        raise CameraSessionError(str(exc)) from None
    return CAMERA_SESSION.status(), ESP32_BRIDGE.status()


def stop_sorting_runtime(*, manual: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stop the ESP32 link before releasing the camera and detector."""
    if manual:
        LIFECYCLE.pause_auto_start()
    if LIFECYCLE.context is not None:
        assert_lifecycle_writes_allowed()
        LIFECYCLE.stop()
    if not EGG_FLOW_PENDING:
        reset_egg_flow()
    return CAMERA_SESSION.status(), ESP32_BRIDGE.status()


def sign_in_user(user: "User", remember: bool = False) -> None:
    session.clear()
    session["user_id"] = user.id
    session["username"] = user_display_label(user)
    session["email"] = user.email or ""
    session["role"] = user.role
    session["avatar_url"] = user.avatar_url or ""
    session.permanent = remember
    session["auth_feedback"] = {
        "icon": "success", "message": "You have signed in successfully."
    }
    if app.config["AUTO_START_SORTING_ON_LOGIN"]:
        if not LIFECYCLE.auto_start_allowed():
            session["sorting_runtime_started"] = False
            session["sorting_runtime_auto_start_suppressed"] = True
        else:
            try:
                start_sorting_runtime(user.id, automatic=True)
                session["sorting_runtime_started"] = True
                session.pop("sorting_runtime_error", None)
            except AutomaticStartSuppressed:
                session["sorting_runtime_started"] = False
                session["sorting_runtime_auto_start_suppressed"] = True
            except Exception as exc:
                app.logger.exception("Unable to auto-start the sorting runtime")
                session["sorting_runtime_started"] = False
                session["sorting_runtime_error"] = str(exc)
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


def parse_timestamp_boundary(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A timezone is required.")
    return parsed.astimezone(timezone.utc)


_AUTHENTICATED_ACTOR = object()


def write_audit_log(
    event_type: str,
    description: str,
    *,
    actor: str | None = None,
    user_id: Any = _AUTHENTICATED_ACTOR,
    event_key: str | None = None,
    created_at: datetime | None = None,
    commit: bool = True,
) -> AuditLog | None:
    if event_key and AuditLog.query.filter_by(event_key=event_key).first():
        return None
    resolved_actor = actor
    if resolved_actor is None and has_request_context():
        resolved_actor = session.get("username")
    if user_id is _AUTHENTICATED_ACTOR:
        user_id = session.get("user_id") if has_request_context() else None
    if user_id is not None and db.session.get(User, user_id) is None:
        user_id = None
    log = AuditLog(
        event_type=event_type,
        actor=resolved_actor or "System",
        user_id=user_id,
        description=description,
        event_key=event_key,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


def write_auth_audit_log(*args: Any, **kwargs: Any) -> None:
    """An audit outage must not undo an established authentication outcome."""
    try:
        write_audit_log(*args, **kwargs)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            app.logger.exception("Authentication audit rollback failed")
        app.logger.exception("Authentication audit write failed")


def _count_verified_good_egg_for_tray(record: EggRecord) -> TraySizeAlert | None:
    """Update one size's durable progress and alert in the egg-save transaction."""
    if record.quality != "Good" or record.quality_verified is not True:
        return None
    if record.size not in TRAY_TRACKED_SIZES:
        raise ValueError(f"Verified Good egg has unsupported tray size: {record.size}")

    # Serialize all updates for this size, including eggs from other sessions.
    progress = (
        TraySizeProgress.query.filter_by(egg_size=record.size)
        .with_for_update().one_or_none()
    )
    if progress is None or progress.opening_balance is None:
        raise RuntimeError(
            f"Admin must initialize the {record.size} tray opening balance before sorting."
        )

    prior_claim = (
        TraySizeProgressEgg.query.filter_by(egg_record_id=record.id)
        .with_for_update().one_or_none()
    )
    if prior_claim is not None:
        if prior_claim.egg_size != record.size:
            raise RuntimeError("Egg Record was already claimed for a different tray size.")
        return None

    db.session.add(TraySizeProgressEgg(egg_record_id=record.id, egg_size=record.size))
    db.session.flush()

    prior_milestones = progress.completed_milestones
    physical_egg_total = (
        progress.opening_balance
        + progress.confirmed_good_eggs_since_opening
        + 1
    )
    new_milestones, remaining = divmod(physical_egg_total, TRAY_CAPACITY)
    progress.confirmed_good_eggs_since_opening += 1
    progress.completed_milestones = new_milestones
    progress.eggs_toward_next_milestone = remaining
    progress.updated_at = datetime.now(timezone.utc)

    if new_milestones <= prior_milestones:
        return None

    alert = TraySizeAlert(
        egg_size=record.size,
        milestone_number=new_milestones,
        eligible_egg_count_at_milestone=new_milestones * TRAY_CAPACITY,
        boundary_egg_record_id=record.id,
    )
    db.session.add(alert)
    db.session.flush()
    return alert


def _uninitialized_tray_sizes() -> list[str]:
    rows = {row.egg_size: row for row in TraySizeProgress.query.all()}
    return [
        egg_size for egg_size in TRAY_TRACKED_SIZES
        if egg_size not in rows or rows[egg_size].opening_balance is None
    ]


def _require_initialized_tray_sizes() -> None:
    missing = _uninitialized_tray_sizes()
    if missing:
        raise CameraSessionError(
            "Admin must set opening balances for all five egg sizes before sorting: "
            + ", ".join(missing)
        )


EGG_FLOW_LOCK = RLock()
EGG_FLOW_SEQUENCE = 0
EGG_FLOW_STAGE = "idle"
EGG_FLOW_QUALITY: dict[str, Any] | None = None
EGG_FLOW_PENDING: dict[str, Any] | None = None
EGG_FLOW_SAVE_WORKER: Thread | None = None
EGG_FLOW_CONTEXT: dict[str, Any] | None = None
EGG_FLOW_REMOVED = False
EGG_FLOW_INSPECTION_THREAD: Thread | None = None
REJECTED_CAPTURES: dict[tuple[str, int], dict[str, Any]] = {}
REJECT_SAVE_WORKER: Thread | None = None
AUTOMATIC_EGG_CYCLES: dict[tuple[int, int], dict[str, Any]] = {}
AUTOMATIC_RECORD_WORKER: Thread | None = None
AUTOMATIC_RECORD_WAKE = Event()
AUTOMATIC_LAST_CYCLE_ID = 0
BRIDGE_EPOCH: int | None = None
BRIDGE_READY = False
BRIDGE_AUTO_RELEASE = False


def prepare_station_generation():
    global BRIDGE_EPOCH, BRIDGE_READY, BRIDGE_AUTO_RELEASE, AUTOMATIC_LAST_CYCLE_ID
    with EGG_FLOW_LOCK:
        BRIDGE_EPOCH, BRIDGE_READY = None, False
        BRIDGE_AUTO_RELEASE = False
        AUTOMATIC_LAST_CYCLE_ID = 0
        reset_egg_flow()


LIFECYCLE = SortingLifecycle(SQLAlchemySessionStore(app, assert_lifecycle_writes_allowed),
    CAMERA_SESSION, ESP32_BRIDGE,
    incomplete=lambda: bool(AUTOMATIC_EGG_CYCLES) or bool(REJECTED_CAPTURES) or bool(EGG_FLOW_PENDING) or EGG_FLOW_STAGE not in {"idle", "waiting_removal"},
    prepare=prepare_station_generation)


def camera_lifecycle_event(kind, context):
    if kind == "ready":
        LIFECYCLE.ready(context)
    elif kind == "failed":
        LIFECYCLE.fail(context)


CAMERA_SESSION.set_lifecycle_handler(camera_lifecycle_event)


def reset_egg_flow() -> None:
    global EGG_FLOW_SEQUENCE, EGG_FLOW_STAGE
    global EGG_FLOW_QUALITY, EGG_FLOW_PENDING
    global EGG_FLOW_CONTEXT, EGG_FLOW_REMOVED, EGG_FLOW_INSPECTION_THREAD
    with EGG_FLOW_LOCK:
        if EGG_FLOW_PENDING is not None:
            # A measured/confirmed egg cannot be discarded by EGG LEFT or stop.
            return
        EGG_FLOW_SEQUENCE += 1
        EGG_FLOW_STAGE = "idle"
        EGG_FLOW_QUALITY = None
        EGG_FLOW_PENDING = None
        EGG_FLOW_CONTEXT = None
        EGG_FLOW_REMOVED = False
        EGG_FLOW_INSPECTION_THREAD = None


def _flow_is_current(
    sequence: int,
    allowed_stages: set[str] | None = None,
) -> bool:
    with EGG_FLOW_LOCK:
        return (
            sequence == EGG_FLOW_SEQUENCE
            and not BRIDGE_AUTO_RELEASE
            and EGG_FLOW_CONTEXT is not None
            and LIFECYCLE.accepts(EGG_FLOW_CONTEXT)
            and (
                allowed_stages is None
                or EGG_FLOW_STAGE in allowed_stages
            )
        )


def _inspect_then_measure(sequence: int) -> None:
    """Match the scale egg to a queued or completed in-place inspection."""
    global EGG_FLOW_STAGE, EGG_FLOW_QUALITY
    context = dict(EGG_FLOW_CONTEXT or {})
    while _flow_is_current(sequence, {"inspecting"}):
        try:
            with EGG_FLOW_LOCK:
                locked_quality = dict(EGG_FLOW_QUALITY) if EGG_FLOW_QUALITY else None
            quality_result = locked_quality or CAMERA_SESSION.wait_for_captured_quality(
                timeout=1.0, is_current=lambda: _flow_is_current(sequence, {"inspecting"}),
                allow_loaded_capture=True,
            )
        except CameraSessionError as exc:
            ESP32_BRIDGE.publish_status(str(exc), "flow_error")
            LIFECYCLE.fail(context)
            return
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
        if not context or not LIFECYCLE.accepts(quality_result) or any(
            quality_result.get(key) != context.get(key)
            for key in ("session_ref", "runtime_generation")
        ):
            ESP32_BRIDGE.publish_status("Capture/session mismatch; recovery required.", "flow_error")
            LIFECYCLE.fail(context)
            return
        captured = {
            **context,
            "label": quality,
            "confidence": float(quality_result["confidence"]),
            "capture_id": quality_result["capture_id"],
            "quality_verified": True,
            "_requires_capture_id": True,
        }
        with EGG_FLOW_LOCK:
            if (
                sequence != EGG_FLOW_SEQUENCE
                or EGG_FLOW_STAGE != "inspecting"
            ):
                return
            EGG_FLOW_QUALITY = captured
        if quality_result.get("claimed_on_scale"):
            ESP32_BRIDGE.publish_camera_capture(quality_result)
        ESP32_BRIDGE.publish_status(
            f"Egg #{captured['capture_id']} matched: {quality}. "
            "Starting weight measurement."
        )

        reported_wait = False
        while _flow_is_current(sequence, {"inspecting"}):
            try:
                with EGG_FLOW_LOCK:
                    if BRIDGE_AUTO_RELEASE:
                        return
                    if (
                        sequence == EGG_FLOW_SEQUENCE
                        and EGG_FLOW_STAGE == "inspecting"
                    ):
                        EGG_FLOW_STAGE = "measuring"
                    else:
                        return
                ESP32_BRIDGE.measure_egg(quality, capture_id=captured["capture_id"])
                return
            except RuntimeError:
                with EGG_FLOW_LOCK:
                    if sequence == EGG_FLOW_SEQUENCE and EGG_FLOW_STAGE == "measuring":
                        EGG_FLOW_STAGE = "inspecting"
                if not reported_wait:
                    ESP32_BRIDGE.publish_status(
                        "Egg held: waiting for the ESP32 connection before weighing.",
                        "flow_error",
                    )
                    reported_wait = True
                sleep(1)


def _run_egg_inspection(sequence: int) -> None:
    global EGG_FLOW_STAGE
    try:
        _inspect_then_measure(sequence)
    except Exception:
        app.logger.exception("Camera-to-load-cell authorization failed")
        with EGG_FLOW_LOCK:
            if sequence != EGG_FLOW_SEQUENCE:
                return
            if EGG_FLOW_STAGE == "measuring" and EGG_FLOW_PENDING is None:
                EGG_FLOW_STAGE = "inspecting"
        ESP32_BRIDGE.publish_status(
            "Camera authorization failed; the pending egg is retained and controller status will retry.",
            "flow_error",
        )


def begin_egg_flow() -> None:
    """Match a scale arrival to its already captured rolling inspection."""
    global EGG_FLOW_SEQUENCE, EGG_FLOW_STAGE
    global EGG_FLOW_QUALITY, EGG_FLOW_PENDING
    global EGG_FLOW_CONTEXT, EGG_FLOW_REMOVED, EGG_FLOW_INSPECTION_THREAD
    with EGG_FLOW_LOCK:
        context = LIFECYCLE.snapshot()
        if BRIDGE_AUTO_RELEASE or not BRIDGE_READY or not LIFECYCLE.accepts(context):
            return
        if EGG_FLOW_STAGE == "inspecting":
            if EGG_FLOW_INSPECTION_THREAD and EGG_FLOW_INSPECTION_THREAD.is_alive():
                return
            if not EGG_FLOW_CONTEXT or EGG_FLOW_CONTEXT.get("connection_epoch") != BRIDGE_EPOCH:
                return
        elif EGG_FLOW_STAGE == "idle":
            EGG_FLOW_CONTEXT = {**context, "connection_epoch": BRIDGE_EPOCH}
            EGG_FLOW_REMOVED = False
            EGG_FLOW_SEQUENCE += 1
            EGG_FLOW_STAGE = "inspecting"
            EGG_FLOW_QUALITY = None
            EGG_FLOW_PENDING = None
        else:
            return
        sequence = EGG_FLOW_SEQUENCE
        EGG_FLOW_INSPECTION_THREAD = Thread(
            target=_run_egg_inspection, args=(sequence,),
            name=f"eggsort-inspection-{sequence}", daemon=True,
        )
        ESP32_BRIDGE.publish_status(
            "Egg on load cell. Matching its pending Good camera capture."
        )
        EGG_FLOW_INSPECTION_THREAD.start()


def queue_sort_after_measurement(event: dict[str, Any]) -> None:
    """Queue a measured egg for persistence as soon as its final weight arrives."""
    global EGG_FLOW_STAGE, EGG_FLOW_PENDING, EGG_FLOW_SAVE_WORKER
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

    # This station permits only one legacy measurement at a time, so the
    # locked camera quality belongs to this physical load-cell cycle. Keep the
    # final measured weight even if older firmware omits or mis-tags IDs.
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 30 <= weight <= 1000:
        ESP32_BRIDGE.publish_status("Invalid final egg weight; recovery required.", "flow_error")
        return
    size = classify_egg_size(int(weight))
    if quality_result["label"] != "Good":
        ESP32_BRIDGE.publish_status("A non-Good camera result reached the load cell; holding for recovery.", "flow_error")
        return
    if event.get("size") != size:
        ESP32_BRIDGE.publish_status(
            f"Controller size {event.get('size')} differs from the measured {size}; recording measured weight.",
        )
    pending = {
        "_context": dict(quality_result),
        "_capture_id": quality_result["capture_id"],
        "weight_grams": int(weight),
        "size": size,
        "quality": quality_result["label"],
        "confidence": quality_result["confidence"],
        # A missing marker must never promote a fallback/unknown capture to a
        # camera-verified egg for persistent per-size tray counting.
        "quality_verified": quality_result.get("quality_verified") is True,
        "session_ref": quality_result["session_ref"],
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
        "Weight confirmed; saving egg record while ESP32 routes it."
    )
    with EGG_FLOW_LOCK:
        if EGG_FLOW_SAVE_WORKER is None or not EGG_FLOW_SAVE_WORKER.is_alive():
            EGG_FLOW_SAVE_WORKER = Thread(
                target=_retry_measured_egg_save,
                name=f"eggsort-weight-record-{quality_result['capture_id']}",
                daemon=True,
            )
            EGG_FLOW_SAVE_WORKER.start()


def _retry_measured_egg_save() -> None:
    """Retry a measured Good egg until its idempotent database write succeeds."""
    global EGG_FLOW_SAVE_WORKER
    while True:
        with EGG_FLOW_LOCK:
            pending = dict(EGG_FLOW_PENDING) if EGG_FLOW_PENDING else None
            stage = EGG_FLOW_STAGE
        if pending is None or stage not in {"sorting", "saving"}:
            with EGG_FLOW_LOCK:
                EGG_FLOW_SAVE_WORKER = None
            return
        context = pending.get("_context", {})
        lifecycle = LIFECYCLE.snapshot()
        if (not LIFECYCLE.accepts(context, saving=True)
                and lifecycle.get("status") != "starting"):
            # Keep the pending record intact for explicit station recovery.
            with EGG_FLOW_LOCK:
                EGG_FLOW_SAVE_WORKER = None
            return
        save_sorted_egg({
            "type": "weight_confirmed",
            "size": pending["size"],
            "capture_id": pending.get("_capture_id"),
        })
        with EGG_FLOW_LOCK:
            saved = EGG_FLOW_PENDING is None
        if saved:
            with EGG_FLOW_LOCK:
                EGG_FLOW_SAVE_WORKER = None
            return
        sleep(1)


def persist_capture(context, payload, *, rejected=False):
    """One atomic egg/audit save, keyed by its original session and capture."""
    assert_lifecycle_writes_allowed()
    with app.app_context():
        try:
            # Match deletion lock order: operator first, then session parent.
            operator = User.query.filter_by(id=context["operator_user_id"]).with_for_update().first()
            parent = SortingSession.query.filter_by(session_ref=context["session_ref"]).with_for_update().one()
            key = f"egg-capture:{context['session_ref']}:{context['capture_id']}"
            if AuditLog.query.filter_by(event_key=key).first():
                return None
            record = EggRecord(**payload)
            db.session.add(record)
            db.session.flush()
            if not rejected:
                _count_verified_good_egg_for_tray(record)
            write_audit_log(
                "egg_rejected" if rejected else "egg_sorted",
                f"EGG-{record.id:06d} {'rejected before weighing' if rejected else 'sorted'}; "
                f"{record.weight_grams} g, {record.size}, quality {record.quality}.",
                actor="System", user_id=operator.id if operator else None,
                event_key=key, commit=False,
            )
            db.session.commit()
            return record.id
        except Exception:
            db.session.rollback()
            raise


def save_sorted_egg(event: dict[str, Any]) -> None:
    """Persist the pending measured egg; duplicate route events stay idempotent."""
    global EGG_FLOW_STAGE, EGG_FLOW_PENDING
    with EGG_FLOW_LOCK:
        # Moving to "saving" before touching the database makes repeated
        # SERVO SORTED lines idempotent: one physical egg can create one row.
        if EGG_FLOW_STAGE != "sorting":
            return
        pending = dict(EGG_FLOW_PENDING) if EGG_FLOW_PENDING else None
        sequence = EGG_FLOW_SEQUENCE
        if pending is not None:
            EGG_FLOW_PENDING["_confirmed"] = True
            EGG_FLOW_STAGE = "saving"
    if pending is None:
        ESP32_BRIDGE.publish_status(
            "Sort confirmation received without a pending egg record.",
            "flow_error",
        )
        return

    confirmed_size = event.get("size")
    if (confirmed_size != pending["size"] or
            (pending.get("_context", {}).get("_requires_capture_id")
             and event.get("capture_id") != pending["_capture_id"])):
        ESP32_BRIDGE.publish_status(
            "ESP32 route confirmation did not match the measured size; "
            "holding the pending record for recovery.",
            "flow_error",
        )
        with EGG_FLOW_LOCK:
            if sequence == EGG_FLOW_SEQUENCE and EGG_FLOW_STAGE == "saving":
                EGG_FLOW_STAGE = "sorting"
                EGG_FLOW_PENDING["_confirmed"] = False
        return

    capture_id = pending.pop("_capture_id")
    context = pending.pop("_context", {})
    pending.pop("_confirmed", None)
    try:
        with LIFECYCLE.persistence(context):
            egg_id = persist_capture(context, pending)
    except Exception:
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
        if sequence == EGG_FLOW_SEQUENCE and EGG_FLOW_STAGE == "saving":
            EGG_FLOW_PENDING = None
            EGG_FLOW_STAGE = "waiting_removal"
            if EGG_FLOW_REMOVED:
                reset_egg_flow()
    ESP32_BRIDGE.publish_status(
        f"Egg #{capture_id} saved as EGG-{egg_id:06d}." if egg_id is not None else f"Egg #{capture_id} was already recorded."
    )


def _save_automatic_egg_cycles() -> None:
    """FIFO recording is independent of the controller's immediate release."""
    global AUTOMATIC_RECORD_WORKER
    while True:
        with EGG_FLOW_LOCK:
            if not AUTOMATIC_EGG_CYCLES:
                AUTOMATIC_RECORD_WORKER = None
                return
            key, cycle = next(iter(AUTOMATIC_EGG_CYCLES.items()))
            context = dict(cycle["context"])
            capture = cycle.get("capture")
            measured = cycle.get("egg_complete")
            sorted_egg = cycle.get("sort_complete")
            allow_loaded = not cycle.get("egg_left") and key == next(reversed(AUTOMATIC_EGG_CYCLES))
        if not LIFECYCLE.accepts(context, saving=True):
            if LIFECYCLE.context and LIFECYCLE.context.matches(context) and LIFECYCLE.snapshot()["status"] == "starting":
                AUTOMATIC_RECORD_WAKE.wait(0.1)
                AUTOMATIC_RECORD_WAKE.clear()
                continue
            with EGG_FLOW_LOCK:
                AUTOMATIC_RECORD_WORKER = None
            return
        try:
            if capture is None:
                capture = CAMERA_SESSION.wait_for_captured_quality(
                    timeout=0.25, allow_loaded_capture=allow_loaded,
                    is_current=lambda: LIFECYCLE.accepts(context, saving=True),
                )
                if capture is not None:
                    if (not LIFECYCLE.context.matches(capture)
                            or str(capture.get("label", "")).lower() != "good"):
                        raise CameraSessionError("Automatic cycle camera context mismatch; record retained.")
                    capture = {**capture, "_quality_verified": True}
                    with EGG_FLOW_LOCK:
                        cycle["capture"] = dict(capture)
            if capture is None and measured is not None and (
                    sorted_egg is not None
                    or monotonic() - cycle.get("detected_at", monotonic()) >= 12.0):
                # The controller has already weighed and released this egg.
                # A missed camera callback must not erase that measurement.
                # Cycle IDs occupy a separate namespace from camera IDs.
                capture = {
                    "capture_id": 1_000_000_000_000
                    + key[0] * 1_000_000 + key[1],
                    "confidence": 0.0,
                    "label": "good",
                    "_quality_verified": False,
                }
                with EGG_FLOW_LOCK:
                    cycle["capture"] = dict(capture)
                ESP32_BRIDGE.publish_status(
                    f"Load-cell cycle #{key[1]} has no camera capture; recording its measured weight."
                )
            if capture is None or measured is None:
                AUTOMATIC_RECORD_WAKE.wait(0.1)
                AUTOMATIC_RECORD_WAKE.clear()
                continue
            weight = measured.get("weight_grams")
            if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                    or not 30 <= weight <= 1000):
                raise CameraSessionError("Automatic cycle has an invalid final load-cell weight; record retained.")
            measured_size = classify_egg_size(weight)
            if measured.get("size") != measured_size or (
                    sorted_egg is not None
                    and sorted_egg.get("size") != measured_size):
                ESP32_BRIDGE.publish_status(
                    f"Load-cell cycle #{key[1]} route size differs; recording size from measured weight."
                )
            record_context = {**context, "capture_id": capture["capture_id"]}
            with LIFECYCLE.persistence(record_context):
                persist_capture(record_context, dict(
                    weight_grams=int(weight), size=measured_size, quality="Good",
                    confidence=float(capture["confidence"]),
                    quality_verified=bool(capture.get("_quality_verified", False)),
                    session_ref=context["session_ref"],
                ))
        except CameraSessionError as exc:
            ESP32_BRIDGE.publish_status(str(exc), "flow_error")
            LIFECYCLE.fail(context)
        except Exception:
            app.logger.exception("Unable to save automatic load-cell cycle")
            ESP32_BRIDGE.publish_status("Automatic egg record retained; database save will retry.", "flow_error")
            AUTOMATIC_RECORD_WAKE.wait(1)
            AUTOMATIC_RECORD_WAKE.clear()
        else:
            with EGG_FLOW_LOCK:
                AUTOMATIC_EGG_CYCLES.pop(key, None)
            ESP32_BRIDGE.publish_status(
                f"Camera egg #{capture['capture_id']} recorded for automatic scale cycle #{key[1]}."
            )


def _handle_automatic_egg_event(event: dict[str, Any]) -> None:
    global AUTOMATIC_RECORD_WORKER, AUTOMATIC_LAST_CYCLE_ID
    kind = event["type"]
    if kind not in {"egg_detected", "egg_complete", "sort_complete", "egg_left"}:
        return
    key = (event["connection_epoch"], event["cycle_id"])
    with EGG_FLOW_LOCK:
        cycle = AUTOMATIC_EGG_CYCLES.get(key)
        if cycle is None:
            # Only an arrival can introduce a physical cycle. Delayed duplicate
            # completion lines cannot create a second camera assignment.
            if kind != "egg_detected" or event["cycle_id"] <= AUTOMATIC_LAST_CYCLE_ID:
                return
            AUTOMATIC_LAST_CYCLE_ID = event["cycle_id"]
            cycle = {"context": dict(event), "detected_at": monotonic()}
            AUTOMATIC_EGG_CYCLES[key] = cycle
        if kind != "egg_detected":
            cycle.setdefault(kind, dict(event))
        AUTOMATIC_RECORD_WAKE.set()
        if AUTOMATIC_RECORD_WORKER is None or not AUTOMATIC_RECORD_WORKER.is_alive():
            AUTOMATIC_RECORD_WORKER = Thread(target=_save_automatic_egg_cycles,
                name="eggsort-automatic-records", daemon=True)
            AUTOMATIC_RECORD_WORKER.start()


def handle_esp32_event(event: dict[str, Any]) -> None:
    global BRIDGE_EPOCH, BRIDGE_READY, BRIDGE_AUTO_RELEASE, EGG_FLOW_REMOVED
    if not LIFECYCLE.context or not LIFECYCLE.context.matches(event):
        return
    event_type = event.get("type")
    if event_type == "connection_open":
        with EGG_FLOW_LOCK:
            reconnecting = BRIDGE_EPOCH is not None
            BRIDGE_EPOCH, BRIDGE_READY = event["connection_epoch"], False
            BRIDGE_AUTO_RELEASE = False
            if reconnecting:
                # Untagged firmware replies cannot safely resume an interrupted cycle.
                LIFECYCLE.fail(event)
        return
    if event_type == "connection_lost":
        BRIDGE_READY = False
        LIFECYCLE.fail(event)
        return
    if event.get("connection_epoch") != BRIDGE_EPOCH:
        return
    if event_type == "controller_protocol":
        BRIDGE_AUTO_RELEASE = event.get("protocol") == "EGGSORT_AUTO_V1"
        return
    if event_type == "measurement_failed":
        reason = str(event.get("reason") or "Unknown controller error")
        ESP32_BRIDGE.publish_status(f"ESP32 measurement failed: {reason}", "flow_error")
        LIFECYCLE.fail(event)
        return
    if event.get("cycle_id") is not None:
        if (LIFECYCLE.accepts(event, saving=True)
                or LIFECYCLE.snapshot()["status"] == "starting"):
            BRIDGE_READY = True
            _handle_automatic_egg_event(event)
        return
    if event_type == "controller_state" and event.get("state") == "WAITING FOR CAMERA":
        BRIDGE_READY = True
        begin_egg_flow()
        return
    if event_type == "ready" or (event_type == "controller_state" and event.get("state") == "READY"):
        BRIDGE_READY = True
        return
    if not BRIDGE_READY or not LIFECYCLE.accepts(event, saving=True):
        return
    if event_type in {"egg_complete", "sort_complete", "egg_left"}:
        if not EGG_FLOW_CONTEXT or EGG_FLOW_CONTEXT.get("connection_epoch") != BRIDGE_EPOCH:
            return
    if event_type == "egg_detected":
        begin_egg_flow()
    elif event_type == "egg_complete":
        queue_sort_after_measurement(event)
    elif event_type == "sort_complete":
        save_sorted_egg(event)
    elif event_type == "egg_left":
        with EGG_FLOW_LOCK:
            EGG_FLOW_REMOVED = True
            if EGG_FLOW_STAGE in {"inspecting", "measuring"}:
                ESP32_BRIDGE.publish_status("Egg left before its measurement completed; clear the station before restarting.", "flow_error")
                LIFECYCLE.fail(event)
                return
            reset_egg_flow()


ESP32_BRIDGE.set_event_handler(handle_esp32_event)
CAMERA_SESSION.set_reject_handler(ESP32_BRIDGE.reject_egg)


def _save_rejected_captures() -> None:
    """Keep database latency off inference; retain failed writes for retry."""
    global REJECT_SAVE_WORKER
    while True:
        with EGG_FLOW_LOCK:
            if not REJECTED_CAPTURES:
                REJECT_SAVE_WORKER = None
                return
            key, capture = next(iter(REJECTED_CAPTURES.items()))
        if not LIFECYCLE.accepts(capture, saving=True):
            # Captures can finish while the station is still starting. Keep
            # their durable-in-process queue alive until the same session is
            # ready, just as the automatic Good-egg recorder does.
            lifecycle = LIFECYCLE.snapshot()
            if (LIFECYCLE.context and LIFECYCLE.context.matches(capture)
                    and lifecycle["status"] == "starting"):
                sleep(0.1)
                continue
            with EGG_FLOW_LOCK:
                REJECT_SAVE_WORKER = None
            return
        try:
            with LIFECYCLE.persistence(capture):
                persist_capture(capture, dict(weight_grams=0, size="N/A", quality=capture["label"].title(),
                    confidence=float(capture.get("confidence", 0.0)), quality_verified=True,
                    session_ref=capture["session_ref"]), rejected=True)
        except Exception:
            app.logger.exception("Unable to persist a camera-rejected egg")
            ESP32_BRIDGE.publish_status(
                "Rejected egg record retained; database save will retry.",
                "flow_error",
            )
            sleep(1)
        else:
            with EGG_FLOW_LOCK:
                REJECTED_CAPTURES.pop(key, None)


def save_camera_capture(capture: dict[str, Any]) -> None:
    """Immediately enqueue rejected eggs; Good eggs wait for scale and sort."""
    global REJECT_SAVE_WORKER
    quality = str(capture.get("label", "")).strip().lower()
    if quality in {"crack", "rotten"}:
        with EGG_FLOW_LOCK:
            key = (capture["session_ref"], capture["capture_id"])
            REJECTED_CAPTURES.setdefault(key, {**capture, "label": quality})
            if REJECT_SAVE_WORKER is None:
                REJECT_SAVE_WORKER = Thread(target=_save_rejected_captures,
                    name="eggsort-rejected-records", daemon=True)
                REJECT_SAVE_WORKER.start()
    ESP32_BRIDGE.publish_camera_capture(capture)


CAMERA_SESSION.set_capture_handler(save_camera_capture)


@app.before_request
def ensure_authenticated_sorting_runtime() -> None:
    """Restore the station runtime for authenticated sessions after restart."""
    if not app.config["LOCAL_HARDWARE_ENABLED"] or not app.config["AUTO_START_SORTING_ON_LOGIN"]:
        return
    if request.endpoint in {"logout", "start_camera", "stop_camera", "static"}:
        return
    if not LIFECYCLE.auto_start_allowed():
        return
    if "user_id" not in session:
        return
    if session.get("sorting_runtime_auto_start_suppressed"):
        return
    user = db.session.get(User, session["user_id"])
    if user is None or not user.is_active:
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
        start_sorting_runtime(user.id, automatic=True)
        session["sorting_runtime_started"] = True
        session.pop("sorting_runtime_error", None)
        session.pop("sorting_runtime_retry_after", None)
    except AutomaticStartSuppressed:
        session["sorting_runtime_started"] = False
        session["sorting_runtime_auto_start_suppressed"] = True
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
            write_auth_audit_log(
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
            write_auth_audit_log(
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
        now = datetime.now(timezone.utc)
        identifier_hash = password_reset_identifier_hash(email)
        client_ip = password_reset_client_ip()
        limited = password_reset_request_is_limited(
            identifier_hash,
            client_ip,
            now=now,
        )
        db.session.add(
            PasswordResetAttempt(
                identifier_hash=identifier_hash,
                ip_address=client_ip,
                created_at=now,
            )
        )
        valid_email = len(email) <= 254 and is_valid_email(email)
        user = User.query.filter(func.lower(User.email) == email).first() if valid_email else None
        if not limited and user is not None and user.is_active and user.password_set:
            token = secrets.token_urlsafe(32)
            user.password_reset_token_hash = invite_token_hash(token)
            user.password_reset_expires_at = now + PASSWORD_RESET_LIFETIME
            db.session.commit()
            try:
                send_password_reset(
                    user,
                    external_url("reset_password", token=token),
                )
            except InvitationDeliveryError:
                app.logger.exception("Password reset email delivery failed")
        else:
            db.session.commit()
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
            # Consume conditionally so two concurrent submissions cannot both
            # use the same token. The existing columns need no migration.
            password_hash = generate_password_hash(password)
            updated = User.query.filter(
                User.id == user.id,
                User.password_reset_token_hash == invite_token_hash(token),
                User.password_reset_expires_at > datetime.now(timezone.utc),
                User.is_active.is_(True),
                User.password_set.is_(True),
            ).update({
                User.password: password_hash,
                User.password_set: True,
                User.password_reset_token_hash: None,
                User.password_reset_expires_at: None,
                User.invite_token_hash: None,
                User.invite_expires_at: None,
            }, synchronize_session=False)
            if updated != 1:
                db.session.rollback()
                return render_template(
                    "reset_password.html", error="This reset link is invalid or has expired.",
                    valid_token=False, notice=None,
                ), 400
            db.session.commit()
            write_auth_audit_log("password_reset", "User reset their password.", actor=user.email or user.username)
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

    # Rollback after an audit outage expires ORM attributes. Preserve the
    # already-validated setup decision without another database read.
    is_admin = user.role == "admin"
    password_is_set = user.password_set
    sign_in_user(user, remember=True)
    write_auth_audit_log(
        "login",
        f"{user.role.title()} signed in successfully with Google.",
        actor=user.email or user.username,
    )
    if is_admin:
        session["google_verified_for_password_setup"] = True
        if not password_is_set:
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
        username=session["username"],
        local_hardware_enabled=app.config["LOCAL_HARDWARE_ENABLED"],
    )


@app.post("/api/detect")
@login_required
def detect() -> Any:
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Image inspection is available on the sorting computer."), 409
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
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Start the camera and ESP32 from EggSort+ on the sorting computer."), 409
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
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Stop the camera and ESP32 from EggSort+ on the sorting computer."), 409
    session["sorting_runtime_auto_start_suppressed"] = True
    try:
        camera_state, hardware_state = stop_sorting_runtime(manual=True)
    except (LifecycleError, CameraSessionError) as exc:
        return jsonify(error=str(exc)), 409
    write_audit_log(
        "camera_stopped",
        "Sorting camera and hardware session stopped manually.",
    )
    return jsonify(camera=camera_state, hardware=hardware_state)


@app.get("/api/camera/status")
@login_required
def camera_status() -> Any:
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Camera status is available on the sorting computer."), 409
    response = jsonify(CAMERA_SESSION.status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/camera/feed")
@login_required
def camera_feed() -> Any:
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Live camera feed is available on the sorting computer."), 409
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
    if not app.config["LOCAL_HARDWARE_ENABLED"]:
        return jsonify(error="Hardware status is available on the sorting computer."), 409
    return jsonify(ESP32_BRIDGE.status())


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
        start_at = parse_timestamp_boundary(request.args.get("start_at"))
        end_before = parse_timestamp_boundary(request.args.get("end_before"))
    except ValueError:
        return jsonify(error="Invalid date filter."), 400
    if ((start_date and end_date and start_date > end_date)
            or (start_at and end_before and start_at >= end_before)):
        return jsonify(error="From date cannot be after To date."), 400

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
    if start_at is not None:
        query = query.filter(EggRecord.sorted_at >= start_at)
    if end_before is not None:
        query = query.filter(EggRecord.sorted_at < end_before)

    records = query.order_by(EggRecord.id.desc()).limit(limit).all()
    return jsonify(
        records=[record.to_dict() for record in records],
        latest_id=max((record.id for record in records), default=after_id),
    )


@app.get("/api/dashboard/stats")
@login_required
def dashboard_stats() -> Any:
    manila_timezone = timezone(timedelta(hours=8))
    today = datetime.now(manila_timezone).date()
    start_date_value = request.args.get("start_date", "").strip()
    end_date_value = request.args.get("end_date", "").strip()
    single_date_value = request.args.get("date", "").strip()
    all_dates = request.args.get("all_dates") == "true"
    def parse_dashboard_date(value: str):
        if not value:
            return None
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.isoformat() != value or not 2 <= parsed.year <= 9998:
            raise ValueError("Unsupported dashboard date")
        return parsed

    try:
        start_date = parse_dashboard_date(start_date_value)
        end_date = parse_dashboard_date(end_date_value)
        single_date = parse_dashboard_date(single_date_value)
    except ValueError:
        return jsonify(error="Dates must use YYYY-MM-DD format, with years 0002 through 9998."), 400

    if start_date is None and end_date is None and single_date is not None:
        start_date = end_date = single_date
    if start_date is None and end_date is None:
        if all_dates:
            end_date = today
            earliest_sorted_at = db.session.query(func.min(EggRecord.sorted_at)).scalar()
            if earliest_sorted_at is None:
                start_date = end_date
            else:
                if not 2 <= earliest_sorted_at.year <= 9998:
                    return jsonify(error="Dashboard dates must be within years 0002 through 9998."), 400
                if earliest_sorted_at.tzinfo is None:
                    earliest_sorted_at = earliest_sorted_at.replace(tzinfo=timezone.utc)
                start_date = min(
                    earliest_sorted_at.astimezone(manila_timezone).date(), end_date
                )
        else:
            end_date = today
            start_date = today - timedelta(days=6)
    elif start_date is None:
        end_date = end_date or today
        earliest_sorted_at = db.session.query(func.min(EggRecord.sorted_at)).scalar()
        if earliest_sorted_at is None:
            start_date = end_date
        else:
            if not 2 <= earliest_sorted_at.year <= 9998:
                return jsonify(error="Dashboard dates must be within years 0002 through 9998."), 400
            if earliest_sorted_at.tzinfo is None:
                earliest_sorted_at = earliest_sorted_at.replace(tzinfo=timezone.utc)
            start_date = min(
                earliest_sorted_at.astimezone(manila_timezone).date(), end_date
            )
    elif end_date is None:
        end_date = max(today, start_date)

    if start_date > end_date:
        return jsonify(error="From date cannot be after To date."), 400
    if start_date.year < 2 or end_date.year > 9998:
        return jsonify(error="Dashboard dates must be within years 0002 through 9998."), 400

    selected_start = datetime.combine(
        start_date, time.min, tzinfo=manila_timezone
    ).astimezone(timezone.utc)
    selected_end = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=manila_timezone
    ).astimezone(timezone.utc)
    selected_records = EggRecord.query.filter(
        EggRecord.sorted_at >= selected_start,
        EggRecord.sorted_at < selected_end,
    )
    total_sorted = selected_records.count()
    size_rows = (
        db.session.query(EggRecord.size, func.count(EggRecord.id))
        .filter(EggRecord.sorted_at >= selected_start, EggRecord.sorted_at < selected_end)
        .group_by(EggRecord.size)
        .all()
    )
    quality_rows = (
        db.session.query(EggRecord.quality, func.count(EggRecord.id))
        .filter(EggRecord.sorted_at >= selected_start, EggRecord.sorted_at < selected_end)
        .group_by(EggRecord.quality)
        .all()
    )
    size_counts = {size: 0 for size in SIZE_ORDER}
    size_counts.update({size: count for size, count in size_rows})
    quality_counts = {quality: count for quality, count in quality_rows}
    good_count = quality_counts.get("Good", 0)
    latest_record = selected_records.order_by(EggRecord.id.desc()).first()
    tray_progress_rows = (
        TraySizeProgress.query.order_by(TraySizeProgress.egg_size.asc()).all()
    )
    trays_completed = sum(row.completed_milestones for row in tray_progress_rows)

    # Use actual Manila dates, rather than combining different weeks' weekdays.
    trend_counts = {}
    recent_records = selected_records.all()
    for record in recent_records:
        sorted_at = record.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        sorted_date = sorted_at.astimezone(manila_timezone).date()
        trend_counts[sorted_date] = trend_counts.get(sorted_date, 0) + 1
    day_count = (end_date - start_date).days + 1
    # Keep zero-filled normal ranges. Very long ranges stay sparse instead of
    # allocating millions of empty chart columns; database queries are unchanged.
    trend_sparse = day_count > 366
    trend_days = sorted(trend_counts) if trend_sparse else [
        start_date + timedelta(days=offset) for offset in range(day_count)
    ]
    stats = dict(
        selected_start_date=start_date.isoformat(),
        selected_end_date=end_date.isoformat(),
        total_sorted=total_sorted,
        trays_completed=trays_completed,
        good_count=good_count,
        quality_rate=round(
            (good_count / total_sorted * 100) if total_sorted else 0,
            1,
        ),
        non_good_count=total_sorted - good_count,
        size_counts=size_counts,
        quality_counts=quality_counts,
        latest_record=latest_record.to_dict() if latest_record else None,
        hardware=ESP32_BRIDGE.status(),
        unread_alerts=TraySizeAlert.query.filter_by(is_read=False).count(),
        tray_progress=[row.to_dict() for row in tray_progress_rows],
        daily_trend_sparse=trend_sparse,
        daily_trend=[
            {
                "date": day.isoformat(),
                "label": day.strftime("%a"),
                "count": trend_counts.get(day, 0),
            }
            for day in trend_days
        ],
    )
    if session.get("role") == "admin":
        recent_audits = (
            AuditLog.query
            .filter(AuditLog.event_type.notin_(("egg_sorted", "egg_rejected")))
            .order_by(AuditLog.id.desc())
            .limit(12)
            .all()
        )
        stats["audit_logs"] = [log.to_dict() for log in recent_audits]
    return jsonify(stats)


@app.post("/api/dashboard/audit-logs/clear")
@admin_required
def clear_dashboard_audit_logs() -> Any:
    return jsonify(error=(
        "Clearing audit logs is disabled pending approval of a safe retention policy. "
        "No audit records were deleted."
    )), 409


@app.get("/api/alerts")
@login_required
def alerts_data() -> Any:
    alert_filter = request.args.get("filter", "all")
    query = TraySizeAlert.query
    if alert_filter == "unread":
        query = query.filter_by(is_read=False)
    elif alert_filter == "read":
        query = query.filter_by(is_read=True)
    alerts_list = (
        query.order_by(TraySizeAlert.id.desc()).limit(MAX_ALERT_RESULTS).all()
    )
    return jsonify(
        alerts=[alert.to_dict() for alert in alerts_list],
        unread_count=TraySizeAlert.query.filter_by(is_read=False).count(),
    )


@app.post("/api/alerts/<alert_id>/read")
@login_required
def mark_alert_read(alert_id: str) -> Any:
    if not alert_id.startswith("size:"):
        return jsonify(error="Invalid alert ID."), 400
    try:
        database_id = int(alert_id.removeprefix("size:"))
    except ValueError:
        return jsonify(error="Invalid alert ID."), 400
    if database_id < 1:
        return jsonify(error="Invalid alert ID."), 400

    alert = db.session.get(TraySizeAlert, database_id)
    if alert is None:
        return jsonify(error="Alert not found."), 404

    if not alert.is_read:
        alert.is_read = True
    alert_data = alert.to_dict()
    unread_count = TraySizeAlert.query.filter_by(is_read=False).count()
    db.session.commit()
    return jsonify(ok=True, alert=alert_data, unread_count=unread_count)


@app.post("/api/alerts/read-all")
@login_required
def mark_all_alerts_read() -> Any:
    TraySizeAlert.query.filter_by(is_read=False).update(
        {"is_read": True},
        synchronize_session=False,
    )
    db.session.commit()
    return jsonify(ok=True, unread_count=0)


@app.get("/api/tray-progress")
@login_required
def tray_progress_data() -> Any:
    rows = TraySizeProgress.query.order_by(TraySizeProgress.egg_size.asc()).all()
    return jsonify(
        sizes=[row.to_dict() for row in rows],
        ready=len(rows) == len(TRAY_TRACKED_SIZES)
        and all(row.opening_balance is not None for row in rows),
    )


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
    stop_failed = False
    try:
        write_auth_audit_log("logout", "Operator signed out.")
        try:
            stop_sorting_runtime()
        except Exception:
            stop_failed = True
            app.logger.exception("Runtime shutdown failed during logout")
            try:
                db.session.rollback()
            except Exception:
                app.logger.exception("Runtime shutdown rollback failed during logout")
    finally:
        session.clear()
    if stop_failed:
        message = (
            "You have been signed out, but stopping the sorting runtime failed. "
            "Hardware shutdown could not be verified. Check the station before continuing."
        )
        session["login_error"] = message
        return render_template("login.html", error=message, notice=None,
                               google_ready=google_is_configured()), 503
    session["auth_feedback"] = {
        "icon": "success", "message": "You have been logged out."
    }
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

    phase = "connecting"
    try:
        smtp, implicit_tls = open_mail_connection()
        with smtp:
            if app.config["MAIL_USE_TLS"] and not implicit_tls:
                phase = "TLS negotiation"
                smtp.starttls()
            phase = "SMTP login"
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
        print(f"\nFAIL: error during {phase} "
              f"({type(exc).__name__}: {exc}).")
        if phase == "SMTP login" and isinstance(
            exc, smtplib.SMTPServerDisconnected
        ):
            print(
                "The server closed the connection during login. For Gmail, "
                "verify 2-Step Verification and create a fresh app password "
                "for MAIL_USERNAME."
            )
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
