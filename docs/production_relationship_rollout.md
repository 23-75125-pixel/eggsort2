# EGGSor+ production relationship rollout — execution record

The Phase 1 production migration ran once and committed on 2026-10-15 against
Supabase project `gtsymkfcoslnobitdheu`. No updated backend was deployed or
activated. The read-only preflight found the same pre-migration
structure as the restored local backup: `user.id` is `INTEGER`,
`egg_record.session_ref` is non-null `VARCHAR(40)`. The final preflight backup
`eggsor_public_20261015_082327_final_preflight.dump` contains 5 users, 166
eggs, 409 audits, and 0 tray alerts. The same counts remained after the backup.
There were 16 distinct egg session references, no blank/placeholder references,
and no custom triggers. Before migration, `sorting_session` and
`audit_log.user_id` did not exist. Existing tables had RLS on,
not forced, and are owned by the backend's `postgres` database role.

The Render service was suspended per the operator, and no local Python/Flask
server or common Flask-port listener was found. The SQL required 409 audit rows.
The read-only postflight confirmed 5 users, 166 eggs, 409 audits, 0 alerts,
16 historical sessions, three validated foreign keys, zero orphans, 20 valid
indexes, and no unvalidated constraints. The migration's transaction compared
fingerprints of the original rows before and after its changes.

## Procedure used

1. Review the feature-branch backend and
   `supabase/migrations/20261015_phase1_production_relationships_proposed.sql`.
   The production configuration accepts only this Supabase project's pooler
   target. Startup verifies the three migrated foreign keys without schema or
   data writes. Sorting writes remain off until the explicit production switch
   is enabled. Camera, ESP32, detection, load-cell, and servo logic are unchanged
   by this production-preparation step.
2. Before the maintenance window, make and verify a fresh production backup.
   Confirm the project identifier in the configured URL's username **without
   printing the URL or password**. PostgreSQL itself reports `postgres` as the
   role and database for multiple Supabase projects; the SQL script cannot
   independently prove the project identifier. Run a fresh read-only preflight
   for table shape, counts, and 16 session references. If the row counts differ
   from the SQL script's reviewed baseline, stop and revise that guard.
3. Keep the laptop Flask server stopped and the Render `eggsort` service
   suspended throughout the migration window. Obtain explicit approval
   for the maintenance window. Apply the production SQL once using the verified
   project connection with `ON_ERROR_STOP=1`. Its single transaction locks the
   original tables, checks starting counts and types, adds/backfills the parent
   rows, validates all three foreign keys, compares original-row fingerprints,
   and commits only if every check passes. On any error, stop; inspect before
   another attempt. The script has no DROP, DELETE, TRUNCATE, or reset.
4. Run a read-only postflight: original counts 5/166/409/0, 16 historical
   sorting sessions, three validated FKs, zero orphan eggs, and unchanged
   historical audit ownership. Keep sorting and egg-record writes disabled.
   Deploying or enabling the updated backend is a separate decision after it
   can be tested safely; the physical prototype is not required for this
   database migration or its postflight verification.

The user explicitly approved this migration. Backend activation requires a
later, separate approval.
The old deployed backend must not save eggs after the new foreign key is enabled:
it does not create `sorting_session` parents, so inserts would fail. Keep egg
sorting paused from immediately before migration through backend activation.

`tray_alert` stayed unchanged. Historical egg owners and audit user IDs were
not guessed.
