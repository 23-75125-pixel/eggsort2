-- PROPOSAL ONLY. Separate explicit cleanup approval required after post-test review.
-- Run as local postgres while connected to the local maintenance database "postgres".
-- DROP DATABASE cannot be transactional. No FORCE and no CASCADE.
\set ON_ERROR_STOP on
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'postgres'
       OR current_user IS DISTINCT FROM 'postgres' THEN
        RAISE EXCEPTION 'Wrong cleanup connection';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
                   WHERE d.datname = 'eggsor_p2b_disposable' AND r.rolname = 'postgres'
                     AND shobj_description(d.oid, 'pg_database') =
                         'EGGSOR_PHASE2B_DISPOSABLE_v1')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eggsor_phase2b_runner') THEN
        RAISE EXCEPTION 'Disposable database or runner is missing or unexpected';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_stat_activity
               WHERE datname = 'eggsor_p2b_disposable' AND pid <> pg_backend_pid()) THEN
        RAISE EXCEPTION 'Active disposable sessions: stop and inspect; do not force-drop';
    END IF;
END;
$$;
DROP DATABASE eggsor_p2b_disposable;
DROP ROLE eggsor_phase2b_runner;
