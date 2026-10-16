-- Read-only verification AFTER the separately approved Phase 1 migration.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong target: local test database required';
    END IF;
END;
$$;

SELECT 'user' AS table_name, COUNT(*) AS actual, 5 AS expected FROM public."user"
UNION ALL SELECT 'egg_record', COUNT(*), 166 FROM public.egg_record
UNION ALL SELECT 'audit_log', COUNT(*), 406 FROM public.audit_log
UNION ALL SELECT 'tray_alert', COUNT(*), 0 FROM public.tray_alert
UNION ALL SELECT 'sorting_session', COUNT(*), 16 FROM public.sorting_session;

SELECT table_name, column_name, data_type, character_maximum_length,
       is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (table_name = 'sorting_session'
       OR (table_name = 'audit_log' AND column_name = 'user_id'))
ORDER BY table_name, ordinal_position;

SELECT con.conname, con.convalidated AS validated,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
WHERE con.conrelid IN ('public.sorting_session'::regclass,
                      'public.egg_record'::regclass, 'public.audit_log'::regclass)
  AND con.contype = 'f'
ORDER BY con.conname;

SELECT COUNT(*) AS orphaned_eggs
FROM public.egg_record e LEFT JOIN public.sorting_session s USING (session_ref)
WHERE s.session_ref IS NULL;
SELECT COUNT(*) AS orphaned_session_users
FROM public.sorting_session s LEFT JOIN public."user" u ON u.id = s.user_id
WHERE s.user_id IS NOT NULL AND u.id IS NULL;
SELECT COUNT(*) AS orphaned_audit_users
FROM public.audit_log a LEFT JOIN public."user" u ON u.id = a.user_id
WHERE a.user_id IS NOT NULL AND u.id IS NULL;

SELECT s.session_ref, s.status, s.user_id, s.started_at, s.ended_at,
       COUNT(e.id) AS egg_count
FROM public.sorting_session s LEFT JOIN public.egg_record e USING (session_ref)
GROUP BY s.session_ref ORDER BY s.session_ref;
SELECT COUNT(*) AS historical_audits_with_guessed_user
FROM public.audit_log WHERE user_id IS NOT NULL;

SELECT COUNT(*) AS index_count,
       bool_and(x.indisvalid AND x.indisready) AS all_indexes_healthy
FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public';
ROLLBACK;
