"""Headless-only fixtures and actual-result evidence, separate from prior suites."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(os.environ.get('EGGSOR_EVIDENCE_DIR',ROOT/'testing/selenium-artifacts'))/'Egg Records'/'native'
RUN=OUT/'runs'/datetime.now(ZoneInfo('Asia/Manila')).strftime('%Y%m%d-%H%M%S-%f')
RESULTS=[]


def pytest_configure(config):
    RUN.mkdir(parents=True,exist_ok=True)
    config.addinivalue_line('markers','case(id, scenario, expected, procedure, data): Egg Records inventory')


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item:item.get_closest_marker('case').args[0])
    inventory=[dict(zip(['test_case_id','scenario','expected_result','test_procedure','test_data'],
                       item.get_closest_marker('case').args)) for item in items]
    (RUN/'inventory.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')


@pytest.fixture(scope='session')
def chrome():
    profile=tempfile.TemporaryDirectory(prefix='eggsort-records-chrome-')
    options=Options()
    options.add_argument('--headless=new')
    options.add_argument('--window-size=1440,1000')
    options.add_argument('--user-data-dir='+profile.name)
    options.page_load_strategy='eager'
    options.set_capability('goog:loggingPrefs',{'browser':'ALL'})
    options.add_experimental_option('prefs',{'download.default_directory':str(RUN/'downloads'),
        'download.prompt_for_download':False})
    driver=None
    try:
        path=os.environ.get('EGGSORT_CHROMEDRIVER')
        driver=webdriver.Chrome(options=options,service=Service(path) if path else Service())
        driver.set_page_load_timeout(30); driver.set_script_timeout(20)
        driver.execute_cdp_cmd('Emulation.setTimezoneOverride',{'timezoneId':'Asia/Manila'})
        driver.execute_cdp_cmd('Browser.setDownloadBehavior',{'behavior':'allow','downloadPath':str(RUN/'downloads')})
        (RUN/'environment.json').write_text(json.dumps({'arguments':options.arguments,
            'capabilities':driver.capabilities,'user_agent':driver.execute_script('return navigator.userAgent'),
            'timezone':'Asia/Manila','python':sys.version},indent=2),encoding='utf-8')
        yield driver
    finally:
        if driver: driver.quit()
        profile.cleanup()


@pytest.fixture
def records_browser(chrome,request):
    request.node.driver=chrome
    chrome.get('about:blank')
    chrome.delete_all_cookies()
    chrome.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride',{})
    chrome.set_window_size(1440,1000)
    chrome.get_log('browser')
    process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('lab_server.py'))],
        cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    ready=queue.Queue(); errors=deque(maxlen=2000)
    threading.Thread(target=lambda:errors.extend(process.stderr),daemon=True).start()
    threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
    try:
        line=ready.get(timeout=30)
        if not line: raise RuntimeError('Records lab failed before startup: '+''.join(errors)[-2000:])
        lab=json.loads(line);lab['base']=f"http://127.0.0.1:{lab['port']}"
        lab['run']=str(RUN)
        request.node.lab=lab
        yield chrome,lab
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill();process.wait(timeout=5)
        case=request.node.get_closest_marker('case').args[0]
        (RUN/f'{case}-server.log').write_text(''.join(errors),encoding='utf-8')
        process.stdout.close();process.stderr.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item,call):
    outcome=yield
    report=outcome.get_result()
    if report.when=='teardown' or (report.when=='setup' and report.passed):return
    case,scenario,expected,procedure,data=item.get_closest_marker('case').args
    evidence=[]
    driver=getattr(item,'driver',None)
    if driver:
        try:
            path=RUN/f'{case}-browser.json'
            path.write_text(json.dumps(driver.get_log('browser'),indent=2),encoding='utf-8');evidence.append(str(path))
            observed=driver.execute_script("""return {url:location.pathname,
              status:document.getElementById('records-status-text')?.textContent,
              row_count:document.querySelectorAll('#egg-records-body tr[data-record-id]').length,
              rows:Array.from(document.querySelectorAll('#egg-records-body tr')).slice(0,20).map(r=>r.innerText),
              filters:Object.fromEntries(['record-search','size-filter','quality-filter','start-date-filter','end-date-filter'].map(id=>[id,document.getElementById(id)?.value]))};""")
            path=RUN/f'{case}-observed.json'
            path.write_text(json.dumps(observed,indent=2),encoding='utf-8');evidence.append(str(path))
            if report.failed:
                path=RUN/f'{case}-failure.png';driver.save_screenshot(str(path));evidence.append(str(path))
        except Exception as exc:
            evidence.append('Capture error: '+str(exc)[:150])
    evidence.append(str(RUN/f'{case}-server.log'))
    actual=getattr(item,'actual','All executed Selenium assertions passed.') if report.passed else str(report.longrepr)
    for key in ('password','control'):
        value=getattr(item,'lab',{}).get(key)
        if value:actual=actual.replace(value,'[redacted]')
    status='PASS' if report.passed else 'SKIPPED' if report.skipped else 'BLOCKED' if report.when=='setup' else 'FAIL'
    RESULTS.append(dict(test_case_id=case,scenario=scenario,test_procedure=procedure,test_data=data,
        expected_result=expected,actual_result=actual,status=status,evidence=evidence,
        executed_at=datetime.now(ZoneInfo('Asia/Manila')).isoformat()))
    (RUN/'results.json').write_text(json.dumps(RESULTS,indent=2),encoding='utf-8')


def pytest_sessionfinish(session,exitstatus):
    (RUN/'results.json').write_text(json.dumps(RESULTS,indent=2),encoding='utf-8')
    print(f'\nEgg Records evidence: {RUN}')
