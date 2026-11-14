"""Compile the User Management Selenium inventory and preserved attempts."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "testing/selenium-artifacts/user-management-review-20261103"
RUNS = OUT / "runs"


def md(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


attempts = []
latest = {}
summaries = []
for run in sorted(path for path in RUNS.iterdir() if path.is_dir()):
    result_path = run / "results.json"
    if not result_path.exists():
        summaries.append((run.name, {}))
        continue
    rows = json.loads(result_path.read_text(encoding="utf-8"))
    status_counts = Counter(row["status"] for row in rows)
    summaries.append((run.name, dict(status_counts)))
    for row in rows:
        attempt = {**row, "run": run.name}
        attempts.append(attempt)
        latest[row["test_case_id"]] = attempt

ordered = [latest[key] for key in sorted(latest)]
counts = Counter(row["status"] for row in ordered)
percentage = (counts["PASS"] / len(ordered) * 100) if ordered else 0
(OUT / "results.json").write_text(json.dumps(ordered, indent=2), encoding="utf-8")
(OUT / "all-attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")

lines = [
    "# EGGSor+ User Management — Chapter IV headless Selenium functional testing",
    "",
    "Execution date: November 3, 2026 (Asia/Manila). Read-only implementation audit followed by isolated automated browser testing.",
    "",
    f"**{len(ordered)} unique executed cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['BLOCKED']} BLOCKED, {counts['SKIPPED']} SKIPPED. Pass percentage: {percentage:.2f}%.**",
    "",
    f"The final complete suite passed all {len(ordered)} collected cases. {len(attempts)} case attempts across inventory, smoke, focused rerun and complete-suite processes are preserved in `all-attempts.json`; the table reports the latest outcome per UM ID.",
    "",
    "## Audited functionality",
    "",
    "The pre-execution source audit is in `AUDIT.md`. The real administrator-only `/user-management` page and `/api/users` list/create/profile-edit/invite-regeneration/delete endpoints were exercised, along with `/accept-invite/<token>` for password setup. Active admin and staff routes use the shared login checks; user-management page and API actions additionally require admin role. Unsafe API actions require CSRF.",
    "",
    "The page lists operators as cards with display name, role, account/setup state, avatar fallback, and actions. API serialization also includes email, username, active/password/invitation/Google status and current-account state, while withholding password/token secrets. Creation invites staff with a generated password hash and expiring single-use token; role is fixed to staff. Staff profile editing changes name, username, email and accepted HTTPS avatar. Blank avatar retains the existing value. Admin profile editing is rejected. Delete protects the current account. Invite creation and deletion use confirmation; mail failure leaves the created invitation available for copying.",
    "",
    "Input policy is the existing backend contract: normalized lowercase email/username; username 3–30 allowed ASCII letters/digits/dot/underscore/hyphen; display name 2–120 characters; email pattern validation; and case-insensitive duplicate rejection. Invitation acceptance enforces matching passwords 8–128 characters and stores a hash.",
    "",
    "Not implemented in this page: search, filtering, pagination, role editing, and an active/inactive toggle. New invited rows are `is_active=true` but `password_set=false`; account activation occurs when the invited user completes password setup. The Audit History tab explicitly states that sign-in/account entries are not recorded there. These absent controls were documented, not treated as failed tests.",
    "",
    "## Test execution report",
    "",
    "| Test Case ID | Test Scenario | Test Procedure | Test Data | Expected Result | Actual Result | Status |",
    "|---|---|---|---|---|---|---|",
]
for row in ordered:
    lines.append("| " + " | ".join(md(row[key]) for key in (
        "test_case_id", "scenario", "test_procedure", "test_data", "expected_result", "actual_result", "status"
    )) + " |")

lines += [
    "",
    "## Commands and execution evidence",
    "",
    "Commands ran from the repository root in PowerShell. The installed virtual-environment Python required approved executable access after the sandbox initially denied it. No packages were installed. Chrome was configured with a disposable temporary profile and `--headless=new`; the fixture inspected `Browser.getBrowserCommandLine` and `navigator.userAgent` and stopped if headless could not be verified. ChromeDriver and Flask lab processes used hidden process creation flags.",
    "",
    "```powershell",
    "$env:EGGSORT_CHROMEDRIVER=\"$env:USERPROFILE\\.cache\\selenium\\chromedriver\\win64\\156.0.8078.80\\chromedriver.exe\"",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py --confcutdir=tests/selenium/user_management --collect-only -q -p no:cacheprovider",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_01 or test_03 or test_09 or test_28 or test_32 or test_33 or test_34 or test_35' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/smoke.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_01 or test_03 or test_09 or test_28 or test_32 or test_33 or test_34 or test_35' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/smoke-rerun.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_04 or test_05 or test_06 or test_09 or test_18 or test_24 or test_25 or test_28 or test_29 or test_30 or test_31' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/functional-smoke.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_09 or test_18 or test_30 or test_31' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/focused-rerun.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_30 or test_31' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/recovery-rerun.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py -k 'test_03 or test_07' --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/navigation-rerun.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/full-run.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/user_management/test_user_management.py --confcutdir=tests/selenium/user_management -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/user-management-review-20261103/full-run-final.xml",
    ".\\.venv\\Scripts\\python.exe -B tests/selenium/user_management/build_report.py",
    "```",
    "",
    "The final suite reported **35 passed in 136.82 seconds**. JUnit evidence is `full-run-final.xml`; final per-case browser logs, observed page state and sanitized server logs are in `runs/20261103-155515-498832/`. Actual headless command-line and user-agent evidence is in that run's `environment.json`. Failed attempts from earlier fixture iterations retain screenshots when a browser was available and are preserved in their timestamped run folders.",
    "",
    "## Fixture corrections and investigation",
    "",
    "The initial smoke cases exposed test-fixture issues: the normal sign-in success dialog remained open and intercepted clicks; the test parsed Werkzeug's scrypt variant string too narrowly; lab-control requests omitted their harness key after page refresh; some assertions read before the next SweetAlert appeared; the mail-warning expectation used text different from the actual message; and an edit wait retained stale DOM elements during refresh. These were corrected only in isolated test files. Focused reruns passed, and the final 35-case suite passed. No application defect was confirmed by these fixture failures.",
    "",
    "Some expected lab traffic is denied by the allowlist (shared alert badge/favicons) and produces browser console warnings; this is recorded as isolation noise and does not count as a failed case. The deliberate users-list 503 is a disposable error injection.",
    "",
    "## Confirmed defects and recommendations",
    "",
    "No confirmed application defect was found in the tested scope. Recommendations are to retain role boundaries, credential secrecy and invite-expiry safeguards, and to implement search, pagination, role editing or direct activation/deactivation only if product requirements call for them. No application/UI changes were made.",
    "",
    "## Isolation and limitations",
    "",
    "Every case used a fresh loopback Flask subprocess backed by a temporary SQLite file, synthetic admin/staff accounts, and real app routes/templates/JavaScript. The existing auth-only loader suppressed dotenv/production database startup and replaced camera, ESP32 and inference modules before app import. Python outbound `connect`/`connect_ex` calls were blocked. Invitation email delivery was replaced by a deterministic test-only `InvitationDeliveryError`; no SMTP connection was made. Browser public CDN assets were allowed. Evidence server logs redact temporary invite tokens and harness keys.",
    "",
    "No production Supabase, existing user accounts, physical hardware, webcam, COM8, ESP32, firmware or personal browser was touched. Google OAuth verification and delivery to a real email provider were not tested. SQLite tests do not establish production PostgreSQL/RLS or deployed migration state. Test accounts, invitation links and passwords existed only in disposable fixture state; reported artifacts redact lab session secrets and passwords.",
    "",
    "Only new isolated Selenium files under `tests/selenium/user_management/` and generated audit/evidence under this directory were added. Existing uncommitted work was preserved. No destructive Git operation, cleanup, branch change, commit, push or merge occurred. Stopped after testing for review.",
    "",
    "### Per-run outcomes",
    "",
]
for run, summary in summaries:
    lines.append(f"- `runs/{run}/`: {sum(summary.values())} recorded outcomes; `{summary}`")

(OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote report for {len(ordered)} unique cases: {dict(counts)}; {len(attempts)} attempts retained")
