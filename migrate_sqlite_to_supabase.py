"""Copy EggSort's local SQLite data into the configured Supabase database.

Run only after setting SUPABASE_DB_URL in .env:
    .\\.venv\\Scripts\\python.exe migrate_sqlite_to_supabase.py
"""

from __future__ import annotations

import sqlite3
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

# Do not seed a new admin before the source user rows are copied.
os.environ["DATABASE_MIGRATION_MODE"] = "1"

from app import app, db


SOURCE_DATABASE = Path("instance/database.db")
TABLES = ("user", "egg_record", "tray_alert", "sale", "audit_log")


def main() -> None:
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        raise RuntimeError(
            "SUPABASE_DB_URL is not configured. Add the Supabase PostgreSQL "
            "URI to .env before migrating."
        )
    if not SOURCE_DATABASE.is_file():
        raise FileNotFoundError(f"SQLite database not found: {SOURCE_DATABASE}")

    with sqlite3.connect(SOURCE_DATABASE) as source, app.app_context():
        source.row_factory = sqlite3.Row
        for table_name in TABLES:
            table = db.metadata.tables[table_name]
            rows = [dict(row) for row in source.execute(f'SELECT * FROM "{table_name}"')]
            if not rows:
                print(f"{table_name}: no rows")
                continue
            statement = insert(table).values(rows).on_conflict_do_nothing(
                index_elements=[table.c.id]
            )
            db.session.execute(statement)
            db.session.commit()
            print(
                f"{table_name}: submitted {len(rows)} rows "
                "(existing IDs are skipped)"
            )

        # Explicit SQLite IDs are retained. Advance every PostgreSQL identity
        # sequence so the next normal Flask insert cannot reuse an imported ID.
        for table_name in TABLES:
            db.session.execute(
                text(
                    "SELECT setval("
                    f"pg_get_serial_sequence('public.\"{table_name}\"', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM public.\"{table_name}\"), 1), "
                    f"(SELECT MAX(id) IS NOT NULL FROM public.\"{table_name}\"))"
                )
            )
        db.session.commit()

    print("Migration complete.")


if __name__ == "__main__":
    main()
