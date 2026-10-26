"""Database models and their API serialization contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from extensions import db


TRAY_CAPACITY = 30
TRAY_TRACKED_SIZES = ("Small", "Medium", "Large", "Extra Large", "Jumbo")


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
    # NULL means a historical/unknown record. Only explicitly camera-verified
    # Good records are eligible for per-size tray progress.
    quality_verified = db.Column(db.Boolean, nullable=True)
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
            "quality_verified": self.quality_verified,
            "session_ref": self.session_ref,
            "sorted_at": _as_utc(self.sorted_at).isoformat(),
        }


class TraySizeProgress(db.Model):
    __tablename__ = "tray_size_progress"
    __table_args__ = (
        db.CheckConstraint(
            "egg_size IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')",
            name="ck_tray_size_progress_supported_size",
        ),
        db.CheckConstraint(
            "opening_balance IS NULL OR opening_balance BETWEEN 0 AND 29",
            name="ck_tray_size_progress_opening_balance",
        ),
        db.CheckConstraint(
            "confirmed_good_eggs_since_opening >= 0 AND completed_milestones >= 0",
            name="ck_tray_size_progress_nonnegative_counts",
        ),
        db.CheckConstraint(
            "eggs_toward_next_milestone IS NULL OR eggs_toward_next_milestone BETWEEN 0 AND 29",
            name="ck_tray_size_progress_remaining_range",
        ),
        db.CheckConstraint(
            "(opening_balance IS NULL AND opening_balance_set_at IS NULL "
            "AND eggs_toward_next_milestone IS NULL "
            "AND confirmed_good_eggs_since_opening = 0 AND completed_milestones = 0) "
            "OR (opening_balance IS NOT NULL AND opening_balance_set_at IS NOT NULL "
            "AND eggs_toward_next_milestone = "
            "((opening_balance::BIGINT + confirmed_good_eggs_since_opening) % 30)::SMALLINT "
            "AND completed_milestones = "
            "((opening_balance::BIGINT + confirmed_good_eggs_since_opening) / 30))",
            name="ck_tray_size_progress_state_consistency",
        ),
    )

    egg_size = db.Column(db.String(30), primary_key=True)
    opening_balance = db.Column(db.SmallInteger, nullable=True)
    opening_balance_set_at = db.Column(db.DateTime(timezone=True), nullable=True)
    opening_balance_set_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "user.id", name="fk_tray_size_progress_opened_by", ondelete="SET NULL"
        ),
        nullable=True,
    )
    confirmed_good_eggs_since_opening = db.Column(
        db.BigInteger, nullable=False, default=0, server_default="0"
    )
    completed_milestones = db.Column(
        db.BigInteger, nullable=False, default=0, server_default="0"
    )
    eggs_toward_next_milestone = db.Column(db.SmallInteger, nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "egg_size": self.egg_size,
            "opening_balance": self.opening_balance,
            "opening_balance_configured": self.opening_balance is not None,
            "confirmed_good_eggs_since_opening": self.confirmed_good_eggs_since_opening,
            "completed_milestones": self.completed_milestones,
            "eggs_toward_next_milestone": self.eggs_toward_next_milestone,
            "updated_at": _as_utc(self.updated_at).isoformat() if self.updated_at else None,
        }


class TraySizeProgressEgg(db.Model):
    __tablename__ = "tray_size_progress_egg"
    __table_args__ = (
        db.UniqueConstraint(
            "egg_record_id", "egg_size", name="uq_tray_size_progress_egg_id_size"
        ),
    )

    egg_record_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "egg_record.id",
            name="fk_tray_size_progress_egg_record",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    egg_size = db.Column(
        db.String(30),
        db.ForeignKey(
            "tray_size_progress.egg_size",
            name="fk_tray_size_progress_egg_size",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    counted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )


class TraySizeAlert(db.Model):
    __tablename__ = "tray_size_alert"
    __table_args__ = (
        db.CheckConstraint(
            "egg_size IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')",
            name="ck_tray_size_alert_supported_size",
        ),
        db.CheckConstraint("milestone_number >= 1", name="ck_tray_size_alert_milestone_positive"),
        db.CheckConstraint(
            "eligible_egg_count_at_milestone = milestone_number * 30",
            name="ck_tray_size_alert_egg_count",
        ),
        db.UniqueConstraint(
            "egg_size", "milestone_number", name="uq_tray_size_alert_size_milestone"
        ),
        db.UniqueConstraint(
            "boundary_egg_record_id", name="uq_tray_size_alert_boundary_egg"
        ),
        db.ForeignKeyConstraint(
            ["boundary_egg_record_id", "egg_size"],
            ["tray_size_progress_egg.egg_record_id", "tray_size_progress_egg.egg_size"],
            name="fk_tray_size_alert_boundary_egg",
            ondelete="RESTRICT",
        ),
        db.Index(
            "ix_tray_size_alert_read_created", "is_read", "created_at", "id"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    egg_size = db.Column(db.String(30), nullable=False)
    milestone_number = db.Column(db.BigInteger, nullable=False)
    eligible_egg_count_at_milestone = db.Column(db.BigInteger, nullable=False)
    boundary_egg_record_id = db.Column(db.Integer, nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": f"size:{self.id}",
            "tray_number": self.milestone_number,
            "egg_count": self.eligible_egg_count_at_milestone,
            "egg_size": self.egg_size,
            "session_ref": None,
            "is_read": self.is_read,
            "created_at": _as_utc(self.created_at).isoformat(),
            "title": f"{self.egg_size} tray {self.milestone_number} completed",
            "message": (
                f"{self.egg_size} tray reached {TRAY_CAPACITY} eggs "
                f"at size-specific milestone {self.milestone_number}."
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


class PasswordResetAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    identifier_hash = db.Column(db.String(64), nullable=False, index=True)
    ip_address = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
