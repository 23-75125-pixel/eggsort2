# Recent EGGSor+ Merged Changes

> **Current repository note (6 November 2026):** This file is a historical summary; its branch/commit entries below describe the earlier review and are not the current push state. The current local branch is `fix/dashboard-module` at `1ad2a4165bff3c5a328f025df38e330fb0c46b95`, matching the available local `origin/fix/dashboard-module` ref. The working tree contains uncommitted changes, so this does not establish that the current work is pushed to GitHub.

**Reviewed:** 20 October 2026  
**Branch:** `feature/database-relationships`  
**Latest pushed commit:** `00edd77` — `Update EggSort features`  
**Integrated merge commit:** `39804b3` — `Merge branch 'main' into feature/database-relationships`

This document summarizes the code changes in the latest push and the `main` branch changes that were integrated immediately before it. It describes where each change is used. A commit in GitHub does not by itself mean that firmware was uploaded or Supabase schema SQL was applied.

## Latest pushed changes (`00edd77`)

| Files | What changed | Where it is used |
|---|---|---|
| `config.py`, `.env.example`, `HARDWARE_SETUP.md` | Automatic camera/hardware startup after login now defaults to off (`AUTO_START_SORTING_ON_LOGIN=0`). | Operators start the camera and ESP32 manually from **Sorting Sessions** when ready. This prevents visiting the app or logging in from starting the machine automatically. A value in the active shell environment or `.env` can override the example/default. |
| `esp32/eggsort_controller/eggsort_controller.ino` | Reworked load-cell and size-servo processing into non-blocking cycle and route queues. Each measured egg has a cycle ID and route timer; route jobs have a capacity of 32. The load-cell cycle can be re-armed after its gate releases the egg without waiting for the earlier egg’s size-servo travel/closure to finish. Load-cell gate open position changed from 190 to 175. | ESP32 automatic weighing and sorting. This requires compiling and uploading the firmware before the physical machine uses these changes. |
| `esp32/eggsort_controller/stable_weight.h`, `stable_weight_tests.cpp` | Stable-weight hold changed from 1.5 seconds to 1 second. The compile-time checks were adjusted for the shorter hold, unstable readings, missing samples, egg removal, and slow drift. | Weight confirmation on the ESP32. The tests are code-level checks; this document did not run them or verify hardware weighing. |
| `esp32_bridge.py`, `test_esp32_bridge.py`, `templates/sorting_session.html` | Sensor text now reports a 1-second stable-weight period. Sorting Sessions now asks for confirmation before restarting or stopping, and guards status polling while a start/stop request is in progress. | Serial status display and the Sorting Sessions controls. The label “1 second” reflects the stability hold; see the timing note below for the separate gate-open delay. |
| `app.py`, `templates/dashboard.html` | Dashboard supports From/To date filtering, a last-seven-days preset, Manila-local date defaults, and fixed Sunday-to-Saturday weekday columns. Summary cards and trend data use the selected date range. Dashboard audit display excludes individual `egg_sorted` and `egg_rejected` events. | The Operational Dashboard: date-based totals, size distribution, quality rate, non-good count, and daily trend. Audit display focuses on system/operator activity rather than repeating each egg record. |
| `app.py` | Removed startup backfill that generated one audit log for every historical egg record. | Prevents older egg rows from filling the dashboard’s system audit log with repetitive “egg sorted” entries. New record-level audit events are still written by the normal record-save path; the dashboard filters them from its audit display. |
| `templates/egg_records.html` | Egg Records defaults to the current Manila date, converts date filters to Manila day boundaries, and resets filters back to today. CSV filenames use the Manila date. | The Egg Records page shows the current day by default while allowing users to select older dates. |

### Timing detail to be aware of

The firmware’s stable-weight hold is **1000 ms**. The current `LOADCELL_OPEN_DELAY_AFTER_WEIGHT` is **200 ms** in `eggsort_controller.ino`; a nearby serial status message still says “GATE OPENS IN 1 SECOND,” so the printed message is stale and does not describe the configured delay. The intentional 200 ms delay is the configured behavior; correct the status text when firmware messaging is next updated.

## Changes integrated from `main` (`39804b3`)

| Files | What changed | Where it is used |
|---|---|---|
| `app.py`, `models.py` | Added password-reset request throttling and a `PasswordResetAttempt` record. Limits are applied by email identifier and client IP, with configurable window, maximums, and cooldown. | The **Forgot Password** flow, to limit repeated reset-email requests. |
| `supabase/schema.sql`, `supabase/reset_schema.sql`, `verify_supabase.py` | Added the `password_reset_attempt` table, indexes, and deny-by-default RLS/revokes to the schema scripts and Supabase verifier. | These files describe and verify the database structure. Applying the SQL to the actual Supabase project is a separate deployment step. `reset_schema.sql` is a reset script and should not be run against data that needs to be preserved. |
| `app.py` | Camera feed/status and hardware status endpoints return an unavailable response when local hardware is disabled. | The hosted/dashboard-only deployment, where the camera and ESP32 are not attached. |
| `test_auth_flow.py`, `test_security_js.js`, `test_app_smoke.py` | Added or expanded source tests for password-reset throttling, authentication/security behavior, and app smoke behavior. | Developer test suite. Tests were not executed for this document. |
| `templates/sales.html` | Removed the standalone Sales page. | Sales was not connected to a working backend endpoint in this branch; it is no longer presented as an implemented module. |

## Practical summary

- **To use the changed sorting behavior on the machine:** upload the current ESP32 sketch; pushing the code to GitHub does not upload it to the board.
- **To use the new password-reset table on Supabase:** apply the project’s reviewed schema migration as a separate database operation; the Git merge alone does not create that table remotely.
- **For normal startup:** login should leave the camera/ESP32 stopped until an operator starts them from Sorting Sessions, unless the active environment explicitly sets auto-start to `1`.
- **Verification status:** this document is based on commit diffs and source inspection. It does not claim that the UI, database migration, firmware build, or physical sorting has passed testing.
