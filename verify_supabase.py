"""Verify EggSort's configured database connection and persisted row counts.

Run with:
    .\\.venv\\Scripts\\python.exe verify_supabase.py
"""

import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text

from app import app, db


TABLES = ("user", "egg_record", "tray_alert", "audit_log")
LOCAL_DATABASE = Path("instance/database.db")


def main() -> None:
    with app.app_context():
        dialect = db.engine.dialect.name
        if dialect != "postgresql":
            raise RuntimeError(
                "The application is not using Supabase PostgreSQL. "
                "Set SUPABASE_DB_URL (or DATABASE_URL) in .env."
            )

        db.session.execute(text("SELECT 1")).scalar_one()
        present_tables = set(inspect(db.engine).get_table_names())
        missing_tables = set(TABLES) - present_tables
        if missing_tables:
            raise RuntimeError(
                "Supabase schema is incomplete. Missing: "
                + ", ".join(sorted(missing_tables))
            )

        print("Supabase PostgreSQL connection: OK")
        for table_name in TABLES:
            count = db.session.execute(
                text(f'SELECT COUNT(*) FROM public."{table_name}"')
            ).scalar_one()
            print(f"{table_name}: {count}")

        if LOCAL_DATABASE.is_file():
            with sqlite3.connect(LOCAL_DATABASE) as local_database:
                local_tables = {
                    row[0]
                    for row in local_database.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for table_name in TABLES:
                    if table_name not in local_tables:
                        continue
                    local_count = local_database.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    ).fetchone()[0]
                    remote_count = db.session.execute(
                        text(f'SELECT COUNT(*) FROM public."{table_name}"')
                    ).scalar_one()
                    if local_count > remote_count:
                        raise RuntimeError(
                            f"Supabase is missing {local_count - remote_count} "
                            f"{table_name} row(s). Run migrate_sqlite_to_supabase.py."
                        )
            print("Local SQLite row-count check: OK")


if __name__ == "__main__":
    main()
