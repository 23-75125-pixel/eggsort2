# Selenium functional execution - Chapter 4 evidence

Execution date: **October 30, 2026 (Asia/Manila)**. Recorded case-completion timestamps: 2026-10-30T00:21:11.900923+08:00 through 2026-10-30T00:23:11.479465+08:00.

**Actual result: 22 PASS, 0 FAIL, 0 BLOCKED; duration 129.74 seconds.**

Headed local Google Chrome, Selenium 4.52.0 and pytest 9.2.0 on Windows; explicit temporary browser profile. Login cases executed before Dashboard cases.

Each case used a fresh loopback server, random disposable credentials and in-memory SQLite. Actual application routes, templates, JavaScript and Dashboard queries executed. Camera, inference and ESP32 were stand-ins; sorting endpoints and server outbound connections were blocked. Production .env and Supabase were not accessed.

| Case | Scenario | Actual result |
|---|---|---|
| LG-01 | Valid Login | PASS |
| LG-02 | Incorrect Password | PASS |
| LG-03 | Empty Fields | PASS |
| LG-04 | Logout | PASS |
| LG-05 | Protected Pages | PASS |
| LG-06 | Keep Me Signed In | PASS |
| LG-07 | Forgot Password | PASS |
| LG-08 | Disabled Account | PASS |
| LG-09 | Login via Email | PASS |
| LG-10 | Login Throttling | PASS |
| LG-11 | Reset Link Reuse | PASS |
| DB-01 | Dashboard Access | PASS |
| DB-02 | Default Statistics | PASS |
| DB-03 | Daily Trend and Filter | PASS |
| DB-04 | Empty Range | PASS |
| DB-05 | Rejected Only | PASS |
| DB-06 | Cumulative Trays | PASS |
| DB-07 | Date Validation | PASS |
| DB-08 | Rapid Date Changes | PASS |
| DB-09 | Failure and Recovery | PASS |
| DB-10 | Staff/Admin Roles | PASS |
| DB-11 | Clear Logs Safety | PASS |

## Evidence and interpretation

Private source evidence remains locally in `testing/selenium-artifacts/20261030-002104/`: `results.json`, `summary.md`, 22 browser logs and 22 bounded server logs. Generated artifacts and earlier diagnostic failure screenshots are excluded from this commit.

This summary describes the successful headed run only. A preceding headless run recorded 21 passes and one DB-09 reload timeout. The test harness was corrected to drain server error output, followed by the complete successful headed rerun. No application fix was introduced for that diagnostic failure.

## Limitations

- SQLite schema copies omit CHECK constraints; PostgreSQL constraints, RLS, production performance and physical hardware are not validated.
- Chrome still loads external frontend CDN assets. Expected HTTP errors under test, favicon denials and CDN warnings may appear in browser logs.
- Keep Me Signed In checks cookie expiry metadata, not 30-day elapsed persistence. Recovery uses local delivery, not SMTP; real Google OAuth is excluded.
- HTTP 400/401/403/409/429/503 assertions use fetch through real WebDriver. Date values are set with WebDriver JavaScript and change events rather than locale-specific calendar widgets.
- Automated results do not constitute unrecorded manual test outcomes. No screenshots of failures were needed in this final all-pass run.
