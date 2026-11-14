"""Compile the isolated Reports timezone-fix execution evidence."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "testing/selenium-artifacts/reports-date-fix-20261103"
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
    counts = Counter(row["status"] for row in rows)
    summaries.append((run.name, dict(counts)))
    for row in rows:
        attempt = {**row, "run": run.name}
        attempts.append(attempt)
        latest[row["test_case_id"]] = attempt

ordered = [latest[key] for key in sorted(latest)]
counts = Counter(row["status"] for row in ordered)
(OUT / "results.json").write_text(json.dumps(ordered, indent=2), encoding="utf-8")
(OUT / "all-attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")

lines = [
    "# EGGSor+ Reports date filtering and grouping — implementation and verification",
    "",
    "Execution date: November 3, 2026 (Asia/Manila). Stopped after verification for review.",
    "",
    f"**{len(ordered)} unique cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['BLOCKED']} BLOCKED, {counts['SKIPPED']} SKIPPED.**",
    "",
    f"RP-15 targeted run passed. The final complete suite passed all {len(ordered)} collected cases, including two added local ISO-week/month boundary cases. All {len(attempts)} case attempts remain in `all-attempts.json`; original failures are retained rather than overwritten.",
    "",
    "## Implementation",
    "",
    "The Reports API now treats its date inputs as Asia/Manila calendar dates. It converts the selected start date's local midnight to UTC and queries `sorted_at >= start`; the selected end date is advanced by one local day, converted to UTC, and queried with `sorted_at < end_before`. This half-open range includes all stored instants on each selected local date without relying on a last-microsecond boundary.",
    "",
    "Daily keys use the Manila-local date. Weekly keys use the ISO year/week of the Manila-local timestamp, preserving ISO week semantics. Monthly keys use the Manila-local year/month. KPI definitions, quality/verification arithmetic, record storage, and other modules are unchanged by this task.",
    "",
    "The initial targeted attempt revealed an edit-placement mistake before feature verification; Egg Records date parsing was restored to its pre-edit content and the Reports-specific parser was corrected. The first correct run then exposed the fixture's UTC-previous-day ID 16 as a Manila October 15 record. Test expectations for RP-10, RP-13, RP-14, RP-15 and RP-26 were updated to the independently converted fixture timestamps. Those are test expectation corrections, not product regressions.",
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
    "## Exact commands and evidence",
    "",
    "Executed from the repository root in PowerShell. The project Python executable required approved access after the sandbox denied execution; no dependencies were installed. The Selenium fixture enforces `--headless=new`, verifies actual Chrome arguments and HeadlessChrome user agent, and aborts rather than falling back to a visible browser.",
    "",
    "```powershell",
    "$env:EGGSORT_CHROMEDRIVER=\"$env:USERPROFILE\\.cache\\selenium\\chromedriver\\win64\\156.0.8078.80\\chromedriver.exe\"",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/reports/test_reports.py -k test_15 --confcutdir=tests/selenium/reports -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/reports-date-fix-20261103/rp15.xml",
    ".\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/reports/test_reports.py --confcutdir=tests/selenium/reports -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/reports-date-fix-20261103/full-run-final.xml",
    ".\\.venv\\Scripts\\python.exe -B tests/selenium/reports/build_report.py",
    "git apply --check testing/selenium-artifacts/reports-date-fix-20261103/rollback.patch",
    "git diff --check -- app.py tests/selenium/reports/test_reports.py tests/selenium/reports/lab_server.py tests/selenium/reports/conftest.py tests/selenium/reports/build_report.py",
    "```",
    "",
    "The RP-15 result is in `rp15.xml` and `runs/20261103-150820-677055/`. The final full-suite result is in `full-run-final.xml` and `runs/20261103-151416-368331/`; pytest reported 33 passed in 177.25 seconds. The earlier initial implementation failure, first full run and screenshots/logs remain in their timestamped run directories and `all-attempts.json`.",
    "",
    "Each case has browser logs, observed UI JSON and bounded server logs. The final environment record contains actual `--headless=new` browser arguments and the HeadlessChrome user agent. Screenshots, if any, were saved without opening.",
    "",
    "## Changed files and rollback",
    "",
    "Application behavior changed only in `app.py` (Reports timezone conversion, half-open filtering, and local period keys). Isolated test changes are limited to `tests/selenium/reports/test_reports.py`, `lab_server.py`, `conftest.py` and `build_report.py`; they update timestamp fixtures/expectations, the evidence output path, and report generation. No other app module, database or UI was changed.",
    "",
    "Verified pre-edit copies and their SHA-256 manifest are under `backups/pre-edit/`; `backup-verification.json` records that all backup files still match their manifest hashes. `rollback.patch` selectively reverses the Reports implementation and related semantic fixture/test changes in `app.py`, `test_reports.py`, and `lab_server.py`. It was validated with `git apply --check` and was not applied.",
    "",
    "## Isolation and limitations",
    "",
    "Each Selenium case used a fresh loopback Flask process, random disposable accounts, temporary SQLite and mocked camera/ESP32/inference modules. Outbound server connections were blocked. Browser public CDN assets were allowed. The test environment did not connect to physical hardware or production Supabase.",
    "",
    "Tests verify Asia/Manila calendar dates, including local midnight, month-start and ISO Monday boundaries. SQLite tests do not establish production PostgreSQL query behavior, deployment timezone configuration, RLS, concurrent load or production data quality. No hardware, firmware, Egg Records or unrelated UI behavior was tested or changed.",
    "",
    "Earlier uncommitted work was preserved. No cleanup, branch change, commit, push, merge, production access or hardware operation occurred.",
    "",
    "### Per-run summaries",
    "",
]
for run, summary in summaries:
    lines.append(f"- `runs/{run}/`: {sum(summary.values())} outcomes; `{summary}`")

(OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote report: {len(ordered)} unique cases {dict(counts)}; {len(attempts)} attempts retained")
