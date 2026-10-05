"""Database initialization, compatibility migrations, and admin bootstrap."""

from __future__ import annotations

import os
import secrets

from flask import Flask
from sqlalchemy import func, inspect, text
from werkzeug.security import generate_password_hash

from extensions import db
from models import User


def initialize_database(
    app: Flask, *, admin_email: str, admin_google_sub: str
) -> None:
    """Create tables, extend legacy user tables, and ensure an administrator."""
    with app.app_context():
        db.create_all()
        _migrate_user_columns()
        _ensure_admin(admin_email, admin_google_sub)


def _migrate_user_columns() -> None:
    user_columns = {
        column["name"] for column in inspect(db.engine).get_columns("user")
    }
    is_postgresql = db.engine.dialect.name == "postgresql"
    boolean_default = "TRUE" if is_postgresql else "1"
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if is_postgresql else "DATETIME"
    statements = {
        "email": 'ALTER TABLE "user" ADD COLUMN email VARCHAR(254)',
        "google_sub": 'ALTER TABLE "user" ADD COLUMN google_sub VARCHAR(255)',
        "display_name": 'ALTER TABLE "user" ADD COLUMN display_name VARCHAR(120)',
        "avatar_url": 'ALTER TABLE "user" ADD COLUMN avatar_url VARCHAR(1024)',
        "role": (
            'ALTER TABLE "user" ADD COLUMN role VARCHAR(20) '
            "NOT NULL DEFAULT 'staff'"
        ),
        "is_active": (
            'ALTER TABLE "user" ADD COLUMN is_active BOOLEAN '
            f"NOT NULL DEFAULT {boolean_default}"
        ),
        "password_set": (
            'ALTER TABLE "user" ADD COLUMN password_set BOOLEAN '
            f"NOT NULL DEFAULT {boolean_default}"
        ),
        "invite_token_hash": (
            'ALTER TABLE "user" ADD COLUMN invite_token_hash VARCHAR(64)'
        ),
        "invite_expires_at": (
            'ALTER TABLE "user" ADD COLUMN invite_expires_at '
            f"{timestamp_type}"
        ),
        "password_reset_token_hash": (
            'ALTER TABLE "user" ADD COLUMN password_reset_token_hash VARCHAR(64)'
        ),
        "password_reset_expires_at": (
            'ALTER TABLE "user" ADD COLUMN password_reset_expires_at '
            f"{timestamp_type}"
        ),
    }
    for column_name, statement in statements.items():
        if column_name not in user_columns:
            db.session.execute(text(statement))
    db.session.execute(
        text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email ON "user" (email)')
    )
    db.session.execute(
        text(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_user_google_sub '
            'ON "user" (google_sub)'
        )
    )
    db.session.commit()


def _ensure_admin(admin_email: str, admin_google_sub: str) -> None:
    initial_admin = User.query.filter(
        func.lower(User.email) == admin_email
    ).first()
    if initial_admin is None:
        initial_admin = User.query.filter(func.lower(User.username) == "admin").first()
    if initial_admin is None:
        initial_admin = User.query.filter(
            func.lower(User.username) == admin_email
        ).first()
    if initial_admin is None and User.query.count() == 1:
        initial_admin = User.query.first()

    migration_mode = os.environ.get("DATABASE_MIGRATION_MODE") == "1"
    if initial_admin is None and not migration_mode:
        initial_admin = User(
            username=admin_email,
            password=generate_password_hash(secrets.token_urlsafe(32)),
            email=admin_email,
            google_sub=admin_google_sub or None,
            role="admin",
            is_active=True,
        )
        db.session.add(initial_admin)
    elif initial_admin is not None:
        initial_admin.email = admin_email
        if admin_google_sub:
            initial_admin.google_sub = admin_google_sub
        initial_admin.role = "admin"
        initial_admin.is_active = True
    db.session.commit()
