-- READ ONLY. Run as eggsor_phase2b_runner before any integration-test writes.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_p2b_disposable'
       OR current_user IS DISTINCT FROM 'eggsor_phase2b_runner'
       OR current_setting('search_path') IS DISTINCT FROM 'pg_catalog' THEN
        RAISE EXCEPTION 'Wrong disposable writer connection';
    END IF;
    IF (SELECT array_agg(tablename ORDER BY tablename) FROM pg_tables
        WHERE schemaname = 'eggsor_p2b_test') IS DISTINCT FROM
       ARRAY['audit_log','egg_record','sorting_session','tray_alert','user']::name[]
       OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public') THEN
        RAISE EXCEPTION 'Unexpected fixture or public tables';
    END IF;
    IF (SELECT count(*) FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace cn ON cn.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace pn ON pn.oid = parent.relnamespace
        WHERE c.contype = 'f' AND c.convalidated
          AND cn.nspname = 'eggsor_p2b_test' AND pn.nspname = 'eggsor_p2b_test') <> 3
       OR EXISTS (SELECT 1 FROM pg_constraint c
           JOIN pg_class child ON child.oid = c.conrelid
           JOIN pg_namespace cn ON cn.oid = child.relnamespace
           JOIN pg_class parent ON parent.oid = c.confrelid
           JOIN pg_namespace pn ON pn.oid = parent.relnamespace
           WHERE c.contype = 'f' AND cn.nspname = 'eggsor_p2b_test'
             AND pn.nspname <> 'eggsor_p2b_test') THEN
        RAISE EXCEPTION 'Fixture foreign key isolation failed';
    END IF;
    IF (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'eggsor_p2b_test' AND c.relkind = 'S') <> 4
       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public' AND c.relkind = 'S') THEN
        RAISE EXCEPTION 'Unexpected identity sequence placement';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname = current_user AND
        (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolinherit OR r.rolbypassrls))
       OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                  WHERE r.rolname = current_user)
       OR has_database_privilege(current_user, current_database(), 'CREATE')
       OR has_database_privilege(current_user, current_database(), 'TEMPORARY')
       OR has_schema_privilege(current_user, 'public', 'CREATE')
       OR has_schema_privilege(current_user, 'eggsor_p2b_test', 'CREATE') THEN
        RAISE EXCEPTION 'Writer role has excessive DDL or inherited privileges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname <> 'eggsor_p2b_test'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND ((c.relkind IN ('r','p','v','f') AND
               (has_table_privilege(current_user, c.oid, 'INSERT') OR
                has_table_privilege(current_user, c.oid, 'UPDATE') OR
                has_table_privilege(current_user, c.oid, 'DELETE') OR
                has_table_privilege(current_user, c.oid, 'TRUNCATE') OR
                has_table_privilege(current_user, c.oid, 'REFERENCES') OR
                has_table_privilege(current_user, c.oid, 'TRIGGER')))
            OR (c.relkind = 'S' AND
               (has_sequence_privilege(current_user, c.oid, 'USAGE') OR
                has_sequence_privilege(current_user, c.oid, 'UPDATE'))))
    ) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef AND n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND has_function_privilege(current_user, p.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Writer can modify objects outside the fixture schema';
    END IF;
    IF NOT (has_table_privilege(current_user, 'eggsor_p2b_test."user"', 'SELECT')
            AND has_table_privilege(current_user, 'eggsor_p2b_test."user"', 'INSERT')
            AND has_table_privilege(current_user, 'eggsor_p2b_test.egg_record', 'DELETE')) THEN
        RAISE EXCEPTION 'Writer fixture privileges are incomplete';
    END IF;
    IF (SELECT count(*) FROM eggsor_p2b_test."user") <> 0
       OR (SELECT count(*) FROM eggsor_p2b_test.sorting_session) <> 0
       OR (SELECT count(*) FROM eggsor_p2b_test.egg_record) <> 0
       OR (SELECT count(*) FROM eggsor_p2b_test.audit_log) <> 0
       OR (SELECT count(*) FROM eggsor_p2b_test.tray_alert) <> 0 THEN
        RAISE EXCEPTION 'Fixture schema must be empty before first test run';
    END IF;
END;
$$;
SELECT 'PASS: isolated disposable target, role, schema, FKs, sequences, privileges, and empty fixtures';
COMMIT;
