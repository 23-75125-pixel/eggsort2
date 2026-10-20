-- READ ONLY. Run this first in the Supabase SQL Editor and review every row.
-- It does not create, update, delete, or alter anything.
WITH proposed AS (
    SELECT
        s.session_ref AS original_session_ref,
        s.status,
        s.started_at,
        CASE
            -- Capture group 1 is the complete desired reference. The suffix
            -- class places '-' first, so it cannot consume the timestamp.
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
)
SELECT original_session_ref, proposed_session_ref, status, started_at
FROM proposed
ORDER BY started_at NULLS LAST, original_session_ref;

-- A nonzero result means stop: a manual review is required before migration.
WITH proposed AS (
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
)
SELECT proposed_session_ref, count(*) AS conflicting_sessions
FROM proposed
WHERE proposed_session_ref IS NOT NULL
GROUP BY proposed_session_ref
HAVING count(*) > 1;
