-- REVIEW DRAFT: do not run until this corrected test is approved.
-- Run as a complete file in a dedicated psql session (-X -f).
-- Standalone negative statements assert 23503 for missing references,
-- and 23001 (restrict_violation) for ON DELETE RESTRICT.
-- All fixture changes roll back; explicit IDs do not advance sequences.
\set ON_ERROR_STOP on
\set ON_ERROR_ROLLBACK off
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $$
BEGIN
    IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet
       OR inet_server_port() IS DISTINCT FROM 55432
       OR current_database() IS DISTINCT FROM 'eggsor_restore_test' THEN
        RAISE EXCEPTION 'Wrong target: isolated local test database required';
    END IF;
END;
$$;
LOCK TABLE public."user", public.sorting_session, public.egg_record, public.audit_log
    IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE
    fixture_id CONSTANT INTEGER := -2147483648;
    missing_id CONSTANT INTEGER := -2147483647;
    fixture_ref TEXT := 'PHASE1-TEST-' || substr(md5(random()::TEXT), 1, 20);
    missing_ref TEXT := 'PHASE1-MISSING-' || substr(md5(random()::TEXT), 1, 20);
BEGIN
    -- Verify the expected FK columns, parents, validation, and deletion rules.
    IF (
        SELECT COUNT(*)
        FROM (VALUES
            ('fk_sorting_session_user', 'public.sorting_session', 'user_id', 'public."user"', 'id', 'n'),
            ('fk_audit_log_user', 'public.audit_log', 'user_id', 'public."user"', 'id', 'n'),
            ('fk_egg_record_sorting_session', 'public.egg_record', 'session_ref', 'public.sorting_session', 'session_ref', 'r')
        ) AS expected(name, child, child_column, parent, parent_column, delete_rule)
        JOIN pg_constraint c ON c.conname = expected.name
            AND c.conrelid = expected.child::regclass
            AND c.confrelid = expected.parent::regclass
        JOIN pg_attribute child_attr ON child_attr.attrelid = c.conrelid
            AND child_attr.attname = expected.child_column
        JOIN pg_attribute parent_attr ON parent_attr.attrelid = c.confrelid
            AND parent_attr.attname = expected.parent_column
        WHERE c.contype = 'f' AND c.convalidated AND NOT c.condeferrable
          AND c.confdeltype::TEXT = expected.delete_rule
          AND c.conkey = ARRAY[child_attr.attnum]::SMALLINT[]
          AND c.confkey = ARRAY[parent_attr.attnum]::SMALLINT[]
    ) <> 3 THEN
        RAISE EXCEPTION 'Expected foreign keys/deletion rules are absent';
    END IF;
    IF EXISTS (SELECT 1 FROM public."user" WHERE id IN (fixture_id, missing_id))
       OR EXISTS (SELECT 1 FROM public.egg_record WHERE id = fixture_id)
       OR EXISTS (SELECT 1 FROM public.audit_log WHERE id = fixture_id)
       OR EXISTS (SELECT 1 FROM public.sorting_session
                  WHERE session_ref IN (fixture_ref, missing_ref)) THEN
        RAISE EXCEPTION 'Fixture collision: stop without changing existing records';
    END IF;
    PERFORM set_config('eggsor.phase1_fixture_ref', fixture_ref, true);
    PERFORM set_config('eggsor.phase1_missing_ref', missing_ref, true);
    INSERT INTO public."user" (id, username, password, role, is_active, password_set)
    VALUES (fixture_id, fixture_ref, '!disabled-test-fixture!', 'staff', false, false);
    INSERT INTO public.sorting_session (session_ref, user_id, status)
    VALUES (fixture_ref, fixture_id, 'running');
    INSERT INTO public.egg_record (id, weight_grams, size, quality, confidence, session_ref)
    VALUES (fixture_id, 50, 'Medium', 'Good', 1.0, fixture_ref);
    INSERT INTO public.audit_log (id, event_type, actor, description, user_id)
    VALUES (fixture_id, 'phase1_test', 'Phase1Test', 'Rollback-only relationship test', fixture_id);
END;
$$;

