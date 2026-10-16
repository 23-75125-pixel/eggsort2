-- SUPERSEDED PROPOSAL: do not use. Phase 2B now targets a separate disposable DB.
-- Retained only for review history; see phase2b_disposable_*_proposed.sql.
-- Fresh isolated schema/role; any existing name is a stop condition.
\set ON_ERROR_STOP on
DO $$ BEGIN RAISE EXCEPTION 'Superseded: use the separate disposable database proposal'; END $$;
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong target';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'eggsor_p2b_write_test')
       OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eggsor_phase2b_runner') THEN
        RAISE EXCEPTION 'Test schema/role already exists: stop and inspect';
    END IF;
    IF (SELECT COUNT(*) FROM public."user") <> 5
       OR (SELECT COUNT(*) FROM public.egg_record) <> 166
       OR (SELECT COUNT(*) FROM public.audit_log) <> 406
       OR (SELECT COUNT(*) FROM public.tray_alert) <> 0
       OR (SELECT COUNT(*) FROM public.sorting_session) <> 16 THEN
        RAISE EXCEPTION 'Public baseline changed';
    END IF;
END;
$$;
CREATE ROLE eggsor_phase2b_runner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
CREATE SCHEMA eggsor_p2b_write_test AUTHORIZATION eggsor_phase2b_runner;
REVOKE ALL ON SCHEMA eggsor_p2b_write_test FROM PUBLIC;
GRANT CONNECT ON DATABASE eggsor_restore_test TO eggsor_phase2b_runner;
SET LOCAL ROLE eggsor_phase2b_runner;


CREATE TABLE eggsor_p2b_write_test.tray_alert (
	id SERIAL NOT NULL,
	tray_number INTEGER NOT NULL,
	egg_count INTEGER NOT NULL,
	session_ref VARCHAR(40) NOT NULL,
	is_read BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tray_number)
)

;


CREATE TABLE eggsor_p2b_write_test."user" (
	id SERIAL NOT NULL,
	username VARCHAR(50) NOT NULL,
	password VARCHAR(200) NOT NULL,
	email VARCHAR(254),
	google_sub VARCHAR(255),
	display_name VARCHAR(120),
	avatar_url VARCHAR(1024),
	role VARCHAR(20) NOT NULL,
	is_active BOOLEAN NOT NULL,
	password_set BOOLEAN NOT NULL,
	invite_token_hash VARCHAR(64),
	invite_expires_at TIMESTAMP WITH TIME ZONE,
	password_reset_token_hash VARCHAR(64),
	password_reset_expires_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (username),
	UNIQUE (email),
	UNIQUE (google_sub)
)

;


CREATE TABLE eggsor_p2b_write_test.audit_log (
	id SERIAL NOT NULL,
	user_id INTEGER,
	event_type VARCHAR(40) NOT NULL,
	actor VARCHAR(80) NOT NULL,
	description VARCHAR(300) NOT NULL,
	event_key VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (event_key),
	CONSTRAINT fk_audit_log_user FOREIGN KEY(user_id) REFERENCES eggsor_p2b_write_test."user" (id) ON DELETE SET NULL
)

;

CREATE INDEX ix_eggsor_p2b_write_test_audit_log_user_id ON eggsor_p2b_write_test.audit_log (user_id);


CREATE TABLE eggsor_p2b_write_test.sorting_session (
	session_ref VARCHAR(40) NOT NULL,
	user_id INTEGER,
	started_at TIMESTAMP WITH TIME ZONE,
	ended_at TIMESTAMP WITH TIME ZONE,
	status VARCHAR(20) DEFAULT 'starting' NOT NULL,
	PRIMARY KEY (session_ref),
	CONSTRAINT ck_sorting_session_time_order CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
	CONSTRAINT ck_sorting_session_status CHECK (status IN ('starting', 'running', 'stopping', 'completed', 'failed', 'interrupted', 'historical', 'unknown')),
	CONSTRAINT fk_sorting_session_user FOREIGN KEY(user_id) REFERENCES eggsor_p2b_write_test."user" (id) ON DELETE SET NULL
)

;

CREATE INDEX ix_eggsor_p2b_write_test_sorting_session_user_id ON eggsor_p2b_write_test.sorting_session (user_id);


CREATE TABLE eggsor_p2b_write_test.egg_record (
	id SERIAL NOT NULL,
	weight_grams INTEGER NOT NULL,
	size VARCHAR(30) NOT NULL,
	quality VARCHAR(30) NOT NULL,
	confidence FLOAT NOT NULL,
	session_ref VARCHAR(40) NOT NULL,
	sorted_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_egg_record_sorting_session FOREIGN KEY(session_ref) REFERENCES eggsor_p2b_write_test.sorting_session (session_ref) ON DELETE RESTRICT
)

;

CREATE INDEX ix_eggsor_p2b_write_test_egg_record_session_ref ON eggsor_p2b_write_test.egg_record (session_ref);

RESET ROLE;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES ('user'), ('egg_record'), ('audit_log'),
                              ('tray_alert'), ('sorting_session')) AS tables(name)
        WHERE has_table_privilege('eggsor_phase2b_runner',
            format('public.%I', name), 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    ) THEN
        RAISE EXCEPTION 'Runner unexpectedly has public write privileges';
    END IF;
END;
$$;
COMMIT;
-- A password must be set privately via psql \password eggsor_phase2b_runner.
-- Do not put a password in this file or supply it through command arguments.
