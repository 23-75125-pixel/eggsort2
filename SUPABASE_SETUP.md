# Supabase database setup

1. Create a Supabase project, then open **Connect** in its dashboard.
2. Copy the **Session pooler** URI (port `5432`) for a normal long-running
   Flask server on an IPv4 network. A direct URI is also suitable when the
   machine supports IPv6. Keep `?sslmode=require` on the URI. Do not use the
   Transaction pooler (port `6543`) for this persistent SQLAlchemy process.
   Remove `pgbouncer=true` if it appears: it is a Prisma-specific parameter
   and the application also removes it defensively.
3. URL-encode any special character in the database password (`@` becomes
   `%40`, for example), then set the completed URI in `.env`:

   ```env
   DATABASE_URL=
   SUPABASE_DB_URL=postgresql://postgres.PROJECT_REF:PASSWORD@HOST:5432/postgres?sslmode=require
   ```

4. Install the PostgreSQL driver:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. For a new/empty project, open **Supabase Dashboard > SQL Editor**, paste and
   run the complete contents of [`supabase/schema.sql`](supabase/schema.sql).
   It creates the four current EggSort+ tables, indexes, and deny-by-default
   Row Level Security. The Flask server uses its private PostgreSQL connection;
   the Supabase browser Data API has no access to these tables.

   To deliberately erase and rebuild an existing EggSort+ schema, run
   [`supabase/reset_schema.sql`](supabase/reset_schema.sql) instead. It drops
   only the EggSort+ application tables, including the retired `sale` table.

6. Skip this step for a clean start. To copy existing local data instead, the
   migration is safe to rerun: rows already copied with the same IDs are
   skipped.

   ```powershell
   .\.venv\Scripts\python.exe migrate_sqlite_to_supabase.py
   ```

7. Start the application normally. It creates any missing tables in Supabase
   automatically.

   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```

8. Confirm the application is actually connected to Supabase and inspect the
   persisted record counts (it does not print credentials):

   ```powershell
   .\.venv\Scripts\python.exe verify_supabase.py
   ```

Do not use the Supabase anon key or service-role key for this Flask database
connection. `SUPABASE_DB_URL` is the PostgreSQL connection URI and should stay
in `.env`, never in source control.
