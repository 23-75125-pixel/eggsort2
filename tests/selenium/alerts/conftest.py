"""Headless-only Alerts runner. Never opens evidence or controls desktop surfaces."""
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
import selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(os.environ.get('EGGSOR_EVIDENCE_DIR',ROOT/'testing/selenium-artifacts'))/'Alerts'/'native'
RUN=OUT/'runs'/datetime.now(ZoneInfo('Asia/Manila')).strftime('%Y%m%d-%H%M%S-%f')
RESULTS=[]
NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)


def pytest_configure(config):
    RUN.mkdir(parents=True,exist_ok=True)
    config.addinivalue_line('markers','case(id, scenario, expected, procedure, data): Alerts inventory')


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item:item.get_closest_marker('case').args[0])
    inventory=[dict(zip(['test_case_id','scenario','expected_result','test_procedure','test_data'],
                       item.get_closest_marker('case').args)) for item in items]
    (RUN/'inventory.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')


@pytest.fixture(scope='session')
def chrome():
    profile=tempfile.TemporaryDirectory(prefix='eggsort-alerts-chrome-')
    options=Options()
    options.add_argument('--headless=new')
    options.add_argument('--window-size=1440,1000')
    options.add_argument('--user-data-dir='+profile.name)
    options.page_load_strategy='eager'
    options.set_capability('goog:loggingPrefs',{'browser':'ALL'})
    driver=None
    try:
        path=os.environ.get('EGGSORT_CHROMEDRIVER')
        service=Service(executable_path=path,popen_kw={'creation_flags':NO_WINDOW}) if path else Service(popen_kw={'creation_flags':NO_WINDOW})
        driver=webdriver.Chrome(options=options,service=service)
        driver.set_page_load_timeout(30);driver.set_script_timeout(25)
        args=driver.execute_cdp_cmd('Browser.getBrowserCommandLine',{})['arguments']
        user_agent=driver.execute_script('return navigator.userAgent')
        if '--headless=new' not in args or 'HeadlessChrome' not in user_agent:
            pytest.exit('STOP: headless Chrome could not be verified; no headed fallback.',returncode=2)
        driver.execute_cdp_cmd('Emulation.setTimezoneOverride',{'timezoneId':'Asia/Manila'})
        (RUN/'environment.json').write_text(json.dumps({'requested_arguments':options.arguments,
            'actual_browser_arguments':args,'user_agent':user_agent,'capabilities':driver.capabilities,
            'headless_verified':True,'driver_no_window_flag':NO_WINDOW,'python':sys.version,
            'selenium':selenium.__version__,'pytest':pytest.__version__,'timezone':'Asia/Manila'},indent=2),encoding='utf-8')
        yield driver
    except Exception as exc:
        pytest.exit('STOP: headless Chrome unavailable; no visible fallback. '+str(exc),returncode=2)
    finally:
        if driver:driver.quit()
        profile.cleanup()


@pytest.fixture
def alerts_browser(chrome,request):
    request.node.driver=chrome
    chrome.get('about:blank')
    chrome.execute_cdp_cmd('Network.clearBrowserCookies',{})
    chrome.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride',{})
    chrome.set_window_size(1440,1000)  # Only this disposable, verified-headless session.
    chrome.get_log('browser')
    database=tempfile.TemporaryDirectory(prefix='eggsort-alerts-lab-')
    environment={**os.environ,'EGGSOR_ALERTS_LAB_DIR':database.name}
    process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('lab_server.py'))],
        cwd=ROOT,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=NO_WINDOW)
    ready=queue.Queue();errors=deque(maxlen=3000)
    reader=threading.Thread(target=lambda:errors.extend(process.stderr),daemon=True);reader.start()
    threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
    try:
        line=ready.get(timeout=30)
        if not line:raise RuntimeError('Alerts lab startup failed: '+''.join(errors)[-3000:])
        lab=json.loads(line);lab['base']=f"http://127.0.0.1:{lab['port']}";lab['run']=str(RUN)
        request.node.lab=lab
        yield chrome,lab
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        reader.join(timeout=2)
        case=request.node.get_closest_marker('case').args[0]
        (RUN/f'{case}-server.log').write_text(''.join(errors),encoding='utf-8')
        process.stdout.close();process.stderr.close()
        database.cleanup()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item,call):
    outcome=yield;report=outcome.get_result()
    if report.when=='teardown' or (report.when=='setup' and report.passed):return
    case,scenario,expected,procedure,data=item.get_closest_marker('case').args
    evidence=[];driver=getattr(item,'driver',None)
    if driver:
        try:
            path=RUN/f'{case}-browser.json';path.write_text(json.dumps(driver.get_log('browser'),indent=2),encoding='utf-8');evidence.append(str(path))
            observed=driver.execute_script("""const badge=document.querySelector('[data-eggsor-unread-badge]');return {
              path:location.pathname,unread:document.getElementById('unread-count')?.textContent,
              badge:badge?{text:badge.textContent,hidden:badge.classList.contains('hidden'),aria:badge.getAttribute('aria-label')}:null,
              cards:Array.from(document.querySelectorAll('#alerts-list article')).slice(0,20).map(e=>e.innerText),
              card_count:document.querySelectorAll('#alerts-list article').length,
              empty:document.getElementById('alerts-empty')?.innerText,error:document.getElementById('alerts-error')?.innerText,
              search:document.getElementById('alert-search')?.value,
              modal:document.querySelector('.swal2-popup')?.innerText};""")
            path=RUN/f'{case}-observed.json';path.write_text(json.dumps(observed,indent=2),encoding='utf-8');evidence.append(str(path))
            if report.failed:
                path=RUN/f'{case}-failure.png';driver.save_screenshot(str(path));evidence.append(str(path))
        except Exception as exc:evidence.append('Capture error: '+str(exc)[:150])
    evidence.append(str(RUN/f'{case}-server.log'))
    actual=getattr(item,'actual','All executed assertions passed.') if report.passed else str(report.longrepr)
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
    print(f'\nAlerts evidence: {RUN}')