-- The next error is expected ONLY if SQLSTATE is 23503.
SAVEPOINT negative_session_user;
\set ON_ERROR_STOP off
UPDATE public.sorting_session SET user_id = -2147483647
WHERE session_ref = current_setting('eggsor.phase1_fixture_ref');
\set captured_sqlstate :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO SAVEPOINT negative_session_user;
RELEASE SAVEPOINT negative_session_user;
SELECT :'captured_sqlstate' = '23503' AS expected_violation \gset
\if :expected_violation
    \echo PASS: sorting_session rejects a nonexistent user.
\else
    \echo FAIL: expected 23503; received :captured_sqlstate
    DO $$ BEGIN RAISE EXCEPTION 'session_user negative test failed'; END; $$;
\endif

-- The next error is expected ONLY if SQLSTATE is 23503.
SAVEPOINT negative_audit_user;
\set ON_ERROR_STOP off
UPDATE public.audit_log SET user_id = -2147483647 WHERE id = -2147483648;
\set captured_sqlstate :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO SAVEPOINT negative_audit_user;
RELEASE SAVEPOINT negative_audit_user;
SELECT :'captured_sqlstate' = '23503' AS expected_violation \gset
\if :expected_violation
    \echo PASS: audit_log rejects a nonexistent user.
\else
    \echo FAIL: expected 23503; received :captured_sqlstate
    DO $$ BEGIN RAISE EXCEPTION 'audit_user negative test failed'; END; $$;
\endif

-- The next error is expected ONLY if SQLSTATE is 23503.
SAVEPOINT negative_egg_session;
\set ON_ERROR_STOP off
UPDATE public.egg_record
SET session_ref = current_setting('eggsor.phase1_missing_ref')
WHERE id = -2147483648;
\set captured_sqlstate :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO SAVEPOINT negative_egg_session;
RELEASE SAVEPOINT negative_egg_session;
SELECT :'captured_sqlstate' = '23503' AS expected_violation \gset
\if :expected_violation
    \echo PASS: egg_record rejects a nonexistent session.
\else
    \echo FAIL: expected 23503; received :captured_sqlstate
    DO $$ BEGIN RAISE EXCEPTION 'egg_session negative test failed'; END; $$;
\endif

-- The next error is expected ONLY if SQLSTATE is 23001 (restrict_violation).
SAVEPOINT negative_session_delete;
\set ON_ERROR_STOP off
DELETE FROM public.sorting_session
WHERE session_ref = current_setting('eggsor.phase1_fixture_ref');
\set captured_sqlstate :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO SAVEPOINT negative_session_delete;
RELEASE SAVEPOINT negative_session_delete;
SELECT :'captured_sqlstate' = '23001' AS expected_violation \gset
\if :expected_violation
    \echo PASS: ON DELETE RESTRICT prevents deleting a session containing an egg.
\else
    \echo FAIL: expected 23001; received :captured_sqlstate
    DO $$ BEGIN RAISE EXCEPTION 'session_delete negative test failed'; END; $$;
\endif

-- Positive SET NULL test; delete ONLY the newly inserted fixture user.
-- This is standalone too, so its FK actions finish before assertions.
DELETE FROM public."user" WHERE id = -2147483648;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public."user" WHERE id = -2147483648)
       OR NOT EXISTS (SELECT 1 FROM public.sorting_session
                      WHERE session_ref = current_setting('eggsor.phase1_fixture_ref')
                        AND user_id IS NULL AND status = 'running')
       OR NOT EXISTS (SELECT 1 FROM public.audit_log
                      WHERE id = -2147483648 AND user_id IS NULL AND actor = 'Phase1Test')
       OR NOT EXISTS (SELECT 1 FROM public.egg_record
                      WHERE id = -2147483648
                        AND session_ref = current_setting('eggsor.phase1_fixture_ref')) THEN
        RAISE EXCEPTION 'User deletion failed to preserve session, audit, or egg';
    END IF;
    RAISE NOTICE 'PASS: user deletion clears both user links and preserves session, audit, and egg';
END;
$$;
ROLLBACK;
\echo Tests completed; all fixture changes rolled back. Run read-only postflight again.
