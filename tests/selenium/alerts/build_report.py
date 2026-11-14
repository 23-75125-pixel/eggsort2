"""Combine actual Alerts evidence without opening artifacts or importing the app."""
import hashlib
import importlib.metadata
import json
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'testing/selenium-artifacts/alerts-review-20261102'
runs=sorted((OUT/'runs').iterdir())
attempts=[r for run in runs if (run/'results.json').is_file()
          for r in json.loads((run/'results.json').read_text(encoding='utf-8'))]
latest={r['test_case_id']:r for r in sorted(attempts,key=lambda r:r['executed_at'])}
assert set(latest)=={f'AL-{n:02}' for n in range(1,45)},'Wait for all44 case outcomes'
rows=[latest[k] for k in sorted(latest)];counts=Counter(r['status'] for r in rows)
percent=counts['PASS']/len(rows)*100
(OUT/'all-attempts.json').write_text(json.dumps(attempts,indent=2),encoding='utf-8')
(OUT/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
baseline=json.loads((OUT/'before-hashes.json').read_text(encoding='utf-8-sig'))
changed=[r['path'] for r in baseline if not (ROOT/r['path']).is_file()
         or hashlib.sha256((ROOT/r['path']).read_bytes()).hexdigest().upper()!=r['hash']]
missing=[p for r in attempts for p in r['evidence'] if not Path(p).is_file()]
environments=[]
for run in runs:
    if (run/'environment.json').is_file():
        env=json.loads((run/'environment.json').read_text())
        assert env['headless_verified'] and '--headless=new' in env['actual_browser_arguments']
        assert 'HeadlessChrome' in env['user_agent']
        environments.append(env)
assert not changed,changed
assert not missing,missing
assert environments
new_files=[p.relative_to(ROOT).as_posix() for p in Path(__file__).parent.glob('*.py')]
(OUT/'preservation-check.json').write_text(json.dumps({'baseline_files_checked':len(baseline),
    'changed_existing_files':changed,'all_baseline_contents_preserved':True,
    'missing_evidence':missing,'new_isolated_test_files':new_files,
    'webdriver_instances_verified_headless':len(environments)},indent=2),encoding='utf-8')
env=environments[-1]
versions={p:importlib.metadata.version(p) for p in ['selenium','pytest','Flask','SQLAlchemy']}
(OUT/'package-versions.json').write_text(json.dumps(versions,indent=2),encoding='utf-8')
findings={
 'AL-37':('Missing response fields are treated as empty success',
   'A synthetic HTTP200 {} response caused alertRows to default to[] and rendered the caught-up empty state despite four unread rows still stored. The page did not show refresh-error feedback. This violates retention/error handling for an unexpected success payload. Validate the alerts array and required response fields before replacing confirmed state; preserve prior rows and show Retry on invalid shape.'),
 'AL-38':('Null unread_count hides a nonzero badge',
   'A synthetic response containing the valid four unread rows and unread_count:null was coerced with Number(null) to0. Four unread cards remained while tab count became0 and sidebar badge hid; DB count stayed4. Reject null/blank/non-numeric counts before numeric conversion and keep the last confirmed count. Review both Alerts page and shared badge numeric guards.'),
 'AL-39':('Unexpected HTML200 acknowledgement reports success',
   'A synthetic HTML200 read-all response performed no DB update, but EggSortAlerts.request swallowed JSON parse failure into{} and accepted HTTP success. The UI displayed All alerts marked as read while four alerts remained unread. Validate the JSON acknowledgement contract, including ok, before reporting success; non-JSON/invalid bodies should use the existing error path. This involves the shared request helper and should receive regression coverage before any approved fix.'),
}
lines=['# EGGSor+ Alerts Module — Chapter IV functional test execution', '',
 'Execution date: November 2, 2026 (Asia/Manila). Read-only implementation audit followed by actual isolated headless Selenium tests.', '',
 f"**Total: {len(rows)} unique cases — {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['BLOCKED']} BLOCKED, {counts['SKIPPED']} SKIPPED. Pass percentage: {percent:.2f}%.**", '',
 f'{len(attempts)} total case attempts are retained in all-attempts.json. Latest outcomes per AL ID are summarized below; preliminary/rerun attempts are not counted twice. Deselected cases in targeted runs are not skipped tests.', '',
 f"Latest-case completion range: {min(r['executed_at'] for r in rows)} through {max(r['executed_at'] for r in rows)}.", '',
 '## Audited scope and active Alerts contract', '',
 'AUDIT.md was saved before execution. Actual app.py routes, templates/alerts.html, _notifications.html, static/notifications.js, static/alerts_badge.js, security handling, models and tray creation/save path were inspected. Existing Dashboard/phase2 mocks were inspected as context; unexecuted tests were not credited as passes.', '',
 'Per-size tray alerts are active in the restored code: the save path calls the real helper and current Alerts endpoints unconditionally query TraySizeAlert. Five tracked sizes are Small, Medium, Large, Extra Large and Jumbo. This establishes code behavior, not production migration state; no Supabase/migration access occurred. Experimental files and the proposed SQL migration were not treated as active features.', '',
 'Implemented UI: default Unread and Read tabs; immediate case-insensitive title/message search within selected loaded tab; mark-one; confirmed Mark All; durable global read state; empty states; failure/Retry panel; sidebar unread badge;1-second page polling and15-second badge polling elsewhere. IDs descend numerically, not by created_at. Retrieval caps at200, while unread_count and read-all cover all stored eligible alerts. There is no UI pagination, export, deletion, per-user ownership or notification delivery channel. Search is local to loaded selected-tab rows, not a server query.', '',
 '## Test environment and safety', '',
 f"Windows; Python {env['python'].split(' ')[0]}; Selenium {versions['selenium']}; pytest {versions['pytest']}; Flask {versions['Flask']}; SQLAlchemy {versions['SQLAlchemy']}; Chrome {env['capabilities']['browserVersion']}; ChromeDriver {env['capabilities']['chrome']['chromedriverVersion'].split(' ')[0]}; Asia/Manila browser timezone. Desktop1440x1000, mobile390x844 via CDP on the disposable headless session only.", '',
 f"Every one of {len(environments)} WebDriver instances was verified using actual Browser.getBrowserCommandLine arguments containing --headless=new AND the HeadlessChrome user agent. Options use temporary user-data-dir profiles and never attach to a personal browser. If headless verification fails, the fixture stops; no visible fallback exists. ChromeDriver and Flask subprocesses use Windows CREATE_NO_WINDOW ({env['driver_no_window_flag']}). No GUI/desktop mouse/keyboard automation or personal browser manipulation was used.", '',
 'Each case uses a fresh loopback Flask subprocess and private temporary SQLite file with random credentials. Existing auth-only loader suppresses dotenv/production database initialization, and mocks camera/ESP32/inference modules before loading the actual app. Hardware and automatic startup are disabled. Outbound server socket connect/connect_ex calls are blocked. Parent fixture closes the process and cleans its temporary database directory. Public frontend CDNs remain available to the isolated browser.', '',
 'Copied SQLite metadata retains FK/unique and portable CHECK constraints; the single PostgreSQL-specific tray-progress consistency CHECK is omitted only from private SQLite schema construction. This is not a production schema change or migration. PostgreSQL row locking, deployment schema state, RLS and concurrency semantics are not verified.', '',
 'Baseline: six size-specific alerts created by the actual helper using35 synthetic verified Good eggs and29 opening eggs per size; four unread IDs6/4/3/1, two read IDs5/2. Medium milestone2 has eligible cumulative count60, with generated text describing a30-egg tray. Foreign-key-linked boundary claims are real disposable ORM rows. Bulk101/205 cases use summarized valid linked synthetic milestones. These are software fixtures, not physical eggs/sensor/tray verification.', '',
 'Login/Dashboard visits are limited to authentication setup and shared badge hosting. No Login, Dashboard, Sorting Sessions or Egg Records module code was modified or their functional suites rerun.', '',
 '## Test execution report', '',
 '| Test Case ID | Test Scenario | Test Procedure | Test Data | Expected Result | Actual Result | Status |',
 '|---|---|---|---|---|---|---|']
def cell(v):return str(v).replace('|','\\|').replace('\n','<br>')
for r in rows:
    actual=r['actual_result']
    if r['status']=='FAIL' and r['test_case_id'] in findings:actual=findings[r['test_case_id']][1].split(' This violates')[0].split(' Reject null')[0].split(' Validate the JSON')[0]
    lines.append('| '+' | '.join(cell(v) for v in [r['test_case_id'],r['scenario'],r['test_procedure'],r['test_data'],r['expected_result'],actual,r['status']])+' |')
lines += ['', '## Confirmed defects and recommended fixes — not implemented', '']
for r in rows:
    if r['status']=='FAIL' and r['test_case_id'] in findings:
        title,body=findings[r['test_case_id']];lines += [f"**{r['test_case_id']} — {title}.** {body}", '']
extra=[r['test_case_id'] for r in rows if r['status']!='PASS' and r['test_case_id'] not in findings]
if extra:lines += ['Other non-pass outcomes requiring separate investigation: '+', '.join(extra)+'. See original results and INVESTIGATION.md.', '']
lines += [
 'These robustness defects were exercised with controlled unexpected responses; the normal Flask endpoints return their documented JSON. The tests do not claim that production currently emits malformed payloads. Requirements come from this task: preserve confirmed data/counts on malformed responses and avoid misleading acknowledgement success. No new business feature or physical condition was assumed.', '',
 'No application/UI fix was implemented. INVESTIGATION.md distinguishes application behavior from any fixture issues and lists retained attempts. No failed attempt is silently replaced or relabeled; latest-case summaries link to their exact evidence.', '',
 '## Exact execution commands and evidence', '',
 'Repository-root PowerShell; console stdout/stderr was captured with Tee-Object into collection.txt, smoke.txt and full-run.txt (plus separately named rerun logs if used):', '',
 '```powershell',
 '$env:EGGSORT_CHROMEDRIVER="$env:USERPROFILE\\.cache\\selenium\\chromedriver\\win64\\156.0.8078.80\\chromedriver.exe"',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/alerts/test_alerts.py --confcutdir=tests/selenium/alerts --collect-only -q -p no:cacheprovider',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/alerts/test_alerts.py -k \'test_01 or test_04 or test_07 or test_41\' --confcutdir=tests/selenium/alerts -q -p no:cacheprovider --tb=short',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/alerts/test_alerts.py --confcutdir=tests/selenium/alerts -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/alerts-review-20261102/full-run.xml',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/alerts/test_alerts.py -k \'test_37 or test_38 or test_39 or test_40 or test_44\' --confcutdir=tests/selenium/alerts -q -p no:cacheprovider --tb=short --junitxml=testing/selenium-artifacts/alerts-review-20261102/investigation-rerun.xml',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/alerts/test_alerts.py -k test_40 --confcutdir=tests/selenium/alerts -q -p no:cacheprovider --tb=short',
 '.\\.venv\\Scripts\\python.exe -B tests/selenium/alerts/build_report.py',
 '```', '',
 'The existing Python executable required approved filesystem execution access. No dependency installation, visible browser fallback or production connection was needed.', '',
 '- AUDIT.md: read-only feature/contract audit and isolation design.',
 '- Each runs/<timestamp>/inventory.json: case inventory collected before fixtures run.',
 '- Each runs/<timestamp>/environment.json: actual command-line/headless/user-agent/driver evidence.',
 '- Per case: AL-xx-browser.json, AL-xx-server.log, AL-xx-observed.json; failed cases also have AL-xx-failure.png. These were saved directly and never opened or displayed automatically.',
 '- results.json: latest44-case machine-readable report. all-attempts.json: every executed attempt with timestamps/evidence.',
 '- full-run.xml and console logs: pytest outcomes and actual runtime evidence.',
 '- before-hashes.json, before-git-status.txt, after-git-status.txt, preservation-check.json: preservation proof.', '',
 'Run summaries:', '']
for run in runs:
    if (run/'results.json').is_file():
        data=json.loads((run/'results.json').read_text());c=Counter(r['status'] for r in data)
        lines.append(f'- runs/{run.name}/: {len(data)} outcomes; {dict(c)}'+(' (collection only)' if not data else ''))
lines += ['', '## Preservation and limitations', '',
 f'All {len(baseline)} existing baseline files retain their exact SHA256 contents. New isolated source files only: '+', '.join(new_files)+'. Generated evidence is under this report directory. Existing uncommitted work was preserved; no cleanup, commit, push, merge or branch switch occurred.', '',
 'Explicit confirmations: Selenium ran headlessly; no visible Chrome/ChromeDriver browser windows were opened; no personal desktop/browser was controlled; no screenshots/reports/artifacts were opened automatically; no physical hardware was accessed; no production database operations occurred; existing application code and unrelated changes were preserved.', '',
 'Failure screenshots are evidence, not physical hardware verification. Normal polling and UI actions use bounded waits; this is not a load/soak test. Shared badge polling outside Alerts is observed at its real15-second interval. The root audit explains active restored-code per-size behavior but cannot establish production migrations without accessing production, which was forbidden. Browser CDN/Tailwind warnings and denied unrelated Dashboard/favicons are lab noise; injected400/401/503/malformed responses are interpreted against their case procedures.', '',
 'Stopped after testing and documentation. No application changes implemented.', '']
content='\n'.join(lines)
for a,b in {'latest44':'latest 44','Desktop1440':'Desktop 1440','mobile390':'mobile 390',
 'using35':'using 35','and29':'and 29','by the actual helper using35':'by the actual helper using 35',
 'with generated text describing a30':'with generated text describing a 30','Bulk101':'Bulk 101',
 'its real15':'its real 15',';1-second':'; 1-second','and15-second':'and 15-second'}.items():content=content.replace(a,b)
(OUT/'REPORT.md').write_text(content,encoding='utf-8')
print(json.dumps({'unique_cases':len(rows),'attempts':len(attempts),'counts':dict(counts),
    'pass_percentage':round(percent,2),'unchanged_baseline_files':len(baseline),'report':str(OUT/'REPORT.md')}))
