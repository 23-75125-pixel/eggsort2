"""Isolated, headless Selenium evidence for the Reports module."""
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

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get("EGGSOR_EVIDENCE_DIR", ROOT / "testing/selenium-artifacts")) / "Reports Management" / "native"
RUN = OUT / "runs" / datetime.now(ZoneInfo("Asia/Manila")).strftime("%Y%m%d-%H%M%S-%f")
RESULTS = []


def pytest_configure(config):
    RUN.mkdir(parents=True, exist_ok=False)
    config.addinivalue_line("markers", "case(id, scenario, expected, procedure, data): Reports inventory")


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item: item.get_closest_marker("case").args[0])
    inventory = [dict(zip(
        ["test_case_id", "scenario", "expected_result", "test_procedure", "test_data"],
        item.get_closest_marker("case").args,
    )) for item in items]
    (RUN / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")


@pytest.fixture(scope="session")
def chrome():
    profile = tempfile.TemporaryDirectory(prefix="eggsort-reports-chrome-")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--user-data-dir=" + profile.name)
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    options.add_experimental_option("prefs", {"download.prompt_for_download": False})
    driver = None
    try:
        driver_path = os.environ.get("EGGSORT_CHROMEDRIVER")
        service = Service(driver_path) if driver_path else Service()
        if os.name == "nt":
            service.creation_flags = subprocess.CREATE_NO_WINDOW
        driver = webdriver.Chrome(options=options, service=service)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Manila"})
        command = driver.execute_cdp_cmd("Browser.getBrowserCommandLine", {}).get("arguments", [])
        user_agent = driver.execute_script("return navigator.userAgent")
        evidence = {
            "requested_arguments": options.arguments,
            "actual_browser_arguments": command,
            "user_agent": user_agent,
            "capabilities": driver.capabilities,
            "headless_verified": "--headless=new" in command and "HeadlessChrome" in user_agent,
            "driver_no_window_flag": subprocess.CREATE_NO_WINDOW if os.name == "nt" else None,
            "python": sys.version,
            "selenium": __import__("selenium").__version__,
            "pytest": pytest.__version__,
            "timezone": "Asia/Manila",
            "viewport": "1440x1000",
        }
        if not evidence["headless_verified"]:
            raise RuntimeError("Headless Chrome verification failed; no headed fallback is allowed.")
        (RUN / "environment.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        yield driver
    finally:
        if driver:
            driver.quit()
        profile.cleanup()


@pytest.fixture
def reports_browser(chrome, request):
    request.node.driver = chrome
    chrome.get("about:blank")
    chrome.delete_all_cookies()
    chrome.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    chrome.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Manila"})
    chrome.set_window_size(1440, 1000)
    chrome.get_log("browser")
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).with_name("lab_server.py"))],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready = queue.Queue()
    errors = deque(maxlen=2500)
    threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True).start()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        line = ready.get(timeout=30)
        if not line:
            raise RuntimeError("Reports lab exited before startup: " + "".join(errors)[-2000:])
        lab = json.loads(line)
        lab["base"] = f"http://127.0.0.1:{lab['port']}"
        lab["run"] = str(RUN)
        request.node.lab = lab
        yield chrome, lab
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        case_id = request.node.get_closest_marker("case").args[0]
        (RUN / f"{case_id}-server.log").write_text("".join(errors), encoding="utf-8")
        process.stdout.close()
        process.stderr.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "teardown" or (report.when == "setup" and report.passed):
        return
    case_id, scenario, expected, procedure, data = item.get_closest_marker("case").args
    evidence = []
    driver = getattr(item, "driver", None)
    if driver:
        try:
            browser_path = RUN / f"{case_id}-browser.json"
            browser_path.write_text(json.dumps(driver.get_log("browser"), indent=2), encoding="utf-8")
            evidence.append(str(browser_path))
            observed = driver.execute_script("""return {
              url: location.pathname,
              status: document.getElementById('report-status')?.textContent,
              updated: document.getElementById('report-updated-at')?.textContent,
              kpis: Object.fromEntries(['report-total','report-good','report-defects','report-quality-rate'].map(id=>[id,document.getElementById(id)?.textContent])),
              dates: Object.fromEntries(['report-start-date','report-end-date','report-sampling'].map(id=>[id,document.getElementById(id)?.value])),
              rows: Array.from(document.querySelectorAll('#report-rows tr')).map(row=>row.innerText)
            };""")
            observed_path = RUN / f"{case_id}-observed.json"
            observed_path.write_text(json.dumps(observed, indent=2), encoding="utf-8")
            evidence.append(str(observed_path))
            if report.failed:
                screenshot = RUN / f"{case_id}-failure.png"
                driver.save_screenshot(str(screenshot))
                evidence.append(str(screenshot))
        except Exception as exc:
            evidence.append("Evidence capture error: " + str(exc)[:180])
    evidence.append(str(RUN / f"{case_id}-server.log"))
    actual = getattr(item, "actual", "All executed Selenium assertions passed.") if report.passed else str(report.longrepr)
    status = "PASS" if report.passed else "SKIPPED" if report.skipped else "BLOCKED" if report.when == "setup" else "FAIL"
    RESULTS.append({
        "test_case_id": case_id, "scenario": scenario, "test_procedure": procedure,
        "test_data": data, "expected_result": expected, "actual_result": actual,
        "status": status, "evidence": evidence,
        "executed_at": datetime.now(ZoneInfo("Asia/Manila")).isoformat(),
    })
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")


def pytest_sessionfinish(session, exitstatus):
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    print(f"\nReports evidence: {RUN}")
