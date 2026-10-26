-- REVIEW ONLY: one-time initialization for the five empty physical trays.
-- Do not run until sorting and database writes are paused and this file is
-- reviewed against the live schema. This transaction never changes egg_record.

BEGIN;

-- Serialize against application progress/ledger/alert/audit writes while the
-- preconditions are checked and the five rows plus audit entry are updated.
LOCK TABLE public.tray_size_progress,
           public.tray_size_progress_egg,
           public.tray_size_alert,
           public.audit_log
    IN ACCESS EXCLUSIVE MODE;

DO $initialize_empty_tray_progress$
DECLARE
    changed_rows INTEGER;
BEGIN
    IF (SELECT count(*) FROM public.tray_size_progress) <> 5
       OR EXISTS (
           SELECT 1
           FROM (VALUES
               ('Small'), ('Medium'), ('Large'), ('Extra Large'), ('Jumbo')
           ) AS expected(egg_size)
           LEFT JOIN public.tray_size_progress AS progress USING (egg_size)
           WHERE progress.egg_size IS NULL
       )
       OR EXISTS (
           SELECT 1 FROM public.tray_size_progress
           WHERE egg_size NOT IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')
       ) THEN
        RAISE EXCEPTION 'Expected exactly one progress row for each of the five supported sizes; no changes applied.';
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.tray_size_progress
        WHERE opening_balance IS NOT NULL
           OR opening_balance_set_at IS NOT NULL
           OR opening_balance_set_by_user_id IS NOT NULL
           OR eggs_toward_next_milestone IS NOT NULL
           OR confirmed_good_eggs_since_opening <> 0
           OR completed_milestones <> 0
    ) THEN
        RAISE EXCEPTION 'Tray progress is already initialized or contains counts; no values were overwritten.';
    END IF;

    IF EXISTS (SELECT 1 FROM public.tray_size_progress_egg)
       OR EXISTS (SELECT 1 FROM public.tray_size_alert)
       OR EXISTS (
           SELECT 1 FROM public.audit_log
           WHERE event_key = 'tray-empty-initialization-v1'
       ) THEN
        RAISE EXCEPTION 'Tray ledger, size alerts, or initialization audit already exists; investigate before retrying.';
    END IF;

    UPDATE public.tray_size_progress
       SET opening_balance = 0,
           opening_balance_set_at = now(),
           opening_balance_set_by_user_id = NULL,
           eggs_toward_next_milestone = 0,
           updated_at = now()
     WHERE egg_size IN ('Small', 'Medium', 'Large', 'Extra Large', 'Jumbo')
       AND opening_balance IS NULL
       AND confirmed_good_eggs_since_opening = 0
       AND completed_milestones = 0;

    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 5 THEN
        RAISE EXCEPTION 'Expected to initialize five tray rows, updated %; transaction rolled back.', changed_rows;
    END IF;

    INSERT INTO public.audit_log (event_type, actor, description, event_key)
    VALUES (
        'tray_balances_auto_initialized',
        'System',
        'Initialized all five empty tray opening balances to 0. Historical Egg Records were not included.',
        'tray-empty-initialization-v1'
    );
END
$initialize_empty_tray_progress$;

COMMIT;
