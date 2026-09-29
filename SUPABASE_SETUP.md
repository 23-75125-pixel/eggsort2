# Supabase database setup

1. Create a Supabase project, then open **Connect** in its dashboard.
2. Copy the **URI** connection string. Use the pooler URI when the application
   runs over IPv4-only networks. Keep `?sslmode=require` on the URI. Remove
   `pgbouncer=true` if it appears: that is a Prisma-only parameter and Psycopg
   rejects it. The application also removes it defensively.
3. URL-encode any special character in the database password (`@` becomes
   `%40`, for example), then set the completed URI in `.env`:

   ```env
   SUPABASE_DB_URL=postgresql://postgres.PROJECT_REF:PASSWORD@HOST:6543/postgres?sslmode=require
   ```

4. Install the PostgreSQL driver:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. In **Supabase Dashboard > SQL Editor**, paste and run the complete contents
   of [`supabase/schema.sql`](supabase/schema.sql). It creates all EggSort+
   tables, indexes, and deny-by-default Row Level Security. The Flask server
   uses its private PostgreSQL connection; the Supabase browser Data API has no
   access to these tables.

6. Copy existing local data to Supabase. The migration is safe to rerun: rows
   already copied with the same IDs are skipped.

   ```powershell
   .\.venv\Scripts\python.exe migrate_sqlite_to_supabase.py
   ```

7. Start the application normally. It creates any missing tables in Supabase
   automatically.

   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```

Do not use the Supabase anon key or service-role key for this Flask database
connection. `SUPABASE_DB_URL` is the PostgreSQL connection URI and should stay
in `.env`, never in source control.
