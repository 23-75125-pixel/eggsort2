-- REVIEW ONLY. Never run without approval, a fresh backup, and paused egg sorting.
-- The caller MUST first verify the connection URL's project identifier is
-- gtsymkfcoslnobitdheu. PostgreSQL reports current_user=postgres and
-- current_database=postgres for multiple Supabase projects; SQL alone cannot
-- authenticate the project identifier. Do not use the local migration here.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '90s';

DO $$
BEGIN
    IF current_database() IS DISTINCT FROM 'postgres'
       OR current_user IS DISTINCT FROM 'postgres'
       OR session_user IS DISTINCT FROM 'postgres'
       OR pg_is_in_recovery() THEN
        RAISE EXCEPTION 'Unexpected production database identity';
    END IF;
END;
$$;

-- Hold the baseline stable until every constraint and preservation check passes.
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
       OR EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'audit_log'
                    AND column_name = 'user_id') THEN
        RAISE EXCEPTION 'Migration already applied or unexpected schema';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'user'
                     AND column_name = 'id' AND udt_name = 'int4')
       OR NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'egg_record'
                        AND column_name = 'session_ref' AND udt_name = 'varchar'
                        AND character_maximum_length = 40 AND is_nullable = 'NO')
       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'public'
                    AND c.relname IN ('user','egg_record','audit_log','tray_alert')
                    AND (pg_get_userbyid(c.relowner) <> current_user
                         OR NOT c.relrowsecurity OR c.relforcerowsecurity)) THEN
        RAISE EXCEPTION 'Production column, owner, or RLS shape differs from preflight';
    END IF;
    IF (SELECT count(*) FROM public."user") <> 5
       OR (SELECT count(*) FROM public.egg_record) <> 166
       OR (SELECT count(*) FROM public.audit_log) <> 409
       OR (SELECT count(*) FROM public.tray_alert) <> 0
       OR (SELECT count(DISTINCT session_ref) FROM public.egg_record) <> 16
       OR EXISTS (SELECT 1 FROM public.egg_record
                  WHERE session_ref IS NULL OR btrim(session_ref) = '') THEN
        RAISE EXCEPTION 'Live baseline changed: obtain a new read-only preflight';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public'
                 AND c.relname IN ('user','egg_record','audit_log','tray_alert')
                 AND NOT t.tgisinternal) THEN
        RAISE EXCEPTION 'Unexpected custom trigger: stop and review';
    END IF;

    SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
      INTO before_users FROM public."user" t;
    SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
      INTO before_eggs FROM public.egg_record t;
    SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
      INTO before_audits FROM public.audit_log t;
    SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
      INTO before_alerts FROM public.tray_alert t;
    SELECT array_agg(DISTINCT session_ref ORDER BY session_ref)
      INTO before_refs FROM public.egg_record;

    CREATE TABLE public.sorting_session (
        session_ref VARCHAR(40) PRIMARY KEY,
        user_id INTEGER NULL,
        started_at TIMESTAMPTZ NULL,
        ended_at TIMESTAMPTZ NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'starting',
        CONSTRAINT fk_sorting_session_user FOREIGN KEY (user_id)
            REFERENCES public."user"(id) ON DELETE SET NULL,
        CONSTRAINT ck_sorting_session_status CHECK (status IN
            ('starting', 'running', 'stopping', 'completed', 'failed',
             'interrupted', 'historical', 'unknown')),
        CONSTRAINT ck_sorting_session_time_order CHECK
            (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)
    );
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
    ALTER TABLE public.sorting_session ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON TABLE public.sorting_session FROM PUBLIC;

    IF (SELECT count(*) FROM public."user") <> 5
       OR (SELECT count(*) FROM public.egg_record) <> 166
       OR (SELECT count(*) FROM public.audit_log) <> 409
       OR (SELECT count(*) FROM public.tray_alert) <> 0
       OR (SELECT count(*) FROM public.sorting_session) <> 16
       OR EXISTS (SELECT 1 FROM public.sorting_session
                  WHERE user_id IS NOT NULL OR started_at IS NOT NULL
                     OR ended_at IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.audit_log WHERE user_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.egg_record e
                  LEFT JOIN public.sorting_session s USING (session_ref)
                  WHERE s.session_ref IS NULL)
       OR (SELECT array_agg(session_ref::text ORDER BY session_ref)
           FROM public.sorting_session) IS DISTINCT FROM before_refs THEN
        RAISE EXCEPTION 'Row counts or historical relationships changed';
    END IF;
    IF (SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
        FROM public."user" t) IS DISTINCT FROM before_users
       OR (SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
           FROM public.egg_record t) IS DISTINCT FROM before_eggs
       OR (SELECT md5(coalesce(string_agg((to_jsonb(t) - 'user_id')::text, '' ORDER BY id), ''))
           FROM public.audit_log t) IS DISTINCT FROM before_audits
       OR (SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY id), ''))
           FROM public.tray_alert t) IS DISTINCT FROM before_alerts THEN
        RAISE EXCEPTION 'Original row values changed; rollback required';
    END IF;
    RAISE NOTICE 'Preserved 580 original rows; created 16 historical session parents; validated three foreign keys';
END;
$$;
COMMIT;
