"""Headless-only Selenium fixtures for isolated User Management tests."""
import json
import os
import queue
import re
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

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get("EGGSOR_EVIDENCE_DIR", ROOT / "testing/selenium-artifacts")) / "User Management" / "native"
RUN = OUT / "runs" / datetime.now(ZoneInfo("Asia/Manila")).strftime("%Y%m%d-%H%M%S-%f")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESULTS = []


def pytest_configure(config):
    RUN.mkdir(parents=True, exist_ok=False)
    config.addinivalue_line("markers", "case(id, scenario, procedure, data, expected): User Management inventory")


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item: item.get_closest_marker("case").args[0])
    inventory = [dict(zip(
        ["test_case_id", "scenario", "test_procedure", "test_data", "expected_result"],
        item.get_closest_marker("case").args,
    )) for item in items]
    (RUN / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")


@pytest.fixture(scope="session")
def chrome():
    profile = tempfile.TemporaryDirectory(prefix="eggsort-user-management-chrome-")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--user-data-dir=" + profile.name)
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    driver = None
    try:
        driver_path = os.environ.get("EGGSORT_CHROMEDRIVER")
        service = Service(driver_path) if driver_path else Service()
        if os.name == "nt":
            service.creation_flags = NO_WINDOW
        driver = webdriver.Chrome(options=options, service=service)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(25)
        actual_args = driver.execute_cdp_cmd("Browser.getBrowserCommandLine", {}).get("arguments", [])
        user_agent = driver.execute_script("return navigator.userAgent")
        if "--headless=new" not in actual_args or "HeadlessChrome" not in user_agent:
            pytest.exit("STOP: headless Chrome could not be verified; no headed fallback is allowed.", returncode=2)
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Manila"})
        environment = {
            "requested_arguments": options.arguments,
            "actual_browser_arguments": actual_args,
            "user_agent": user_agent,
            "capabilities": driver.capabilities,
            "headless_verified": True,
            "driver_no_window_flag": NO_WINDOW,
            "python": sys.version,
            "selenium": selenium.__version__,
            "pytest": pytest.__version__,
            "timezone": "Asia/Manila",
            "viewport": "1440x1000",
        }
        (RUN / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
        yield driver
    except pytest.Exit:
        raise
    except Exception as exc:
        pytest.exit("STOP: headless Selenium setup failed; no visible fallback. " + str(exc), returncode=2)
    finally:
        if driver:
            driver.quit()
        profile.cleanup()


@pytest.fixture
def um_browser(chrome, request):
    request.node.driver = chrome
    chrome.get("about:blank")
    chrome.delete_all_cookies()
    chrome.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    chrome.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Manila"})
    chrome.set_window_size(1440, 1000)
    chrome.get_log("browser")

    database = tempfile.TemporaryDirectory(prefix="eggsort-user-management-lab-")
    env = {**os.environ, "EGGSOR_USER_MANAGEMENT_LAB_DIR": database.name}
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).with_name("lab_server.py"))],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=NO_WINDOW,
    )
    ready = queue.Queue()
    errors = deque(maxlen=3000)
    stderr_reader = threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True)
    stderr_reader.start()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        line = ready.get(timeout=30)
        if not line:
            raise RuntimeError("User Management lab startup failed: " + "".join(errors)[-2500:])
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
        stderr_reader.join(timeout=2)
        case_id = request.node.get_closest_marker("case").args[0]
        server_log = "".join(errors)
        server_log = re.sub(r"(/accept-invite/)[A-Za-z0-9_-]+", r"\1[redacted]", server_log)
        server_log = re.sub(r"([?&]key=)[^&\s\"]+", r"\1[redacted]", server_log)
        (RUN / f"{case_id}-server.log").write_text(server_log, encoding="utf-8")
        process.stdout.close()
        process.stderr.close()
        database.cleanup()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "teardown" or (report.when == "setup" and report.passed):
        return
    case_id, scenario, procedure, data, expected = item.get_closest_marker("case").args
    evidence = []
    driver = getattr(item, "driver", None)
    if driver:
        try:
            browser_log = RUN / f"{case_id}-browser.json"
            browser_log.write_text(json.dumps(driver.get_log("browser"), indent=2), encoding="utf-8")
            evidence.append(str(browser_log))
            observed = driver.execute_script("""return {
              path:location.pathname, title:document.title,
              users:Array.from(document.querySelectorAll('#users-grid article')).map(e=>e.innerText),
              userCount:document.querySelectorAll('#users-grid article').length,
              userModal:document.getElementById('user-modal')?.className,
              inviteModal:document.getElementById('invite-modal')?.className,
              error:document.querySelector('[role=alert]')?.innerText,
              dialog:document.querySelector('.swal2-popup')?.innerText,
              viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth}
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
    server_log = RUN / f"{case_id}-server.log"
    evidence.append(str(server_log))
    actual = getattr(item, "actual", "All executed Selenium assertions passed.") if report.passed else str(report.longrepr)
    lab = getattr(item, "lab", {})
    for value in (lab.get("control_key"), lab.get("admin_password"), lab.get("staff_password")):
        if value:
            actual = actual.replace(value, "[redacted]")
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
    print(f"\nUser Management evidence: {RUN}")
