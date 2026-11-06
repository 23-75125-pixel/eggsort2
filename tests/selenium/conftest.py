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

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "testing" / "selenium-artifacts" / datetime.now(ZoneInfo("Asia/Manila")).strftime("%Y%m%d-%H%M%S")
RESULTS = []

def pytest_configure(config):
    RUN.mkdir(parents=True, exist_ok=True)
    config.addinivalue_line("markers", "case(id, scenario, expected): Chapter 4 case metadata")

@pytest.fixture(scope="session")
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
        driver = webdriver.Chrome(options=options)
        driver.set_script_timeout(20)
        driver.set_page_load_timeout(30)
        yield driver
    finally:
        if driver: driver.quit()
        profile.cleanup()

@pytest.fixture
def browser(chrome, request):
    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("lab_server.py"))],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    server_errors = deque(maxlen=200)
    threading.Thread(target=lambda: server_errors.extend(process.stderr), daemon=True).start()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        line = ready.get(timeout=30)
        if not line: raise RuntimeError("Disposable lab failed: " + process.stderr.read()[-3000:])
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
    case, scenario, expected = marker.args
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
    lab = getattr(item, "lab", {})
    for key in ("password", "control"):
        if lab.get(key): actual = actual.replace(lab[key], "[redacted]")
    RESULTS.append(dict(test_case_id=case, scenario=scenario, expected_result=expected,
        actual_result=actual, status="PASS" if report.passed else "BLOCKED" if report.when=="setup" or report.skipped else "FAIL",
        executed_at=datetime.now(ZoneInfo("Asia/Manila")).isoformat(), evidence=evidence))
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")

def pytest_sessionfinish(session, exitstatus):
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    lines = ["# Selenium actual execution results", "", "| Case | Status | Scenario |", "|---|---|---|"]
    lines += [f"| {r['test_case_id']} | {r['status']} | {r['scenario']} |" for r in RESULTS]
    (RUN / "summary.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"\nSelenium evidence: {RUN}")

def pytest_collection_modifyitems(items):
    def order(item):
        marker = item.get_closest_marker("case")
        code = marker.args[0] if marker else "ZZ-99"
        return (0 if code.startswith("LG-") else 1, code)
    items.sort(key=order)
