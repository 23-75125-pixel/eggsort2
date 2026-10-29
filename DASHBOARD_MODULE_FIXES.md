# Dashboard fixes and supervised verification

## Safety and preserved behavior

Audit clearing is disabled in the admin UI and API. An authenticated admin
with valid CSRF receives HTTP 409; staff remain forbidden and anonymous callers
remain unauthenticated. No audit rows or capture deduplication keys are deleted.
The existing audit visibility filter and persistence code are unchanged.

The 750 ms polling interval, pending-request guard, database query strategy,
quality counts, size thresholds and cumulative tray calculations remain.
Camera, firmware, serial, load-cell, experimental files, production configuration
and database schema are untouched. No cleanup, commit or deployment is included.

## Date and presentation behavior

- Trends count actual Manila calendar dates, including distinct dates in
  different weeks. Normal ranges include zero days. Labels show full dates.
- Ranges longer than 366 days show only dates with records, with an explicit
  chart note. This bounds empty chart columns, not selected records or queries;
  the existing unbounded row-loading performance risk remains.
- Explicit dates must be canonical YYYY-MM-DD, within years 0002–9998. Reversed,
  invalid and unsupported dates return HTTP 400. The reserved extreme years
  prevent overflow in timezone and next-day boundary arithmetic.
- Date controls are created once. An old response is discarded if the current
  controls differ, and a queued request fetches the newest selection.
- An always-present responsive message distinguishes loading, initial failure,
  stale results and recovery. Last successful values remain during failures.
- Size N/A appears as Unmeasured/N/A. Other unsupported historical size labels
  appear in Other/Unknown; stored records/classification are not changed.
- Tray totals and progress are explicitly cumulative and not date-filtered.
- Staff and admin share the role-neutral Operations Portal heading.

## Automated checks

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_dashboard test_auth_flow test_login_regressions test_security -v
node --test test_dashboard_js.js test_notifications.js test_security_js.js
```

Dashboard route tests use fake queries and forbid database connections/commits.
The reused auth harness initializes only disposable in-memory auth tables and
substitutes hardware modules. JavaScript tests execute the real inline script
in a small DOM stand-in. Width scenarios verify warning behavior and markup,
not rendered browser screenshots. No production credentials or data are used.

## Remaining manual checks and decisions

Use a separately supervised, hardware-disabled disposable environment for
browser testing. Check 360px/desktop layouts, real date input interaction,
multi-week charts, all-rejected/empty data, staff/admin differences, and network
failure/recovery. Never test audit deletion against production: it is disabled.

A retention policy is still required before enabling Clear Logs. Polling rate,
SQL aggregation, consistent multi-query snapshots, and fetch timeout changes
are separate proposals requiring measurement/review. Real Supabase latency and
browser layout are not validated by mocked tests. Existing session and Login
security policy decisions remain unchanged.

## Disposable browser lab

```powershell
.\.venv\Scripts\python.exe -B dashboard_manual.py
```

Open http://127.0.0.1:5012/login. Test-only accounts are `dashadmin` and
`dashstaff`, both with password `DashboardTest123!`. These credentials exist
only in fresh in-memory SQLite. Admin controls at `/__dashboard_lab` select
normal, simulated failure or slow-response mode. Ctrl+C discards the dataset.
The lab uses the actual Dashboard routes, template and SQLAlchemy calculations;
camera/inference/ESP32 are stand-ins and sorting endpoints are blocked. The
standalone entry point blocks Python outbound connections. Do not deploy it.

Manila today is T, fixed at startup. The default T-6 through T dataset contains
10 eggs: 6 Good, 2 Crack, 2 Rotten; quality rate 60%. Visible size counts are
Small 1, Medium 2, Large 1, Extra Large 1, Jumbo 1, Unmeasured/N/A 4.
Daily counts are 2, 0, 0, 3, 0, 2, 3. Including T-7 adds one Good Small:
11 total, 7 Good, quality rate 63.6%. Cumulative trays remain 2 and the lab
controls should always report 11 capture markers. The disposable schema copy
omits CHECK constraints for SQLite compatibility; it does not validate actual
PostgreSQL constraints, RLS, production latency or physical operations.

Automated verification: 56 focused Python tests and 22 JavaScript checks passed.
Browser PASS/FAIL outcomes must be recorded separately after actual execution;
this document does not claim completion of the Dashboard manual scenarios.
