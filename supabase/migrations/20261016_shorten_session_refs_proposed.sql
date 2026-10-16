-- PROPOSED PRODUCTION MIGRATION: DO NOT RUN until separately approved.
-- Preconditions: Flask/Render writers are stopped and the read-only preview
-- has been reviewed with no duplicate proposed references.
BEGIN;

CREATE TEMP TABLE session_ref_baseline ON COMMIT DROP AS
SELECT
    (SELECT count(*) FROM public.sorting_session) AS sessions,
    (SELECT count(*) FROM public.egg_record) AS eggs,
    (SELECT count(*) FROM public.tray_alert) AS alerts;

CREATE TEMP TABLE session_ref_map ON COMMIT DROP AS
SELECT original_session_ref, proposed_session_ref
FROM (
    SELECT
        s.session_ref AS original_session_ref,
        CASE
            WHEN s.session_ref ~ '^SES-[0-9]{8}-[0-9]{6}-[-A-Za-z0-9_]+$'
                THEN regexp_replace(
                    s.session_ref,
                    '^(SES-[0-9]{8}-[0-9]{6})-[-A-Za-z0-9_]+$',
                    E'\\1'
                )
            WHEN s.started_at IS NOT NULL
                THEN 'SES-' || to_char(
                    s.started_at AT TIME ZONE 'Asia/Manila',
                    'YYYYMMDD-HH24MISS'
                )
            ELSE NULL
        END AS proposed_session_ref
    FROM public.sorting_session AS s
    WHERE s.status NOT IN ('historical', 'unknown')
      AND s.session_ref !~ '^SES-[0-9]{8}-[0-9]{6}(-[0-9]{2})?$'
) AS proposed
WHERE proposed_session_ref IS NOT NULL
  AND original_session_ref <> proposed_session_ref;

DO $$
BEGIN
    IF EXISTS (
        SELECT proposed_session_ref
        FROM session_ref_map
        GROUP BY proposed_session_ref
        HAVING count(*) > 1
    ) OR EXISTS (
        SELECT 1
        FROM session_ref_map AS m
        JOIN public.sorting_session AS s
          ON s.session_ref = m.proposed_session_ref
         AND s.session_ref <> m.original_session_ref
    ) THEN
        RAISE EXCEPTION
            'A proposed short session reference already exists; no changes were made.';
    END IF;
END $$;

-- The cascade lets each child egg retain its relationship during the parent
-- primary-key rename. The deletion behavior remains RESTRICT.
ALTER TABLE public.egg_record
    DROP CONSTRAINT fk_egg_record_sorting_session;

ALTER TABLE public.egg_record
    ADD CONSTRAINT fk_egg_record_sorting_session
    FOREIGN KEY (session_ref)
    REFERENCES public.sorting_session(session_ref)
    ON DELETE RESTRICT
    ON UPDATE CASCADE;

UPDATE public.sorting_session AS s
SET session_ref = m.proposed_session_ref
FROM session_ref_map AS m
WHERE s.session_ref = m.original_session_ref;

-- tray_alert has no foreign key, so preserve any matching references directly.
UPDATE public.tray_alert AS t
SET session_ref = m.proposed_session_ref
FROM session_ref_map AS m
WHERE t.session_ref = m.original_session_ref;

DO $$
DECLARE
    baseline session_ref_baseline%ROWTYPE;
BEGIN
    SELECT * INTO baseline FROM session_ref_baseline;
    IF (SELECT count(*) FROM public.sorting_session) <> baseline.sessions
       OR (SELECT count(*) FROM public.egg_record) <> baseline.eggs
       OR (SELECT count(*) FROM public.tray_alert) <> baseline.alerts
       OR EXISTS (
           SELECT 1
           FROM public.egg_record AS e
           LEFT JOIN public.sorting_session AS s ON s.session_ref = e.session_ref
           WHERE s.session_ref IS NULL
       ) THEN
        RAISE EXCEPTION 'Record-count or egg/session relationship validation failed.';
    END IF;
END $$;

SELECT original_session_ref, proposed_session_ref
FROM session_ref_map
ORDER BY original_session_ref;

COMMIT;
