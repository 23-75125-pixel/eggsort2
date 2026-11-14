"""Compile executed Sorting Sessions evidence; never imports application code."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'testing/selenium-artifacts/sorting-review-20261101'
runs = [ROOT / 'testing/selenium-artifacts' / arg for arg in sys.argv[1:]]
attempts = [row for run in runs for row in json.loads((run/'results.json').read_text())]
rows = list({r['test_case_id']:r for r in sorted(attempts,key=lambda r:r['executed_at'])}.values())
rows.sort(key=lambda r:r['test_case_id'])
counts = Counter(r['status'] for r in rows)
OUT.mkdir(parents=True, exist_ok=True)
(OUT/'combined-results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
(OUT/'all-attempts.json').write_text(json.dumps(attempts,indent=2),encoding='utf-8')
actuals = {
'SS-01':'Anonymous page request redirected to /login.',
'SS-02':'Authenticated admin opened Sorting Sessions; Start enabled; initial empty capture text present.',
'SS-03':'Cancel left session count and both worker start counts at zero.',
'SS-04':'UI remained Starting; one session created; no premature success notice.',
'SS-05':'Frame-only remained starting; controller readiness advanced same session to running and success notice.',
'SS-06':'Late timeout callback preserved running session and recovery_required=false.',
'SS-07':'Missing first frame produced failed/recovery state and visible timeout; both fake workers stopped.',
'SS-08':'Missing controller READY produced failed state naming ESP32 controller readiness.',
'SS-09':'Timeout cleanup error retained failed/recovery state; software did not claim verified physical actuation.',
'SS-10':'Cancel Stop kept lifecycle running; both worker stop counters stayed zero.',
'SS-11':'Confirmed Stop completed session, stopped both fake workers and showed Closed Successfully; physical verification false.',
'SS-12':'Startup exception failed session and showed Operation failed, but message was Unexpected token (known error-format defect).',
'SS-13':'Audit fault displayed nonfatal warning; one starting session persisted in disposable SQLite.',
'SS-14':'Rejected status fetch displayed Camera/session status unavailable and transport error rather than Session stopped.',
'SS-15':'Disconnect failed session; reconnect did not clear recovery_required or return to running.',
'SS-16':'Duplicate Start while confirmation open created exactly one session and one start per worker.',
'SS-17':'Stale poll did not overwrite newer text; stalled JSON body hit timed-out error.',
'SS-18':'UI displayed exit count 2, Egg #41 and good zone classification.',
'SS-19':'SQLite record: 59g, Medium, Not Classified, unverified, confidence 0; one capture marker and no pending cycle.',
'SS-20':'SQLite record: 59g, Medium, Good, verified, confidence .94; capture marker ended :77.',
'SS-22':'Expired fake capture failed lifecycle with flow_error, no egg record and retained queue head; recovery and expired text visible.',
'SS-23':'Sidebar navigation worked; idle visible, active hidden; expected IDs unique, ESP32 stopped, count 0, no completed egg, no horizontal overflow.',
'SS-24':'Loading overlay shown before readiness; afterward hidden and fake MJPEG image naturalWidth > 0.',
'SS-25':'Restart after Stop created second session with new reference; first completed; exactly two starts per fake worker.',
'SS-26':'Running refresh retained reference and start count 1; stopped refresh remained completed and idle.',
'SS-27':'Staff role could start and stop through UI; resulting lifecycle completed.',
'SS-28':'Remote-mode button disabled and sorting-computer instruction shown; five hardware APIs returned 409.',
'SS-29':'Actual polling rendered 0g, reading 2 at 58.5g, final 59g, simulated IR message and unavailable-servo message.',
'SS-30':'Latest completed record displayed 59 g, Medium, Good and Verified.',
'SS-32':'Start and Stop initiating controls disabled during held fetch; duplicate clicks sent one request; controls restored after responses.',
'SS-33':'Injected hardware fetch failure displayed Hardware status unavailable; restored transport returned Connected on COM-FAKE.',
'SS-34':'Stop cleanup failure remained recovery-required without success notice; removing fault and retrying completed session.',
'SS-35':'Latest record displayed 59 g, Medium, Not Classified and Unverified.',
'SS-36':'Four anonymous sorting API fetches returned 401 with Authentication is required JSON.',
}
def cell(v): return str(v).replace('|','\\|').replace('\n','<br>')
lines=['# EGGSor+ Sorting Sessions — headless Selenium execution', '',
 'Execution date: November 1, 2026 (Asia/Manila).', '',
 f"**{len(rows)} executed: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['BLOCKED']} blocked, {counts['SKIPPED']} skipped.**", '',
 f"These are the latest results for {len(rows)} unique cases across {len(attempts)} case attempts. All attempts are retained in all-attempts.json.", '',
 f"Recorded case completion range: {min(r['executed_at'] for r in rows)} to {max(r['executed_at'] for r in rows)}.", '',
 'Real restored Flask routes, Jinja templates, browser JavaScript, auth/CSRF, lifecycle and ORM record paths were exercised through Selenium WebDriver. Each case used a fresh loopback server, random disposable credentials, temporary SQLite and fake camera/ESP32 workers. Chrome used `--headless=new`; no browser windows were opened. Login was used only as authentication setup; Login and Dashboard test suites were not rerun.', '',
 'Python 3.13.14; Selenium 4.52.0; pytest 9.2.0; Flask 3.1.3; Chrome 156.0.8078.76; ChromeDriver 156.0.8078.80; 1440 × 1000 viewport. The supplemental run saves browser-environment.json, including HeadlessChrome user agent and launch arguments.', '',
 'The lab suppresses dotenv loading and production database initialization before loading the actual app. Hardware and inference modules are replaced before import; private SQLite is checked before schema creation. Python outbound connect calls are blocked during app loading and server operation. Browser frontend CDN assets may still load from their normal public hosts. No production Supabase records, physical webcam, COM8, ESP32, servos or load cell were used.', '',
 '## Executed cases', '',
 '| ID | Scenario | Procedure | Test data | Expected result | Actual result | Status |',
 '|---|---|---|---|---|---|---|']
for r in rows:
    actual=actuals.get(r['test_case_id'],r['actual_result']) if r['status']=='PASS' else r['actual_result']
    if r['test_case_id']=='SS-21' and r['status']=='FAIL':
        actual='quality_verified was True; identity-safe assertion expected False. Same-session queued capture 501 was accepted for controller cycle 1 without a shared identity proof.'
    if r['test_case_id']=='SS-31' and r['status']=='FAIL':
        actual='Operation failed modal showed JSON parser text (Unexpected token) instead of the injected ESP32 startup failure. See screenshot and server traceback.'
    lines.append('| '+' | '.join(cell(v) for v in [r['test_case_id'],r['scenario'],r['test_procedure'],r['test_data'],r['expected_result'],actual,r['status']])+' |')
lines += ['', '## Findings and recommended fixes (not implemented)', '',
 '1. **Capture association has no demonstrated per-egg identity proof (SS-21).** A queued same-session Good capture with ID 501 was accepted for controller cycle 1 and marked verified. Camera and controller IDs are separate namespaces, so numeric inequality alone does not prove this was a physically different egg; the test deliberately applies the stricter identity-safe expectation. The fixture supplies no capture timestamp, and this result does not establish chronological expiry. Review the intended FIFO contract and require an explicit association, or retain Unverified when identity cannot be established. Add per-egg, reordered, delayed and duplicate capture cases after that decision.', '',
 '2. **Startup exceptions leak a JSON parse error to the operator (SS-12, SS-31).** Fake camera/bridge start raises LifecycleError. The start route catches CameraSessionError only, returning HTML 500 for LifecycleError. The browser then reports Unexpected token rather than a useful camera/ESP32 message. SS-12 passes its narrow failure/no-false-success contract while documenting this defect; SS-31 explicitly requires a readable ESP32 error and fails. Recommend returning a structured JSON error for lifecycle startup failures and gracefully handling non-JSON error bodies.', '',
 'No product fixes were implemented. Fake failure injections and control endpoints exist only in isolated Selenium files.', '',
 '## Commands and evidence', '',
 'Run from the repository root in PowerShell:', '', '```powershell',
 "$env:EGGSORT_SELENIUM_HEADLESS='1'",
 '$env:EGGSORT_CHROMEDRIVER="$env:USERPROFILE\\.cache\\selenium\\chromedriver\\win64\\156.0.8078.80\\chromedriver.exe"',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/test_sorting_sessions.py --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short',
 '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/test_sorting_sessions_review.py --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short',
 '```', '',
 'These two commands were executed as separate processes. The first sandboxed attempt could not access the existing venv Python executable and ran no tests. Retrying with approved executable access launched the suites. No installation or application-code mutation was needed.', '',
 '**Test-fixture correction:** SS-14 initially timed out because the one-shot fetch override could be skipped while an existing camera status poll was pending, or restored before the next Selenium observation. Only the test was corrected to await any pending poll and retain the injected failure through the assertions. The original FAIL screenshot/logs remain in the first run; the targeted rerun determines its final result. Rerun command:', '',
 '```powershell', '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/test_sorting_sessions.py -k ss14 --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short', '```', '',
 '**Expectation correction:** SS-36 initially expected HTTP redirects for API requests. Inspection and logs confirmed that the current API contract is 401 JSON; the frontend fetch wrapper separately redirects the browser to Login. The test was corrected to assert all four 401 JSON responses, and passed on rerun. This is a test expectation error, not a product defect. Original FAIL evidence remains. Rerun command:', '',
 '```powershell', '.\\.venv\\Scripts\\python.exe -B -m pytest tests/selenium/test_sorting_sessions_review.py -k anonymous_apis --confcutdir=tests/selenium -q -p no:cacheprovider --tb=short', '```', '',
 'Evidence directories:', '']
for run in runs:
    lines += [f'- `{run.relative_to(ROOT).as_posix()}/`: per-case results.json, summary.md, browser logs and bounded server logs; failed cases have `SS-xx-failure.png`. Supplemental cases also save observed UI JSON and browser environment.']
lines += ['', 'The combined machine-readable record is `combined-results.json` beside this report. Full assertion failures remain there and in the original run results; the table above condenses them. Console evidence is saved beside this report. Expected injected HTTP errors, denied unrelated endpoints/favicon and Tailwind development-CDN warnings are not automatically test failures.', '',
 '## Scope limits', '',
 '- All hardware events, readiness, weights and classifications were simulated. Physical frame quality, model inference accuracy, serial behavior, electrical sensors and actuator motion remain untested; no physical checks were attempted.',
 '- SQLite copies omit PostgreSQL CHECK constraints; production Supabase, RLS, concurrent multi-station behavior and production performance were not tested.',
 '- Readiness timeout cases invoke the real timeout callback as a simulated deadline; they do not wait the full 45 seconds.',
 '- The current UI labels the idle start action Restart Camera & ESP32; restart coverage is stop followed by start, since the active panel has only Stop Session.',
 '- Initial ESP32 and weight details are inside the hidden active panel; the initial state test inspects DOM text as well as visible idle layout. Layout verification covers expected elements, visibility and horizontal overflow at the stated desktop viewport.',
 '- Staff operation is permitted by current login_required routes. No admin-only Sorting Sessions rule was inferred. Remote hardware mode and anonymous restrictions were exercised.',
 '- Existing uncommitted production work was preserved; see before-hashes.json, before-git-status.txt and preservation-check.json. Only isolated Selenium files and generated evidence were added or updated.', '']
(OUT/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'cases':len(rows),'counts':dict(counts),'report':str(OUT/'REPORT.md')}))
