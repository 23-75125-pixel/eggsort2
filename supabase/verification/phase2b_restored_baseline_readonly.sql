-- READ ONLY. Run before setup/tests and after tests against eggsor_restore_test.
-- Save the two deterministic outputs outside the project and compare byte-for-byte.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong restored baseline target';
    END IF;
END;
$$;
DO $$
BEGIN
    IF (SELECT count(*) FROM public."user") <> 5
       OR (SELECT count(*) FROM public.egg_record) <> 166
       OR (SELECT count(*) FROM public.audit_log) <> 406
       OR (SELECT count(*) FROM public.tray_alert) <> 0
       OR (SELECT count(*) FROM public.sorting_session) <> 16 THEN
        RAISE EXCEPTION 'Restored baseline counts differ from Phase 1 verification';
    END IF;
END;
$$;
SELECT 'user' AS table_name, count(*) AS row_count,
       md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), '')) AS fingerprint
FROM public."user" t
UNION ALL SELECT 'egg_record', count(*),
       md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
FROM public.egg_record t
UNION ALL SELECT 'audit_log', count(*),
       md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
FROM public.audit_log t
UNION ALL SELECT 'tray_alert', count(*),
       md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
FROM public.tray_alert t
UNION ALL SELECT 'sorting_session', count(*),
       md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY session_ref), ''))
FROM public.sorting_session t
ORDER BY table_name;
COMMIT;
