"""Database initialization, compatibility migrations, and admin bootstrap."""

from __future__ import annotations

import os
import secrets
from typing import Any

from flask import Flask
from sqlalchemy import Float, func, inspect, text
from sqlalchemy.dialects import postgresql
from werkzeug.security import generate_password_hash

from extensions import db
from models import User
from config import ConfigurationError, disposable_write_database_url, local_test_database_url, production_database_url


def initialize_database(
    app: Flask, *, admin_email: str, admin_google_sub: str
) -> None:
    """Create tables, extend legacy user tables, and ensure an administrator."""
    with app.app_context():
        if app.config.get("LOCAL_TEST_MODE"):
            verify_local_test_schema()
            return
        if app.config.get("PHASE2B_WRITE_TEST_MODE"):
            verify_disposable_test_schema()
            return
        if app.config.get("EGGSOR_PRODUCTION_DB_MODE"):
            verify_production_relationship_schema()
            return
        db.create_all()
        _migrate_user_columns()
        _ensure_admin(admin_email, admin_google_sub)


def verify_local_test_schema() -> None:
    """Verify an already-migrated local database without bootstrap or repair."""
    local_test_database_url(db.engine.url)
    with db.engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        target = connection.execute(text(
            "SELECT host(inet_server_addr()), inet_server_port(), current_database()"
        )).one()
        if tuple(target) != ("127.0.0.1", 55432, "eggsor_restore_test"):
            raise ConfigurationError("Connected server is not the isolated local test database.")
        verify_model_schema(inspect(connection))
        valid_fks = connection.execute(text(
            "SELECT count(*) FROM pg_constraint WHERE convalidated AND contype = 'f' "
            "AND (conrelid, conname) IN (( 'public.sorting_session'::regclass, 'fk_sorting_session_user'), "
            "('public.egg_record'::regclass, 'fk_egg_record_sorting_session'), "
            "('public.audit_log'::regclass, 'fk_audit_log_user'))"
        )).scalar_one()
        if valid_fks != 3:
            raise ConfigurationError("Phase 1 foreign keys have not all been validated.")
        # No commit: the read-only inspection transaction is rolled back on close.


def verify_model_schema(inspector: Any, schema: str = "public") -> None:
    """Require mapped tables/columns and the three approved deletion rules."""
    present = set(inspector.get_table_names(schema=schema))
    for table in db.metadata.sorted_tables:
        if table.name not in present:
            raise ConfigurationError("Phase 1 schema missing required table: " + table.name)
        columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=schema)}
        for column in table.columns:
            if column.name not in columns:
                raise ConfigurationError("Phase 1 schema missing column: " + table.name + "." + column.name)
            actual = columns[column.name]
            type_matches = (
                isinstance(column.type, Float) and isinstance(actual["type"], Float)
            ) or actual["type"].compile(dialect=postgresql.dialect()) == column.type.compile(dialect=postgresql.dialect())
            timezone_matches = getattr(actual["type"], "timezone", None) == getattr(column.type, "timezone", None)
            if (not type_matches or not timezone_matches
                    or actual["nullable"] != column.nullable):
                raise ConfigurationError("Phase 1 column definition mismatch: " + table.name + "." + column.name)
        if inspector.get_pk_constraint(table.name, schema=schema)["constrained_columns"] != [c.name for c in table.primary_key.columns]:
            raise ConfigurationError("Phase 1 primary key mismatch: " + table.name)
        for constraint in table.foreign_key_constraints:
            expected_parent_columns = [element.parent.name for element in constraint.elements]
            expected_referred_columns = [element.column.name for element in constraint.elements]
            expected_referred_table = constraint.elements[0].column.table.name
            matches = [fk for fk in inspector.get_foreign_keys(table.name, schema=schema)
                       if fk["name"] == constraint.name
                       and fk["constrained_columns"] == expected_parent_columns
                       and fk["referred_table"] == expected_referred_table
                       and fk.get("referred_schema") in (None, schema)
                       and fk["referred_columns"] == expected_referred_columns
                       and fk.get("options", {}).get("ondelete", "").upper() == constraint.ondelete]
            if not matches:
                raise ConfigurationError("Phase 1 foreign key mismatch: " + str(constraint.name))


