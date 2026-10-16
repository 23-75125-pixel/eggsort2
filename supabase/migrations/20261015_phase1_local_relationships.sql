-- REVIEW DRAFT: do not run until explicitly approved.
-- ONLY for the restored local eggsor_restore_test database. Not for Supabase.
-- Verified local user.id is INTEGER. No Flask integration in this phase.
-- Requires psql and the local administrator. One-time migration; reruns fail safely.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong target: migration is restricted to the isolated local test DB';
    END IF;
END;
$$;

-- Block concurrent writes while preserving and validating the baseline.
LOCK TABLE public."user", public.egg_record, public.audit_log, public.tray_alert
    IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    before_users TEXT;
    before_eggs TEXT;
    before_audits TEXT;
    before_alerts TEXT;
    before_refs TEXT[];
BEGIN
    IF to_regclass('public.sorting_session') IS NOT NULL
       OR EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'audit_log'
             AND column_name = 'user_id'
       ) THEN
        RAISE EXCEPTION 'Migration already applied or unexpected schema: stop and inspect';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'user'
          AND column_name = 'id' AND udt_name = 'int4'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'egg_record'
          AND column_name = 'session_ref' AND udt_name = 'varchar'
          AND character_maximum_length = 40 AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION 'Column types differ from the approved local preflight';
    END IF;
    IF (SELECT COUNT(*) FROM public."user") <> 5
       OR (SELECT COUNT(*) FROM public.egg_record) <> 166
       OR (SELECT COUNT(*) FROM public.audit_log) <> 406
       OR (SELECT COUNT(*) FROM public.tray_alert) <> 0
       OR (SELECT COUNT(DISTINCT session_ref) FROM public.egg_record) <> 16
       OR EXISTS (SELECT 1 FROM public.egg_record
                  WHERE session_ref IS NULL OR btrim(session_ref) = '') THEN
        RAISE EXCEPTION 'Baseline changed: obtain a fresh preflight before migrating';
    END IF;

    -- Internal fingerprints verify original values without displaying them.
    SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
      INTO before_users FROM public."user" t;
    SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
      INTO before_eggs FROM public.egg_record t;
    SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
      INTO before_audits FROM public.audit_log t;
    SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
      INTO before_alerts FROM public.tray_alert t;
    SELECT array_agg(DISTINCT session_ref ORDER BY session_ref)
      INTO before_refs FROM public.egg_record;

    CREATE TABLE public.sorting_session (
        session_ref VARCHAR(40) PRIMARY KEY,
        user_id INTEGER NULL,
        started_at TIMESTAMPTZ NULL,
        ended_at TIMESTAMPTZ NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'starting',
        CONSTRAINT fk_sorting_session_user
            FOREIGN KEY (user_id) REFERENCES public."user"(id)
            ON DELETE SET NULL,
        CONSTRAINT ck_sorting_session_status CHECK (status IN (
            'starting', 'running', 'stopping', 'completed',
            'failed', 'interrupted', 'historical', 'unknown'
        )),
        CONSTRAINT ck_sorting_session_time_order CHECK (
            ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at
        )
    );

    -- Preserve exact references; do not infer owners or lifecycle times.
    INSERT INTO public.sorting_session (session_ref, status)
    SELECT DISTINCT session_ref,
        CASE WHEN upper(btrim(session_ref)) IN ('NO-ACTIVE-SESSION', 'HISTORICAL')
             THEN 'unknown' ELSE 'historical' END
    FROM public.egg_record;

    ALTER TABLE public.audit_log ADD COLUMN user_id INTEGER NULL;
    ALTER TABLE public.audit_log ADD CONSTRAINT fk_audit_log_user
        FOREIGN KEY (user_id) REFERENCES public."user"(id)
        ON DELETE SET NULL NOT VALID;
    ALTER TABLE public.egg_record ADD CONSTRAINT fk_egg_record_sorting_session
        FOREIGN KEY (session_ref) REFERENCES public.sorting_session(session_ref)
        ON DELETE RESTRICT NOT VALID;
    ALTER TABLE public.audit_log VALIDATE CONSTRAINT fk_audit_log_user;
    ALTER TABLE public.egg_record VALIDATE CONSTRAINT fk_egg_record_sorting_session;

    CREATE INDEX ix_sorting_session_user_id ON public.sorting_session(user_id);
    CREATE INDEX ix_audit_log_user_id ON public.audit_log(user_id);
    CREATE INDEX ix_egg_record_session_ref ON public.egg_record(session_ref);

    -- Preserve the existing deny-by-default pattern for application tables.
    -- The local table owner/admin can inspect it. No browser access is granted.
    ALTER TABLE public.sorting_session ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON TABLE public.sorting_session FROM PUBLIC;

    IF (SELECT COUNT(*) FROM public.sorting_session) <> 16
       OR EXISTS (SELECT 1 FROM public.sorting_session
                  WHERE user_id IS NOT NULL OR started_at IS NOT NULL
                     OR ended_at IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.audit_log WHERE user_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.egg_record e
                  LEFT JOIN public.sorting_session s USING (session_ref)
                  WHERE s.session_ref IS NULL)
       OR (SELECT array_agg(session_ref::TEXT ORDER BY session_ref)
           FROM public.sorting_session) IS DISTINCT FROM before_refs THEN
        RAISE EXCEPTION 'Relationship/backfill validation failed';
    END IF;
    IF (SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
        FROM public."user" t) IS DISTINCT FROM before_users
       OR (SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
           FROM public.egg_record t) IS DISTINCT FROM before_eggs
       OR (SELECT md5(COALESCE(string_agg((to_jsonb(t) - 'user_id')::TEXT, '' ORDER BY id), ''))
           FROM public.audit_log t) IS DISTINCT FROM before_audits
       OR (SELECT md5(COALESCE(string_agg(to_jsonb(t)::TEXT, '' ORDER BY id), ''))
           FROM public.tray_alert t) IS DISTINCT FROM before_alerts THEN
        RAISE EXCEPTION 'Original record values changed; transaction must roll back';
    END IF;
    RAISE NOTICE 'Validation passed: original rows preserved; 16 session parents; zero orphan eggs';
END;
$$;

COMMIT;
