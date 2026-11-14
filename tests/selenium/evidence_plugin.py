"""Per-case evidence and machine-readable results for the Selenium suites."""
import csv
import html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest


PREFIX_MODULE = {
    "LG": "Login", "DB": "Dashboard", "SS": "Sorting Sessions",
    "ER": "Egg Records", "AL": "Alerts", "RP": "Reports Management",
    "UM": "User Management",
}


class EvidencePlugin:
    def __init__(self, output):
        self.output = Path(output)
        self.started = time.monotonic()
        self.rows = {}
        self.started_at = datetime.now(ZoneInfo("Asia/Manila"))
        self.output.mkdir(parents=True, exist_ok=True)

    def pytest_configure(self, config):
        self.config = config

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        self.record_report(item, outcome.get_result())

    def record_report(self, item, report):
        if report.when == "setup" and report.passed:
            return
        if report.when == "call" or (report.when == "setup" and (report.failed or report.skipped)):
            marker = item.get_closest_marker("case")
            if not marker or not marker.args:
                return
            case_id = str(marker.args[0])
            parts = case_id.split("-", 1)
            module = PREFIX_MODULE.get(parts[0], "Other")
            scenario = str(marker.args[1]) if len(marker.args) > 1 else item.name
            expected = str(marker.args[2]) if len(marker.args) > 2 else "See the existing test assertion."
            status = "PASS" if report.passed else "SKIPPED" if report.skipped else (
                "BLOCKED" if report.when == "setup" else "FAIL"
            )
            case_dir = self.output / module / case_id
            case_dir.mkdir(parents=True, exist_ok=True)
            screenshot = None
            driver = getattr(item, "driver", None)
            if driver:
                try:
                    # All lab data is synthetic; blank password controls before capture as defense in depth.
                    driver.execute_script("""document.querySelectorAll('input[type=password]').forEach(e=>e.value='');
                      document.querySelectorAll('input,textarea,a,[data-invite-url]').forEach(e=>{
                        const v=('value' in e?e.value:e.textContent)||'';
                        if(/(?:accept-invite|reset-password)/i.test(v)){
                          if('value' in e)e.value='[disposable link redacted]';
                          else e.textContent='[disposable link redacted]';
                        }
                      });""")
                    name = "state.png" if status == "PASS" else "failure.png"
                    path = case_dir / name
                    if driver.save_screenshot(str(path)):
                        screenshot = str(path.relative_to(self.output)).replace("\\", "/")
                except Exception:
                    pass
            actual = getattr(item, "actual", None)
            if not actual:
                actual = "All test assertions passed." if status == "PASS" else (
                "Skipped by pytest." if status == "SKIPPED" else str(report.longrepr or "Test failed.")
                )
            lab = getattr(item, "lab", {}) or {}
            for key, value in lab.items():
                if any(secret in key.lower() for secret in ("password", "token", "control", "secret")) and value:
                    actual = actual.replace(str(value), "[redacted]")
            actual = re.sub(r"(?i)(password|token|secret|control_key)=([^&\s]+)", r"\1=[redacted]", str(actual))
            self.rows[case_id] = {
                "test_case_id": case_id, "module": module, "scenario": scenario,
                "expected_result": expected, "actual_result": actual, "status": status,
                "evidence_filename": screenshot or "", "evidence_path": screenshot,
            }

    def pytest_sessionfinish(self, session, exitstatus):
        rows = sorted(self.rows.values(), key=lambda r: r["test_case_id"])
        duration = time.monotonic() - self.started
        counts = Counter(row["status"] for row in rows)
        (self.output / "results.json").write_text(json.dumps({
            "execution_date": self.started_at.isoformat(), "duration_seconds": round(duration, 2),
            "counts": dict(counts), "cases": rows,
        }, indent=2), encoding="utf-8")
        with (self.output / "summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=["Test Case ID", "Module", "Scenario", "Expected Result", "Actual Result", "Status", "Evidence Filename"])
            writer.writeheader()
            for row in rows:
                writer.writerow({"Test Case ID": row["test_case_id"], "Module": row["module"], "Scenario": row["scenario"],
                    "Expected Result": row["expected_result"], "Actual Result": row["actual_result"], "Status": row["status"],
                    "Evidence Filename": row["evidence_filename"]})
        self._write_html(rows, duration, counts)

    def _write_html(self, rows, duration, counts):
        esc = lambda value: html.escape(str(value or ""))
        groups = "".join(f"<tr><td>{esc(r['test_case_id'])}</td><td>{esc(r['module'])}</td><td>{esc(r['scenario'])}</td><td>{esc(r['expected_result'])}</td><td>{esc(r['actual_result'])}</td><td class='{esc(r['status'].lower())}'>{esc(r['status'])}</td><td>{('<a href="'+esc(r['evidence_path'])+'">Screenshot</a>') if r['evidence_path'] else 'Unavailable'}</td></tr>" for r in rows)
        total = len(rows)
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>EGGSor+ Selenium Evidence</title>
<style>body{{font:14px system-ui;margin:2rem;color:#202936}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd3dc;padding:.55rem;text-align:left;vertical-align:top}}th{{background:#edf2f7}}.pass{{color:#087443}}.fail{{color:#b42318}}.skipped{{color:#946200}}img{{max-width:900px}}.meta{{padding:1rem;background:#f5f7fa}}</style></head><body>
<h1>EGGSor+ Selenium Test Evidence</h1><div class='meta'>Execution date: {esc(self.started_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}<br>Duration: {duration:.2f} seconds<br>Total: {total} | PASS: {counts['PASS']} | FAIL: {counts['FAIL']} | BLOCKED: {counts['BLOCKED']} | SKIPPED: {counts['SKIPPED']}</div>
<p>Counts include cases that reached a pytest call result or were skipped/blocked during setup. Screenshots are linked where Chrome exposed a meaningful page state.</p><table><thead><tr><th>Case ID</th><th>Module</th><th>Scenario</th><th>Expected Result</th><th>Actual Result</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{groups}</tbody></table></body></html>"""
        (self.output / "index.html").write_text(body, encoding="utf-8")

