"""Database models and their API serialization contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from extensions import db


TRAY_CAPACITY = 30


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(254), unique=True)
    google_sub = db.Column(db.String(255), unique=True)
    display_name = db.Column(db.String(120))
    avatar_url = db.Column(db.String(1024))
    role = db.Column(db.String(20), nullable=False, default="staff")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    password_set = db.Column(db.Boolean, nullable=False, default=False)
    invite_token_hash = db.Column(db.String(64))
    invite_expires_at = db.Column(db.DateTime(timezone=True))
    password_reset_token_hash = db.Column(db.String(64))
    password_reset_expires_at = db.Column(db.DateTime(timezone=True))

    sorting_sessions = db.relationship("SortingSession", back_populates="user", passive_deletes="all")
    audit_logs = db.relationship("AuditLog", back_populates="user", passive_deletes="all")


class SortingSession(db.Model):
    __tablename__ = "sorting_session"
    __table_args__ = (
        db.CheckConstraint("status IN ('starting', 'running', 'stopping', 'completed', 'failed', 'interrupted', 'historical', 'unknown')", name="ck_sorting_session_status"),
        db.CheckConstraint("ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at", name="ck_sorting_session_time_order"),
    )
    session_ref = db.Column(db.String(40), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", name="fk_sorting_session_user", ondelete="SET NULL"), nullable=True, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="starting", server_default="starting")
    user = db.relationship("User", back_populates="sorting_sessions")
    egg_records = db.relationship("EggRecord", back_populates="sorting_session", passive_deletes="all")


class EggRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    weight_grams = db.Column(db.Integer, nullable=False)
    size = db.Column(db.String(30), nullable=False)
    quality = db.Column(db.String(30), nullable=False)
    confidence = db.Column(db.Float, nullable=False, default=0.0)
    session_ref = db.Column(db.String(40), db.ForeignKey("sorting_session.session_ref", name="fk_egg_record_sorting_session", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False, index=True)
    sorting_session = db.relationship("SortingSession", back_populates="egg_records")
    sorted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "egg_id": f"EGG-{self.id:06d}",
            # Camera rejects bypass the load cell, so zero is the storage
            # sentinel for an unavailable weight rather than a measurement.
            "weight_grams": self.weight_grams or None,
            "size": self.size,
            "quality": self.quality,
            "confidence": round(self.confidence, 4),
            "session_ref": self.session_ref,
            "sorted_at": _as_utc(self.sorted_at).isoformat(),
        }


class TrayAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, unique=True, nullable=False)
    egg_count = db.Column(db.Integer, nullable=False)
    session_ref = db.Column(db.String(40), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tray_number": self.tray_number,
            "egg_count": self.egg_count,
            "session_ref": self.session_ref,
            "is_read": self.is_read,
            "created_at": _as_utc(self.created_at).isoformat(),
            "title": f"Tray {self.tray_number} completed",
            "message": (
                f"Tray {self.tray_number} reached {TRAY_CAPACITY} sorted eggs "
                f"({self.egg_count} total eggs)."
            ),
        }


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", name="fk_audit_log_user", ondelete="SET NULL"), nullable=True, index=True)
    user = db.relationship("User", back_populates="audit_logs")
    event_type = db.Column(db.String(40), nullable=False)
    actor = db.Column(db.String(80), nullable=False, default="System")
    description = db.Column(db.String(300), nullable=False)
    event_key = db.Column(db.String(100), unique=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "actor": self.actor,
            "description": self.description,
            "created_at": _as_utc(self.created_at).isoformat(),
        }
