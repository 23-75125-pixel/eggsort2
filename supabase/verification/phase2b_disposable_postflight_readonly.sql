-- READ ONLY. Run as the restricted runner after the approved integration tests.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_p2b_disposable'
       OR current_user IS DISTINCT FROM 'eggsor_phase2b_runner'
       OR current_setting('search_path') IS DISTINCT FROM 'pg_catalog' THEN
        RAISE EXCEPTION 'Wrong disposable postflight target';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public')
       OR EXISTS (SELECT 1 FROM eggsor_p2b_test.egg_record e
                  LEFT JOIN eggsor_p2b_test.sorting_session s USING (session_ref)
                  WHERE s.session_ref IS NULL)
       OR EXISTS (SELECT 1 FROM eggsor_p2b_test.sorting_session s
                  LEFT JOIN eggsor_p2b_test."user" u ON u.id = s.user_id
                  WHERE s.user_id IS NOT NULL AND u.id IS NULL)
       OR EXISTS (SELECT 1 FROM eggsor_p2b_test.audit_log a
                  LEFT JOIN eggsor_p2b_test."user" u ON u.id = a.user_id
                  WHERE a.user_id IS NOT NULL AND u.id IS NULL) THEN
        RAISE EXCEPTION 'Unexpected public tables or orphaned fixture records';
    END IF;
END;
$$;
SELECT 'user' AS table_name, count(*) AS row_count FROM eggsor_p2b_test."user"
UNION ALL SELECT 'sorting_session', count(*) FROM eggsor_p2b_test.sorting_session
UNION ALL SELECT 'egg_record', count(*) FROM eggsor_p2b_test.egg_record
UNION ALL SELECT 'audit_log', count(*) FROM eggsor_p2b_test.audit_log
UNION ALL SELECT 'tray_alert', count(*) FROM eggsor_p2b_test.tray_alert
ORDER BY table_name;
SELECT status, count(*) FROM eggsor_p2b_test.sorting_session GROUP BY status ORDER BY status;
COMMIT;
