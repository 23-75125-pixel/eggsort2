-- Read-only Phase 1 inspection. Run only against the isolated local test DB.
-- No table data, credentials, or actor values are displayed.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong target: inspection requires the isolated local test database';
    END IF;
END;
$$;

SELECT inet_server_addr() AS host, inet_server_port() AS port,
       current_database() AS database;

SELECT expected.table_name,
       to_regclass(format('public.%I', expected.table_name)) IS NOT NULL AS present
FROM (VALUES ('user'), ('egg_record'), ('audit_log'), ('tray_alert'),
             ('sorting_session')) AS expected(table_name);

SELECT table_name, column_name, ordinal_position, data_type, udt_name,
       character_maximum_length, is_nullable, is_identity, identity_generation
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('user', 'egg_record', 'audit_log', 'tray_alert', 'sorting_session')
ORDER BY table_name, ordinal_position;

SELECT c.relname AS table_name, con.conname AS constraint_name,
       con.convalidated AS validated,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
ORDER BY table_name, constraint_name;

SELECT t.relname AS table_name, i.relname AS index_name,
       x.indisvalid AS valid, x.indisready AS ready,
       pg_get_indexdef(i.oid) AS definition
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'public'
ORDER BY table_name, index_name;

SELECT 'user' AS table_name, COUNT(*) AS actual, 5 AS expected FROM public."user"
UNION ALL SELECT 'egg_record', COUNT(*), 166 FROM public.egg_record
UNION ALL SELECT 'audit_log', COUNT(*), 406 FROM public.audit_log
UNION ALL SELECT 'tray_alert', COUNT(*), 0 FROM public.tray_alert;

SELECT COUNT(DISTINCT session_ref) AS distinct_egg_session_refs,
       COUNT(*) FILTER (WHERE session_ref IS NULL) AS null_refs,
       COUNT(*) FILTER (WHERE btrim(session_ref) = '') AS blank_refs,
       MAX(length(session_ref)) AS maximum_reference_length
FROM public.egg_record;

SELECT session_ref, COUNT(*) AS egg_count,
       CASE
           WHEN session_ref IS NULL OR btrim(session_ref) = '' THEN 'review required'
           WHEN upper(btrim(session_ref)) IN ('NO-ACTIVE-SESSION', 'HISTORICAL')
               THEN 'placeholder'
           ELSE 'historical reference'
       END AS classification
FROM public.egg_record
GROUP BY session_ref
ORDER BY session_ref;

SELECT c.relname AS table_name, t.tgname AS trigger_name,
       pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
ORDER BY table_name, trigger_name;

SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled,
       c.relforcerowsecurity AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY table_name;

ROLLBACK;
