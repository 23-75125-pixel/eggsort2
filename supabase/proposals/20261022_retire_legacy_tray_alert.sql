-- PROPOSAL ONLY. Do not run until this change is approved and the live schema
-- has been reviewed. Drops only the obsolete global tray_alert table.
-- RESTRICT (the default) deliberately makes dependencies stop the operation.

BEGIN;
SET LOCAL lock_timeout = '5s';

DO $retire_legacy_tray_alert$
BEGIN
    IF to_regclass('public.tray_size_progress') IS NULL
       OR to_regclass('public.tray_size_progress_egg') IS NULL
       OR to_regclass('public.tray_size_alert') IS NULL THEN
        RAISE EXCEPTION 'Per-size tray tables are missing; legacy retirement is unsafe.';
    END IF;

    IF to_regclass('public.tray_alert') IS NULL THEN
        RAISE NOTICE 'public.tray_alert is already absent; nothing to drop.';
    ELSE
        IF (SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'tray_alert') <> 6
           OR (SELECT count(DISTINCT column_name) FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'tray_alert'
                 AND column_name IN
                     ('id', 'tray_number', 'egg_count', 'session_ref', 'is_read', 'created_at')) <> 6 THEN
            RAISE EXCEPTION 'public.tray_alert does not match the expected legacy table shape.';
        END IF;

        DROP TABLE public.tray_alert RESTRICT;
    END IF;
END
$retire_legacy_tray_alert$;

COMMIT;
