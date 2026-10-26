-- PROPOSED ONLY. DO NOT RUN without explicit approval, a fresh backup,
-- paused Flask/Render writers, and a verified database/project target.
-- This additive migration is intended to be reviewed and tested first in an
-- isolated PostgreSQL database restored with the current public schema.
-- Supabase commonly reports current_database() = 'postgres' for different
-- projects. Verify the project identifier from the connection URL separately.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '45s';

DO $$
BEGIN
    IF current_user IS DISTINCT FROM 'postgres'
       OR session_user IS DISTINCT FROM 'postgres'
       OR pg_is_in_recovery() THEN
        RAISE EXCEPTION 'Unexpected database role or read replica; stop and inspect';
    END IF;

    IF to_regclass('public."user"') IS NULL
       OR to_regclass('public.sorting_session') IS NULL
       OR to_regclass('public.egg_record') IS NULL
       OR to_regclass('public.tray_alert') IS NULL THEN
        RAISE EXCEPTION 'Expected EGGSor+ tables are missing; apply/review prerequisites first';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'egg_record'
          AND column_name = 'quality_verified'
    ) OR to_regclass('public.tray_size_progress') IS NOT NULL
      OR to_regclass('public.tray_size_progress_egg') IS NOT NULL
      OR to_regclass('public.tray_size_alert') IS NOT NULL THEN
        RAISE EXCEPTION 'Per-size tray migration objects already exist; stop and inspect';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN ('egg_record', 'tray_alert')
          AND NOT c.relrowsecurity
    ) THEN
        RAISE EXCEPTION 'Expected row-level security is not enabled; stop and inspect';
    END IF;
END;
$$;

-- Stop concurrent egg writes while taking the preservation snapshot and
-- applying the additive schema changes. The application must also be paused.
LOCK TABLE public.egg_record, public.tray_alert
    IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE _per_size_tray_eggs_before ON COMMIT DROP AS
SELECT id, weight_grams, size, quality, confidence, session_ref, sorted_at
FROM public.egg_record;

CREATE TEMP TABLE _per_size_tray_alerts_before ON COMMIT DROP AS
SELECT id, tray_number, egg_count, session_ref, is_read, created_at
FROM public.tray_alert;

-- NULL means legacy/unknown verification. The current backend can continue
-- inserting rows without specifying this nullable column. Phase 2 must write
-- TRUE only for camera-confirmed quality and FALSE for unverified fallback.
ALTER TABLE public.egg_record
    ADD COLUMN quality_verified BOOLEAN;

COMMENT ON COLUMN public.egg_record.quality_verified IS
    'NULL=legacy/unknown; TRUE=quality verified; FALSE=not verified. Only TRUE Good records count toward size-tray progress.';

