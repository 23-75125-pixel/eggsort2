import json
import os
import queue
import subprocess
import sys
import threading
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("EGGSOR_EVIDENCE_DIR", ROOT / "testing" / "selenium-artifacts")) / "shared" / datetime.now(ZoneInfo("Asia/Manila")).strftime("%Y%m%d-%H%M%S")
RESULTS = []

def pytest_configure(config):
    RUN.mkdir(parents=True, exist_ok=True)
    config.addinivalue_line("markers", "case(id, scenario, expected): Chapter 4 case metadata")

@pytest.fixture(scope="function")
def chrome():
    options = Options()
    if os.environ.get("EGGSORT_SELENIUM_HEADLESS", "0") == "1": options.add_argument("--headless=new")
    profile = tempfile.TemporaryDirectory(prefix="eggsort-selenium-chrome-")
    options.add_argument("--user-data-dir=" + profile.name)
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1440,1000")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    driver = None
    try:
        # Use an explicitly supplied local driver when the host has one but
        # Selenium Manager cannot reach the network. Normal local runs continue
        # to use Selenium Manager.
        driver_path = os.environ.get("EGGSORT_CHROMEDRIVER")
        service = Service(executable_path=driver_path) if driver_path else Service()
        driver = webdriver.Chrome(options=options, service=service)
        (RUN / "browser-environment.json").write_text(json.dumps({
            "chrome": driver.capabilities.get("browserVersion"),
            "chromedriver": driver.capabilities.get("chrome", {}).get("chromedriverVersion"),
            "arguments": options.arguments,
            "userAgent": driver.execute_script("return navigator.userAgent"),
        }, indent=2), encoding="utf-8")
        driver.set_script_timeout(20)
        driver.set_page_load_timeout(30)
        yield driver
    finally:
        if driver: driver.quit()
        profile.cleanup()

@pytest.fixture
def browser(chrome, request):
    yield from _browser_for_server(chrome, request, "lab_server.py")

@pytest.fixture
def sorting_browser(chrome, request):
    """Actual Sorting Sessions routes backed only by disposable SQLite and fakes."""
    yield from _browser_for_server(chrome, request, "sorting_lab_server.py")

def _browser_for_server(chrome, request, server_name):
    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name(server_name))],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    server_errors = deque(maxlen=200)
    threading.Thread(target=lambda: server_errors.extend(process.stderr), daemon=True).start()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        line = ready.get(timeout=30)
        if not line: raise RuntimeError(f"Disposable lab {server_name} failed before startup.")
        lab = json.loads(line)
        lab["base"] = f"http://127.0.0.1:{lab['port']}"
        chrome.delete_all_cookies()
        chrome.set_window_size(1440,1000)
        chrome.get_log("browser")
        request.node.driver = chrome
        request.node.lab = lab
        yield chrome, lab
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        # Do not persist the bootstrap line containing disposable credentials.
        marker = request.node.get_closest_marker("case")
        if marker:
            (RUN / (marker.args[0] + "-server.log")).write_text("".join(server_errors), encoding="utf-8")
        process.stdout.close(); process.stderr.close()

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("case")
    if not marker or report.when == "teardown": return
    if report.when == "setup" and report.passed: return
    case, scenario, expected = marker.args[:3]
    procedure = marker.args[3] if len(marker.args) > 3 else "Execute the marked browser assertions."
    test_data = marker.args[4] if len(marker.args) > 4 else "Disposable lab fixture."
    evidence = []
    driver = getattr(item, "driver", None)
    if driver:
        try:
            logs = driver.get_log("browser")
            path = RUN / f"{case}-browser.json"
            path.write_text(json.dumps(logs, indent=2), encoding="utf-8"); evidence.append(str(path))
            if report.failed:
                path = RUN / f"{case}-failure.png"
                driver.save_screenshot(str(path)); evidence.append(str(path))
        except Exception: pass
    actual = "All Selenium assertions verified." if report.passed else str(report.longrepr)
    if driver:
        try:
            observed = driver.execute_script("""return Object.fromEntries(
              ['camera-idle','camera-active','detection-status','hardware-status','hardware-event',
               'latest-egg-record','camera-error','capture-zone-status','egg-zone-count',
               'last-zone-capture'].map(id=>{const e=document.getElementById(id);
                 return [id,e?{text:e.textContent.trim(),visible:!!e.getClientRects().length}:null]}));""")
            (RUN / f"{case}-observed.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
            evidence.append(str(RUN / f"{case}-observed.json"))
            if report.passed:
                actual += " Observed UI: " + json.dumps({k:v['text'] for k,v in observed.items() if v and v['visible']})
        except Exception:
            pass
    lab = getattr(item, "lab", {})
    server_log = RUN / f"{case}-server.log"
    evidence.append(str(server_log))
    for key in ("password", "control"):
        if lab.get(key): actual = actual.replace(lab[key], "[redacted]")
    RESULTS.append(dict(test_case_id=case, scenario=scenario, test_procedure=procedure,
        test_data=test_data, expected_result=expected,
        actual_result=actual, status="PASS" if report.passed else "BLOCKED" if report.when=="setup" or report.skipped else "FAIL",
        executed_at=datetime.now(ZoneInfo("Asia/Manila")).isoformat(), evidence=evidence))
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")

def pytest_sessionfinish(session, exitstatus):
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    lines = ["# Selenium actual execution results", "", "| Test Case ID | Test Scenario | Test Procedure | Test Data | Expected Result | Actual Result | Status |", "|---|---|---|---|---|---|---|"]
    def cell(value): return str(value).replace("|", "\\|").replace("\n", " ")
    lines += ["| " + " | ".join(cell(r[key]) for key in (
        "test_case_id", "scenario", "test_procedure", "test_data", "expected_result", "actual_result", "status")) + " |" for r in RESULTS]
    (RUN / "summary.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"\nSelenium evidence: {RUN}")

def pytest_collection_modifyitems(items):
    def order(item):
        marker = item.get_closest_marker("case")
        code = marker.args[0] if marker else "ZZ-99"
        return (0 if code.startswith("LG-") else 1, code)
    items.sort(key=order)