def verify_disposable_test_schema() -> None:
    """Read-only startup gate; every check precedes any synthetic fixture write."""
    disposable_write_database_url(db.engine.url)
    schema = "eggsor_p2b_test"
    expected = {table.name for table in db.metadata.sorted_tables}
    with db.engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        identity = connection.execute(text(
            "SELECT host(inet_server_addr()), inet_server_port(), current_database(), "
            "current_user, current_setting('search_path')"
        )).one()
        if tuple(identity) != ("127.0.0.1", 55432, "eggsor_p2b_disposable",
                               "eggsor_phase2b_runner", "pg_catalog"):
            raise ConfigurationError("Disposable database connection identity is incorrect.")
        inspector = inspect(connection)
        if set(inspector.get_table_names(schema=schema)) != expected:
            raise ConfigurationError("Disposable schema does not contain exactly the five expected tables.")
        if inspector.get_table_names(schema="public"):
            raise ConfigurationError("Disposable public schema must contain no application tables.")
        verify_model_schema(inspector, schema=schema)
        fk_count = connection.execute(text("""
            SELECT count(*) FROM pg_constraint c
            JOIN pg_class child ON child.oid = c.conrelid
            JOIN pg_namespace cn ON cn.oid = child.relnamespace
            JOIN pg_class parent ON parent.oid = c.confrelid
            JOIN pg_namespace pn ON pn.oid = parent.relnamespace
            WHERE c.contype = 'f' AND cn.nspname = 'eggsor_p2b_test'
              AND pn.nspname = 'eggsor_p2b_test' AND c.convalidated
        """)).scalar_one()
        if fk_count != 3:
            raise ConfigurationError("Disposable schema must have three validated internal foreign keys.")
        sequence_count = connection.execute(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S' AND n.nspname = 'eggsor_p2b_test'
        """)).scalar_one()
        if sequence_count != 4:
            raise ConfigurationError("Disposable identity sequences are missing or misplaced.")
        permission_error = connection.execute(text("""
            SELECT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls
                    OR r.rolinherit OR NOT r.rolcanlogin
                    OR has_database_privilege(r.rolname, current_database(), 'CREATE')
                    OR has_database_privilege(r.rolname, current_database(), 'TEMPORARY')
                    OR has_schema_privilege(r.rolname, 'public', 'CREATE')
                    OR has_schema_privilege(r.rolname, 'eggsor_p2b_test', 'CREATE')
                    OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid))
            FROM pg_roles r WHERE r.rolname = current_user
        """)).scalar_one()
        if permission_error:
            raise ConfigurationError("Disposable runner has unexpected administrative or DDL privileges.")
        fixture_grants = connection.execute(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'eggsor_p2b_test' AND c.relkind = 'r'
              AND has_table_privilege(current_user, c.oid, 'SELECT')
              AND has_table_privilege(current_user, c.oid, 'INSERT')
              AND has_table_privilege(current_user, c.oid, 'UPDATE')
              AND has_table_privilege(current_user, c.oid, 'DELETE')
        """)).scalar_one()
        sequence_grants = connection.execute(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'eggsor_p2b_test' AND c.relkind = 'S'
              AND has_sequence_privilege(current_user, c.oid, 'USAGE')
        """)).scalar_one()
        if fixture_grants != 5 or sequence_grants != 4 or not connection.execute(text(
                "SELECT has_schema_privilege(current_user, 'eggsor_p2b_test', 'USAGE')"
        )).scalar_one():
            raise ConfigurationError("Disposable runner fixture permissions are incomplete.")
        outside_writes = connection.execute(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('eggsor_p2b_test', 'pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND ((c.relkind IN ('r', 'p', 'v', 'f') AND
                    (has_table_privilege(current_user, c.oid, 'INSERT') OR
                     has_table_privilege(current_user, c.oid, 'UPDATE') OR
                     has_table_privilege(current_user, c.oid, 'DELETE') OR
                     has_table_privilege(current_user, c.oid, 'TRUNCATE') OR
                     has_table_privilege(current_user, c.oid, 'REFERENCES') OR
                     has_table_privilege(current_user, c.oid, 'TRIGGER')))
                   OR (c.relkind = 'S' AND
                    (has_sequence_privilege(current_user, c.oid, 'USAGE') OR
                     has_sequence_privilege(current_user, c.oid, 'UPDATE'))))
        """)).scalar_one()
        if outside_writes:
            raise ConfigurationError("Disposable runner has privileges outside the fixture schema.")
        callable_definers = connection.execute(text("""
            SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE p.prosecdef AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
              AND has_function_privilege(current_user, p.oid, 'EXECUTE')
        """)).scalar_one()
        if callable_definers:
            raise ConfigurationError("Callable SECURITY DEFINER routines require review before writing.")
        # The inspection transaction has no commit and is rolled back on close.


