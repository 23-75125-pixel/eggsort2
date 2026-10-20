-- READ ONLY. After creating the runner, verify it cannot modify restored public.
-- Run as local postgres against eggsor_restore_test. Never change its grants.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
DECLARE runner_oid oid;
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test'
       OR current_user IS DISTINCT FROM 'postgres' THEN
        RAISE EXCEPTION 'Wrong restored privilege-check target';
    END IF;
    SELECT oid INTO runner_oid FROM pg_roles WHERE rolname = 'eggsor_phase2b_runner';
    IF runner_oid IS NULL THEN
        RAISE EXCEPTION 'Disposable runner does not exist';
    END IF;
    IF has_schema_privilege('eggsor_phase2b_runner', 'public', 'CREATE')
       OR has_database_privilege('eggsor_phase2b_runner', current_database(), 'CREATE')
       OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = runner_oid)
       OR EXISTS (
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public' AND
            ((c.relkind IN ('r','p','v','f') AND
              (has_table_privilege('eggsor_phase2b_runner', c.oid, 'INSERT') OR
               has_table_privilege('eggsor_phase2b_runner', c.oid, 'UPDATE') OR
               has_table_privilege('eggsor_phase2b_runner', c.oid, 'DELETE') OR
               has_table_privilege('eggsor_phase2b_runner', c.oid, 'TRUNCATE') OR
               has_table_privilege('eggsor_phase2b_runner', c.oid, 'REFERENCES') OR
               has_table_privilege('eggsor_phase2b_runner', c.oid, 'TRIGGER')))
             OR (c.relkind = 'S' AND
              (has_sequence_privilege('eggsor_phase2b_runner', c.oid, 'USAGE') OR
               has_sequence_privilege('eggsor_phase2b_runner', c.oid, 'UPDATE'))))
       ) OR EXISTS (
          SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE n.nspname = 'public' AND p.prosecdef
            AND has_function_privilege('eggsor_phase2b_runner', p.oid, 'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Runner has a path to modify the restored database; stop';
    END IF;
END;
$$;
SELECT 'PASS: disposable runner has no public write path in restored database';
COMMIT;
