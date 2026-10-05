"""Web security controls shared by every EggSort+ route."""

from __future__ import annotations

import secrets
from collections import defaultdict, deque
from hmac import compare_digest
from threading import Lock
from time import monotonic
from typing import Any

from flask import Flask, Response, abort, jsonify, request, session


CSRF_SESSION_KEY = "_csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class AttemptLimiter:
    """Small, thread-safe sliding-window limiter for authentication attempts."""

    MAX_TRACKED_KEYS = 2_048

    def __init__(self, max_attempts: int = 5, window_seconds: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def is_limited(self, key: str) -> bool:
        normalized = key.strip().casefold() or "<blank>"
        with self._lock:
            attempts = self._active_attempts(normalized)
            return len(attempts) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        normalized = key.strip().casefold() or "<blank>"
        with self._lock:
            if (
                normalized not in self._attempts
                and len(self._attempts) >= self.MAX_TRACKED_KEYS
            ):
                self._attempts.pop(next(iter(self._attempts)))
            self._active_attempts(normalized).append(monotonic())

    def reset(self, key: str) -> None:
        normalized = key.strip().casefold() or "<blank>"
        with self._lock:
            self._attempts.pop(normalized, None)

    def _active_attempts(self, key: str) -> deque[float]:
        attempts = self._attempts[key]
        cutoff = monotonic() - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        return attempts


def init_web_security(app: Flask) -> None:
    """Register CSRF protection and conservative response headers."""
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.errorhandler(413)
    def request_too_large(_error: Exception) -> tuple[Response, int] | tuple[str, int]:
        message = "The request exceeds the configured upload limit."
        if request.path.startswith("/api/"):
            return jsonify(error=message), 413
        return message, 413

    @app.before_request
    def validate_csrf_token() -> Response | tuple[Response, int] | None:
        if request.method in SAFE_METHODS:
            return None
        if request.path.startswith("/api/") and "user_id" not in session:
            return jsonify(error="Authentication is required."), 401

        expected = session.get(CSRF_SESSION_KEY, "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied and not request.is_json:
            supplied = request.form.get("_csrf_token", "")
        if not expected or not supplied or not compare_digest(expected, supplied):
            message = "The security token is missing or expired. Refresh and try again."
            if request.path.startswith("/api/"):
                return jsonify(error=message), 400
            abort(400, description=message)
        return None

    @app.after_request
    def apply_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def csrf_token() -> str:
    """Return the current session's CSRF token, creating it when needed."""
    token: Any = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token
