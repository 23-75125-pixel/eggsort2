-- PROPOSAL ONLY. Run with psql as local postgres, connected to local "postgres".
-- CREATE DATABASE cannot be wrapped in a transaction. Stop on any error; no retry.
\set ON_ERROR_STOP on
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'postgres'
       OR current_user IS DISTINCT FROM 'postgres' THEN
        RAISE EXCEPTION 'Wrong local server, maintenance database, or administrator';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'eggsor_p2b_disposable')
       OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eggsor_phase2b_runner') THEN
        RAISE EXCEPTION 'Disposable database or runner already exists: stop and inspect';
    END IF;
END;
$$;
CREATE DATABASE eggsor_p2b_disposable WITH OWNER postgres TEMPLATE template0 ENCODING 'UTF8';
COMMENT ON DATABASE eggsor_p2b_disposable IS 'EGGSOR_PHASE2B_DISPOSABLE_v1';
REVOKE ALL ON DATABASE eggsor_p2b_disposable FROM PUBLIC;
