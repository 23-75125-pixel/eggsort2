# EGGSor+ Chrome functional tests

From the repository root, using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/selenium/test_functional.py --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short
```

Chrome runs headed by default with an explicit temporary profile. To request
headed mode explicitly in your local Windows PowerShell terminal:

```powershell
$env:EGGSORT_SELENIUM_HEADLESS = '0'
.\.venv\Scripts\python.exe -B -m pytest tests/selenium/test_functional.py --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short
# When finished:
Remove-Item Env:EGGSORT_SELENIUM_HEADLESS
```

Requires the installed Selenium/pytest packages, Google Chrome and a compatible
ChromeDriver (Selenium Manager may resolve/download it). No pytest-wide discovery
is needed: the explicit path avoids unrelated production-importing test modules.

Each case starts a fresh loopback-only server subprocess and disposable memory
SQLite dataset through the existing auth harness and Dashboard lab. The actual
templates, browser JavaScript, auth/CSRF, routes and Dashboard ORM calculations
execute. No actual .env is loaded. Camera, inference and ESP32 are stand-ins;
sorting routes are denied and Python outbound connections are blocked inside
the server. SMTP is replaced by local delivery. Passwords and the control token
are random per subprocess and are not persisted in reports. No real Google or
SMTP integration is exercised. The lab control endpoint exists only in this
test subprocess and requires an ephemeral secret.

Set the flag to `1` only when headless execution is intended. A noninteractive
execution environment may launch headed Chrome without showing it on your
desktop; run the command in your own terminal when you need to watch it.

Chrome is reused across cases with cookies cleared; each server has its own
fresh accounts, limiter and records. Explicit waits handle forms, modal messages,
requests and rendered state. Normal application 750 ms polling is unchanged.
Servers terminate during fixture teardown; Chrome quits at session teardown.
Server stderr is continuously drained into a bounded buffer to avoid blocking
the HTTP server when deliberate failure cases produce tracebacks. The final
buffer is saved as per-case server logs. Login LG-01 through LG-11 always run
before Dashboard DB-01 through DB-11. No extra viewing delays are inserted.
Do not use Ctrl+C as the normal completion path; abrupt OS termination can
prevent pytest teardown. No production hardware process should be stopped.

## Results

Private generated artifacts go under `testing/selenium-artifacts/<Manila timestamp>/`,
which is ignored by Git. `results.json` has case ID, scenario, expected/actual
result, PASS/FAIL/BLOCKED, Manila execution timestamp and evidence paths.
`summary.md` is a compact table. Browser logs are saved per executed case;
failed cases also capture screenshots when Chrome is available. Setup failures
are BLOCKED, never PASS. An assertion failure is FAIL even when caused by a
test harness issue; fix the harness and retain separate rerun evidence.

Failures are not permission to change application code. Record reproduction
and review the cause first. Browser resource logs may include expected 400/401/
403/409/429/503 responses deliberately tested, blocked favicon requests, and
Tailwind CDN warnings. These need interpretation, not automatic pass/fail.

## Limitations

SQLite schema copies omit CHECK constraints; PostgreSQL constraints, RLS,
production latency and physical routing are not validated. External CDN assets
are still fetched by Chrome. Keep-me-signed-in verifies cookie persistence
metadata rather than waiting 30 days. Throttling, reset reuse and Clear Logs
HTTP codes are checked by same-origin fetch executed through real WebDriver.
Date input values are set and change events dispatched through WebDriver to
avoid locale-dependent native calendar widgets. Day-relative fixtures require
the run to stay within one Manila calendar day. This suite is functional
automation evidence and does not substitute for recorded manual PASS/FAIL.