-- One serialized progress row per supported physical size. NULL opening_balance
-- means staff have not yet entered the manually counted opening tray balance.
-- The count begins only after Phase 2 establishes that opening balance.
CREATE TABLE public.tray_size_progress (
    egg_size VARCHAR(30) PRIMARY KEY,
    opening_balance SMALLINT,
    opening_balance_set_at TIMESTAMPTZ,
    opening_balance_set_by_user_id INTEGER,
    confirmed_good_eggs_since_opening BIGINT NOT NULL DEFAULT 0,
    completed_milestones BIGINT NOT NULL DEFAULT 0,
    eggs_toward_next_milestone SMALLINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tray_size_progress_supported_size
        CHECK (egg_size IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')),
    CONSTRAINT ck_tray_size_progress_opening_balance
        CHECK (opening_balance IS NULL OR opening_balance BETWEEN 0 AND 29),
    CONSTRAINT ck_tray_size_progress_nonnegative_counts
        CHECK (confirmed_good_eggs_since_opening >= 0 AND completed_milestones >= 0),
    CONSTRAINT ck_tray_size_progress_remaining_range
        CHECK (eggs_toward_next_milestone IS NULL OR eggs_toward_next_milestone BETWEEN 0 AND 29),
    CONSTRAINT fk_tray_size_progress_opened_by
        FOREIGN KEY (opening_balance_set_by_user_id)
        REFERENCES public."user"(id) ON DELETE SET NULL,
    CONSTRAINT ck_tray_size_progress_state_consistency CHECK (
        (
            opening_balance IS NULL
            AND opening_balance_set_at IS NULL
            AND eggs_toward_next_milestone IS NULL
            AND confirmed_good_eggs_since_opening = 0
            AND completed_milestones = 0
        )
        OR
        (
            opening_balance IS NOT NULL
            AND opening_balance_set_at IS NOT NULL
            AND eggs_toward_next_milestone =
                ((opening_balance::BIGINT + confirmed_good_eggs_since_opening) % 30)::SMALLINT
            AND completed_milestones =
                ((opening_balance::BIGINT + confirmed_good_eggs_since_opening) / 30)
        )
    )
);

COMMENT ON TABLE public.tray_size_progress IS
    'Persistent per-size packing progress. Opening balances are manually counted incomplete physical trays; updates must lock this row.';

INSERT INTO public.tray_size_progress (egg_size)
VALUES ('Small'), ('Medium'), ('Large'), ('Extra Large'), ('Jumbo');

-- Claims each verified Good Egg Record at most once. Phase 2 must insert this
-- row and update tray_size_progress / tray_size_alert in one transaction; the
-- primary key is the database-level idempotency guard for concurrent retries.
CREATE TABLE public.tray_size_progress_egg (
    egg_record_id INTEGER PRIMARY KEY,
    egg_size VARCHAR(30) NOT NULL,
    counted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_tray_size_progress_egg_record
        FOREIGN KEY (egg_record_id)
        REFERENCES public.egg_record(id) ON DELETE RESTRICT,
    CONSTRAINT fk_tray_size_progress_egg_size
        FOREIGN KEY (egg_size)
        REFERENCES public.tray_size_progress(egg_size) ON DELETE RESTRICT,
    CONSTRAINT uq_tray_size_progress_egg_id_size
        UNIQUE (egg_record_id, egg_size)
);

COMMENT ON TABLE public.tray_size_progress_egg IS
    'Idempotency ledger: a verified Good Egg Record may contribute to per-size tray progress only once.';

-- Keep legacy global alerts, their tray_number uniqueness, read states, and
-- wording untouched. After the future backend cutover, the normal Alerts page
-- and unread badge will use tray_size_alert only; legacy rows remain stored.
-- Before any future cleanup, take a timestamped pg_dump backup of tray_alert,
-- verify its row count and is_read values in a separate restore, retain the
-- backup under the project's retention policy, and obtain explicit approval.
-- Do not drop or truncate tray_alert as part of this migration or cutover.
CREATE TABLE public.tray_size_alert (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    egg_size VARCHAR(30) NOT NULL,
    milestone_number BIGINT NOT NULL,
    eligible_egg_count_at_milestone BIGINT NOT NULL,
    boundary_egg_record_id INTEGER NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tray_size_alert_supported_size
        CHECK (egg_size IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')),
    CONSTRAINT ck_tray_size_alert_milestone_positive
        CHECK (milestone_number >= 1),
    CONSTRAINT ck_tray_size_alert_egg_count
        CHECK (eligible_egg_count_at_milestone = milestone_number * 30),
    CONSTRAINT uq_tray_size_alert_size_milestone
        UNIQUE (egg_size, milestone_number),
    CONSTRAINT uq_tray_size_alert_boundary_egg
        UNIQUE (boundary_egg_record_id),
    CONSTRAINT fk_tray_size_alert_boundary_egg
        FOREIGN KEY (boundary_egg_record_id, egg_size)
        REFERENCES public.tray_size_progress_egg(egg_record_id, egg_size)
        ON DELETE RESTRICT
);

COMMENT ON TABLE public.tray_size_alert IS
    'New per-size 30-egg milestones. Existing tray_alert rows remain legacy/global and are not assigned a size.';

CREATE INDEX ix_tray_size_alert_read_created
    ON public.tray_size_alert (is_read, created_at DESC, id DESC);

CREATE INDEX ix_egg_record_verified_good_size
    ON public.egg_record (size, id)
    WHERE quality = 'Good' AND quality_verified IS TRUE;

-- Match the existing Supabase deny-by-default policy: Flask accesses tables
-- through its private database connection; browser API roles get no table or
-- sequence privileges.
ALTER TABLE public.tray_size_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tray_size_progress_egg ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tray_size_alert ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.tray_size_progress FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.tray_size_progress_egg FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.tray_size_alert FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.tray_size_alert_id_seq FROM PUBLIC, anon, authenticated;

DO $$
BEGIN
    IF EXISTS (
        (SELECT id, weight_grams, size, quality, confidence, session_ref, sorted_at
         FROM _per_size_tray_eggs_before
         EXCEPT
         SELECT id, weight_grams, size, quality, confidence, session_ref, sorted_at
         FROM public.egg_record)
        UNION ALL
        (SELECT id, weight_grams, size, quality, confidence, session_ref, sorted_at
         FROM public.egg_record
         EXCEPT
         SELECT id, weight_grams, size, quality, confidence, session_ref, sorted_at
         FROM _per_size_tray_eggs_before)
    ) THEN
        RAISE EXCEPTION 'Egg Record preservation check failed';
    END IF;

    IF EXISTS (
        (SELECT id, tray_number, egg_count, session_ref, is_read, created_at
         FROM _per_size_tray_alerts_before
         EXCEPT
         SELECT id, tray_number, egg_count, session_ref, is_read, created_at
         FROM public.tray_alert)
        UNION ALL
        (SELECT id, tray_number, egg_count, session_ref, is_read, created_at
         FROM public.tray_alert
         EXCEPT
         SELECT id, tray_number, egg_count, session_ref, is_read, created_at
         FROM _per_size_tray_alerts_before)
    ) THEN
        RAISE EXCEPTION 'Historical Tray Alert or Read/Unread preservation check failed';
    END IF;

    IF (SELECT count(*) FROM public.tray_size_progress) <> 5
       OR EXISTS (
           SELECT 1 FROM public.tray_size_progress
           WHERE opening_balance IS NOT NULL
              OR eggs_toward_next_milestone IS NOT NULL
              OR confirmed_good_eggs_since_opening <> 0
              OR completed_milestones <> 0
       )
       OR EXISTS (
           SELECT 1 FROM public.egg_record
           WHERE quality_verified IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'Per-size seed state or legacy verification value is unexpected';
    END IF;
END;
$$;

COMMIT;
