"""Validated application configuration for EggSort+.

Environment parsing lives here so every runtime component uses the same
rules and reports invalid values immediately during startup.
"""

from __future__ import annotations

import os
import re
import secrets
import warnings
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
PLACEHOLDER_SECRET = "replace-with-a-long-random-secret"


class ConfigurationError(ValueError):
    """Raised when an environment variable contains an invalid value."""


def env_text(name: str, default: str = "", *, strip: bool = True) -> str:
    value = os.environ.get(name, default)
    return value.strip() if strip else value


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{name} must be one of: {', '.join(sorted(TRUE_VALUES | FALSE_VALUES))}."
    )


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    _validate_range(name, value, minimum, maximum)
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc
    _validate_range(name, value, minimum, maximum)
    return value


def _validate_range(
    name: str,
    value: int | float,
    minimum: int | float | None,
    maximum: int | float | None,
) -> None:
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}.")


def normalize_database_url(value: str) -> str:
    """Normalize provider-specific PostgreSQL URLs for SQLAlchemy/Psycopg."""
    database_url = value.strip() or "sqlite:///database.db"
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    database_url = re.sub(
        r"([?&])pgbouncer=true(?:&|$)",
        r"\1",
        database_url,
        flags=re.IGNORECASE,
    )
    return database_url.rstrip("?&")


def build_app_config() -> dict[str, Any]:
    """Build Flask configuration from validated environment values."""
    public_base_url = (
        env_text("PUBLIC_BASE_URL") or env_text("RENDER_EXTERNAL_URL")
    ).rstrip("/")
    secret_key = env_text("SECRET_KEY")
    if not secret_key or secret_key == PLACEHOLDER_SECRET:
        secret_key = secrets.token_hex(32)
        warnings.warn(
            "SECRET_KEY is missing or still a placeholder; sessions will be "
            "invalidated after restart. Set a persistent 32+ character value.",
            RuntimeWarning,
            stacklevel=2,
        )
    elif len(secret_key) < 32:
        raise ConfigurationError("SECRET_KEY must contain at least 32 characters.")

    mail_username = env_text("MAIL_USERNAME")
    mail_use_tls = env_bool("MAIL_USE_TLS", True)
    mail_use_ssl = env_bool("MAIL_USE_SSL", False)
    if mail_use_tls and mail_use_ssl:
        raise ConfigurationError(
            "MAIL_USE_TLS and MAIL_USE_SSL cannot both be enabled."
        )

    database_url = normalize_database_url(
        env_text("SUPABASE_DB_URL") or env_text("DATABASE_URL")
    )
    config = {
        "SECRET_KEY": secret_key,
        "PERMANENT_SESSION_LIFETIME": timedelta(days=30),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": public_base_url.startswith("https://"),
        "MAX_CONTENT_LENGTH": env_int(
            "MAX_UPLOAD_MB", 8, minimum=1, maximum=32
        )
        * 1024
        * 1024,
        "GOOGLE_CLIENT_ID": env_text("GOOGLE_CLIENT_ID"),
        "GOOGLE_CLIENT_SECRET": env_text("GOOGLE_CLIENT_SECRET"),
        "ADMIN_EMAIL": env_text("ADMIN_EMAIL", "admin@example.com").lower(),
        "ADMIN_GOOGLE_SUB": env_text("ADMIN_GOOGLE_SUB"),
        "PUBLIC_BASE_URL": public_base_url,
        "GOOGLE_AUTH_CLOCK_SKEW_SECONDS": env_int(
            "GOOGLE_AUTH_CLOCK_SKEW_SECONDS", 120, minimum=0, maximum=600
        ),
        "MAIL_SERVER": env_text("MAIL_SERVER"),
        "MAIL_PORT": env_int("MAIL_PORT", 587, minimum=1, maximum=65535),
        "MAIL_USE_TLS": mail_use_tls,
        "MAIL_USE_SSL": mail_use_ssl,
        "MAIL_USERNAME": mail_username,
        "MAIL_PASSWORD": env_text("MAIL_PASSWORD").replace(" ", ""),
        "MAIL_FROM": env_text("MAIL_FROM", mail_username),
        "AUTO_START_SORTING_ON_LOGIN": env_bool(
            "AUTO_START_SORTING_ON_LOGIN", True
        ),
        "SQLALCHEMY_DATABASE_URI": database_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }
    if public_base_url.startswith("https://"):
        public_host = urlsplit(public_base_url).hostname
        if public_host:
            config["TRUSTED_HOSTS"] = [public_host]
    return config