def verify_production_relationship_schema() -> None:
    """Require Phase 1 relationships before a production process can start."""
    production_database_url(db.engine.url)
    with db.engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        identity = connection.execute(text(
            "SELECT current_database(), current_user, session_user, "
            "current_setting('transaction_read_only')"
        )).one()
        if tuple(identity) != ("postgres", "postgres", "postgres", "on"):
            raise ConfigurationError("Production connection identity is incorrect.")
        verify_model_schema(inspect(connection), schema="public")
        valid_fks = connection.execute(text("""
            SELECT count(*) FROM pg_constraint c
            WHERE c.contype = 'f' AND c.convalidated AND
              (c.conrelid, c.conname) IN
              (('public.sorting_session'::regclass, 'fk_sorting_session_user'),
               ('public.egg_record'::regclass, 'fk_egg_record_sorting_session'),
               ('public.audit_log'::regclass, 'fk_audit_log_user'),
               ('public.tray_size_progress'::regclass, 'fk_tray_size_progress_opened_by'),
               ('public.tray_size_progress_egg'::regclass, 'fk_tray_size_progress_egg_record'),
               ('public.tray_size_progress_egg'::regclass, 'fk_tray_size_progress_egg_size'),
               ('public.tray_size_alert'::regclass, 'fk_tray_size_alert_boundary_egg'))
        """)).scalar_one()
        if valid_fks != 7:
            raise ConfigurationError("Production relationships are not fully validated.")
        tray_constraints = connection.execute(text("""
            SELECT count(*) FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname IN ('tray_size_progress', 'tray_size_progress_egg', 'tray_size_alert')
              AND c.convalidated AND c.conname IN (
              'ck_tray_size_progress_supported_size',
              'ck_tray_size_progress_opening_balance',
              'ck_tray_size_progress_nonnegative_counts',
              'ck_tray_size_progress_remaining_range',
              'ck_tray_size_progress_state_consistency',
              'uq_tray_size_progress_egg_id_size',
              'ck_tray_size_alert_supported_size',
              'ck_tray_size_alert_milestone_positive',
              'ck_tray_size_alert_egg_count',
              'uq_tray_size_alert_size_milestone',
              'uq_tray_size_alert_boundary_egg'
            )
        """)).scalar_one()
        if tray_constraints != 11:
            raise ConfigurationError("Per-size tray constraints are missing or unvalidated.")
        owner_rls = connection.execute(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname IN
              ('user','egg_record','audit_log','sorting_session',
               'tray_size_progress','tray_size_progress_egg','tray_size_alert')
              AND c.relrowsecurity AND NOT c.relforcerowsecurity
              AND pg_get_userbyid(c.relowner)=current_user
        """)).scalar_one()
        if owner_rls != 7:
            raise ConfigurationError("Production table ownership or RLS behavior differs from preflight.")


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
