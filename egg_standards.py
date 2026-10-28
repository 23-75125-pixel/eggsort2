"""Single source of truth for EggSort weight classifications."""

from __future__ import annotations

from typing import Final


SIZE_ORDER: Final[tuple[str, ...]] = (
    "Peewee",
    "Small",
    "Medium",
    "Large",
    "Extra Large",
    "Jumbo",
)

SIZE_CODES: Final[dict[str, str]] = {
    "Peewee": "PEEWEE",
    "Small": "SMALL",
    "Medium": "MEDIUM",
    "Large": "LARGE",
    "Extra Large": "EXTRA_LARGE",
    "Jumbo": "JUMBO",
}


def classify_egg_size(weight_grams: int | float) -> str:
    """Classify with the Roxas Farm client-defined weight boundaries."""
    weight = float(weight_grams)
    if weight <= 55:
        return "Small"
    if weight <= 60:
        return "Medium"
    if weight <= 65:
        return "Large"
    if weight <= 70:
        return "Extra Large"
    return "Jumbo"


def servo_command(size: str) -> str:
    try:
        return f"SORT:{SIZE_CODES[size]}"
    except KeyError as exc:
        raise ValueError(f"Unsupported egg size: {size}") from exc
