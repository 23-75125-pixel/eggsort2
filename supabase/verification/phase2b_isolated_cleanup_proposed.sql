-- SUPERSEDED PROPOSAL: do not use. Phase 2B now targets a separate disposable DB.
-- Retained only for review history; see phase2b_disposable_cleanup_proposed.sql.
-- No CASCADE. Any unexpected dependency or baseline stops the transaction.
\set ON_ERROR_STOP on
DO $$ BEGIN RAISE EXCEPTION 'Superseded: use the separate disposable database proposal'; END $$;
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong cleanup target';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
        WHERE n.nspname = 'eggsor_p2b_write_test' AND r.rolname = 'eggsor_phase2b_runner'
    ) THEN
        RAISE EXCEPTION 'Unexpected schema or owner';
    END IF;
    IF (SELECT COUNT(*) FROM public."user") <> 5
       OR (SELECT COUNT(*) FROM public.egg_record) <> 166
       OR (SELECT COUNT(*) FROM public.audit_log) <> 406
       OR (SELECT COUNT(*) FROM public.tray_alert) <> 0
       OR (SELECT COUNT(*) FROM public.sorting_session) <> 16 THEN
        RAISE EXCEPTION 'Public baseline changed: retain test objects for inspection';
    END IF;
    -- Owned sequences/indexes automatically removed with tables must be isolated.
    IF EXISTS (
        SELECT 1 FROM pg_depend d
        JOIN pg_class parent ON parent.oid = d.refobjid
        JOIN pg_namespace pn ON pn.oid = parent.relnamespace
        JOIN pg_class child ON child.oid = d.objid
        JOIN pg_namespace cn ON cn.oid = child.relnamespace
        WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
          AND pn.nspname = 'eggsor_p2b_write_test'
          AND cn.nspname <> 'eggsor_p2b_write_test'
    ) THEN
        RAISE EXCEPTION 'External relation dependency: stop cleanup';
    END IF;
END;
$$;
DROP TABLE eggsor_p2b_write_test.audit_log RESTRICT;
DROP TABLE eggsor_p2b_write_test.egg_record RESTRICT;
DROP TABLE eggsor_p2b_write_test.tray_alert RESTRICT;
DROP TABLE eggsor_p2b_write_test.sorting_session RESTRICT;
DROP TABLE eggsor_p2b_write_test."user" RESTRICT;
DROP SCHEMA eggsor_p2b_write_test RESTRICT;
REVOKE CONNECT ON DATABASE eggsor_restore_test FROM eggsor_phase2b_runner;
DROP ROLE eggsor_phase2b_runner;
COMMIT;
