"""Reusable input normalization and validation for EggSort+ APIs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,30}$")


class ValidationError(ValueError):
    """Raised when user-controlled input does not satisfy domain rules."""


@dataclass(frozen=True, slots=True)
class StaffProfile:
    email: str
    username: str
    display_name: str
    avatar_url: str | None


def is_valid_email(value: str) -> bool:
    return EMAIL_PATTERN.fullmatch(value) is not None


def normalize_avatar_url(value: Any) -> str | None:
    """Accept only bounded HTTPS image URLs."""
    candidate = str(value or "").strip()[:1024]
    return candidate if candidate.startswith("https://") else None


def parse_staff_profile(payload: Mapping[str, Any]) -> StaffProfile:
    """Normalize and validate a staff create/update request."""
    email = str(payload.get("email", "")).strip().lower()
    username = str(payload.get("username", "")).strip().lower()
    display_name = str(payload.get("display_name", "")).strip()
    if not is_valid_email(email):
        raise ValidationError("Enter a valid email address.")
    if USERNAME_PATTERN.fullmatch(username) is None:
        raise ValidationError(
            "Username must be 3-30 characters using letters, numbers, "
            "dots, underscores, or hyphens."
        )
    if not 2 <= len(display_name) <= 120:
        raise ValidationError("Name must contain 2-120 characters.")
    return StaffProfile(
        email=email,
        username=username,
        display_name=display_name,
        avatar_url=normalize_avatar_url(payload.get("avatar_url")),
    )
