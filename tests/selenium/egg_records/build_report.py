"""Compile actual Selenium attempts and preservation evidence; no app import."""
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'testing/selenium-artifacts/egg-records-review-20261101'
runs=sorted((OUT/'runs').iterdir())
attempts=[r for run in runs if (run/'results.json').exists()
          for r in json.loads((run/'results.json').read_text(encoding='utf-8'))]
latest={r['test_case_id']:r for r in sorted(attempts,key=lambda r:r['executed_at'])}
assert set(latest)=={f'ER-{n:02}' for n in range(1,43)},'Wait for all42 case results'
rows=[latest[k] for k in sorted(latest)]
counts=Counter(r['status'] for r in rows)
(OUT/'all-attempts.json').write_text(json.dumps(attempts,indent=2),encoding='utf-8')
(OUT/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
baseline=json.loads((OUT/'before-hashes.json').read_text(encoding='utf-8-sig'))
changed=[r['path'] for r in baseline if not (ROOT/r['path']).is_file()
         or hashlib.sha256((ROOT/r['path']).read_bytes()).hexdigest().upper()!=r['hash']]
missing=[path for r in attempts for path in r['evidence'] if not Path(path).is_file()]
preservation={'baseline_files_checked':len(baseline),'changed_existing_files':changed,
    'all_existing_baseline_contents_preserved':not changed,'missing_execution_evidence':missing,
    'new_isolated_test_files':[p.relative_to(ROOT).as_posix() for p in Path(__file__).parent.glob('*.py')]}
(OUT/'preservation-check.json').write_text(json.dumps(preservation,indent=2),encoding='utf-8')
assert not changed,changed
assert not missing,missing
environments=[]
for run in runs:
    file=run/'environment.json'
    if file.exists():
        env=json.loads(file.read_text(encoding='utf-8'))
        assert '--headless=new' in env['arguments']
        assert 'HeadlessChrome' in env['user_agent']
        environments.append(env)
assert environments
env=environments[-1]
versions=json.loads((OUT/'package-versions.json').read_text())
lines=['# EGGSor+ Egg Records — Chapter IV functional test execution', '',
    'Execution date: November 1, 2026 (Asia/Manila). Read-only audit followed by actual headless Selenium execution.', '',
    f"**42 unique cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['BLOCKED']} BLOCKED, {counts['SKIPPED']} SKIPPED.**", '',
    f"These are the latest executed outcomes across {len(attempts)} case attempts, including preliminary smoke checks. All attempts remain in all-attempts.json; duplicate smoke cases are not counted twice. Unsupported features are excluded from inventory, not labeled skipped.", '',
    f"Latest-case completion range: {min(r['executed_at'] for r in rows)} through {max(r['executed_at'] for r in rows)}.", '',
    '## Audited functionality', '',
    'The pre-execution audit is in AUDIT.md. The real /egg-records and /api/egg-records routes, login_required permissions, EggRecord queries/serialization, Jinja template, shared browser scripts and inline Egg Records JavaScript were exercised. Features include six-column records, three verification states, stored classifications, unavailable weights, ID/session/weight search, size/quality/date filters, reset, one-second live polling, CSV export and a print action.', '',
    'Defaults: today in Asia/Manila, descending record ID, maximum500 returned rows. No interactive pagination, column sorting, verification filter, editing/deletion, detail dialog or server-side export exists. API after_id/limit checks are API-contract tests, not claims of UI pagination. Imported/historical sizes are displayed from storage without reclassification. Staff and admin can read records; there is no row-owner restriction. The ADMIN breadcrumb is static even for staff.', '',
    '## Environment and isolation', '',
    f"Chrome {env['capabilities']['browserVersion']}, ChromeDriver {env['capabilities']['chrome']['chromedriverVersion'].split(' ')[0]}, Selenium {versions['selenium']}, pytest {versions['pytest']}, Flask {versions['Flask']}, SQLAlchemy {versions['SQLAlchemy']}, Python {env['python'].split(' ')[0]}, Windows. Chrome launched with --headless=new and a disposable profile; no visible browser windows. Browser timezone was explicitly Asia/Manila; normal viewport1440x1000, responsive case390x844. environment.json records launch arguments, capabilities and HeadlessChrome user agent.", '',
    'Every case used a fresh loopback-only Flask subprocess, independently signed sessions, random disposable admin/staff credentials and a private temporary SQLite file. Existing auth_test_support suppresses dotenv and production engine initialization and replaces camera/ESP32/inference modules before loading the real app. Hardware and auto-start are disabled. Five relevant model tables are created with foreign keys enabled and existing SQLite-compatible constraints; no production schema migration occurs. Outbound Python socket connect/connect_ex calls are blocked during serving. Browser public CDN assets remain available for actual styling and alerts.', '',
    'No webcam, COM8, ESP32, servo, load cell, firmware upload or production Supabase connection/write was used. Physical sorting/model accuracy and PostgreSQL/RLS were outside scope. Login and Dashboard were visited only as authentication/navigation setup; their suites were not rerun. Printer invocation was intercepted to keep all execution headless; no physical print or native print dialog occurred.', '',
    'Fixture coverage:15 baseline records,13 in today, one immediately before Manila midnight and one at next midnight. Includes all six stored size labels, Good/Crack/Rotten/Not Classified, true/false/null verification, zero-weight sentinel, quotes/comma and HTML-like session text. Separate cases replace only disposable records with zero or505 rows. New-row polling uses controlled DB insertion, not simulated physical sensor events.', '',
    '## Test execution report', '',
    '| Test Case ID | Test Scenario | Test Procedure | Test Data | Expected Result | Actual Result | Status |',
    '|---|---|---|---|---|---|---|']
def cell(value):return str(value).replace('|','\\|').replace('\n','<br>')
for r in rows:
    actual=r['actual_result']
    if r['test_case_id']=='ER-40' and r['status']=='FAIL':
        actual='Confirmed: rendered banner says Medium50–56g. Shared classifier(59) and displayed EGG-000002 both say Medium at59g; consistency assertion failed (59<=56 is false). Screenshot/browser log/observed UI/server log retained.'
    lines.append('| '+' | '.join(cell(v) for v in [r['test_case_id'],r['scenario'],r['test_procedure'],r['test_data'],r['expected_result'],actual,r['status']])+' |')
lines += ['', '## Confirmed defect and investigation', '',
    '**ER-40 — size-standards banner disagrees with implemented classification.** The browser-visible informational text describes Medium50..56g and Large57..63g. The current shared classifier maps weights>55 through60 to Medium, so59g is Medium; its exact result is passed from the isolated server bootstrap and the same value is stored/rendered in ID2. The test does not assume a new industry standard or recompute historical records. Read-only inspection of egg_standards.py and templates/egg_records.html corroborates the contradiction. The preliminary smoke check and full execution preserve separate attempts/evidence. This is an application content inconsistency, not a SQLite fixture or Selenium timing problem.', '',
    'Recommendation for review: align the explanatory banner with the existing authoritative classifier after confirming the desired wording; maintain display of historical stored sizes. No fix was implemented.', '',
    '**Fixture investigations:** The initial full run recorded CSV watcher timeouts (ER-33/34) because headless Chrome overwrote the same date-based filename, plus an ER-41 visibility assertion during sidebar animation and a minimum desktop window-width mismatch. Only isolated tests were corrected: unique per-download directories, exact CDP mobile metrics, and an observed-bound/visibility wait. The CSV cases passed on targeted rerun. ER-26 was strengthened and rerun with a nondefault quality choice. INVESTIGATION.md explains each correction; all original screenshots/logs/results are retained.', '',
    'Other observed behavior is documented without inventing failures: CSV missing weight is literal null while table weight is N/A; quality filters use stored quality rather than verification; staff can read the page despite a static ADMIN breadcrumb; only loaded500 rows are exported. These are existing representations/limitations, not failed invented feature requirements. The print test verifies delegation only, not print pagination/layout. Filter changes during a pending request can temporarily retain the old response; ER-39 checks implemented eventual recovery on the next poll, not an absent stale-response guard.', '',
    'The only remaining failed unique case is ER-40. The CSV and mobile fixture failures passed on rerun. No application fixes were implemented.', '',
    '## Exact execution commands', '',
    'Repository-root PowerShell commands (stdout/stderr saved with Tee-Object to collection.txt, smoke.txt and full-run.txt):', '',
    '```powershell',
    '$env:EGGSORT_CHROMEDRIVER="$env:USERPROFILE\\.cache\\selenium\\chromedriver\\win64\\156.0.8078.80\\chromedriver.exe"',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py --confcutdir=tests/selenium/egg_records --collect-only -q -p no:cacheprovider',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py -k \'test_01 or test_03 or test_40\' --confcutdir=tests/selenium/egg_records -q -p no:cacheprovider --tb=short',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py --confcutdir=tests/selenium/egg_records -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/egg-records-review-20261101/full-run.xml',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py -k test_26 --confcutdir=tests/selenium/egg_records -q -p no:cacheprovider --tb=short',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py -k \'test_32 or test_33 or test_34\' --confcutdir=tests/selenium/egg_records -q -p no:cacheprovider --tb=short',
    '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/egg_records/test_egg_records.py -k test_41 --confcutdir=tests/selenium/egg_records -q -p no:cacheprovider --tb=short',
    '.\\.venv\\Scripts\\python.exe -B tests/selenium/egg_records/build_report.py',
    '```', '',
    'The Chrome fixture hardcodes --headless=new; no environment flag can accidentally select headed mode. The existing Python executable required approved access outside the filesystem sandbox; all application data remained isolated. No dependency installation, Git mutation or production-code change was needed.', '',
    '## Evidence locations', '',
    '- AUDIT.md: actual feature/query/permission audit, recorded before execution.',
    '- Each runs/<timestamp>/inventory.json: collected test inventory before fixtures executed.',
    '- Each runs/<timestamp>/results.json: actual outcomes, full assertions, completion timestamps and evidence paths.',
    '- Per case: ER-xx-browser.json, ER-xx-server.log, ER-xx-observed.json; ER-xx-failure.png when failed. Downloaded CSVs are preserved under the applicable run downloads/ directory.',
    '- environment.json in executed run directories: actual headless Chrome launch and timezone evidence.',
    '- results.json: latest42-case machine-readable report. all-attempts.json: all smoke/full/rerun attempts without overwriting failures.',
    '- full-run.xml and console .txt files: pytest execution evidence.',
    '- before-hashes.json, before-git-status.txt, after-git-status.txt and preservation-check.json: existing contents and uncommitted changes preserved.', '',
    'Run summaries:', '']
for run in runs:
    f=run/'results.json'
    if f.exists():
        entries=json.loads(f.read_text(encoding='utf-8'));c=Counter(r['status'] for r in entries)
        lines.append(f"- runs/{run.name}/: {len(entries)} case outcomes; {dict(c)}" + (' (collection only)' if not entries else ''))
lines += ['',
    'Expected console noise: denied unrelated Dashboard/alert-badge/favicon requests under the lab allowlist, Tailwind development-CDN warning and deliberate503/502/400/401 injections. Logs are interpreted with their test procedure; console entries are not automatically test failures.', '',
    '## Preservation and limits', '',
    f'All{len(baseline)} existing baseline source/test/document files retain their exact SHA256 contents. Only new isolated files under tests/selenium/egg_records/ and generated evidence under this report directory were created. Existing uncommitted changes were preserved. No cleanup, branch switch, commit or push occurred.', '',
    'SQLite behavior is not evidence for production PostgreSQL/RLS, real deployment latency or concurrent users. Date formatting uses the browser locale/timezone; this run selected Asia/Manila. Polling tests use bounded Selenium waits, not a sustained load/soak test. Automatic Manila day rollover over an actual midnight is not elapsed-time tested. No physical hardware or manual test outcomes are claimed.', '',
    'Stopped after testing; no application or UI fixes implemented.', '']
content='\n'.join(lines)
for old,new in {
    'maximum500':'maximum 500','viewport1440x1000':'viewport 1440x1000',
    'case390x844':'case 390x844','Fixture coverage:15':'Fixture coverage: 15',
    'records,13':'records, 13','zero or505':'zero or 505','loaded500':'loaded 500',
    'Medium50':'Medium 50','Large57':'Large 57','through60':'through 60',
    'so59g':'so 59g','at59g':'at 59g',f'All{len(baseline)}':'All '+str(len(baseline)),
}.items():content=content.replace(old,new)
(OUT/'REPORT.md').write_text(content,encoding='utf-8')
print(json.dumps({'unique_cases':len(rows),'attempts':len(attempts),'counts':dict(counts),
                 'unchanged_baseline_files':len(baseline),'report':str(OUT/'REPORT.md')}))
