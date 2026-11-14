"""Run the existing EGGSor+ Selenium suites and create consolidated evidence."""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("EGGSOR_EVIDENCE_DIR", ROOT / "EGGSor_Test_Evidence")).resolve()
OUT.mkdir(exist_ok=True)
affected_only = "--affected" in sys.argv[1:]

# The legacy shared browser fixture is headless only when this flag is enabled.
os.environ["EGGSORT_SELENIUM_HEADLESS"] = "1"
os.environ["EGGSOR_EVIDENCE_DIR"] = str(OUT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "selenium"))

import pytest
from evidence_plugin import EvidencePlugin

if affected_only:
    targets = [
        "tests/selenium/test_functional.py",
        "tests/selenium/test_sorting_sessions.py",
        "tests/selenium/test_sorting_sessions_review.py",
        "tests/selenium/reports/test_reports.py",
    ]
else:
    targets = [
        "tests/selenium/test_functional.py",
        "tests/selenium/test_sorting_sessions.py",
        "tests/selenium/test_sorting_sessions_review.py",
        "tests/selenium/test_ss31_response_fallback.py",
        "tests/selenium/egg_records/test_egg_records.py",
        "tests/selenium/alerts/test_alerts.py",
        "tests/selenium/reports/test_reports.py",
        "tests/selenium/user_management/test_user_management.py",
    ]
args = [*targets, "--confcutdir=tests/selenium", "-q", "-p", "no:cacheprovider",
        "--tb=short", f"--junitxml={OUT / 'junit.xml'}"]
if affected_only:
    args.extend(["-k", " or ".join([
        "test_db06", "test_06_layout", "test_08_independent_kpis",
        "test_09_verification_categories", "test_10_daily_group", "test_11_weekly",
        "test_12_monthly", "test_26_session_aggregation", "test_31_safe_quality_text",
        "test_32_manila_iso_week_boundary", "test_33_manila_month_boundary",
        "test_ss21_stale_capture_is_not_marked_verified", "test_stop_failure_retry",
    ])])
plugin = EvidencePlugin(OUT)
exit_code = pytest.main(args, plugins=[plugin])
# Remove disposable one-time links and lab-control keys from text evidence.
for path in OUT.rglob("*"):
    if path.is_file() and path.suffix.lower() in {".json", ".log", ".csv", ".html", ".xml", ".md", ".txt"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"(?i)(/(?:accept-invite|reset-password)/)[A-Za-z0-9_-]{16,}", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)([?&]key=)[^&\s\"<>]+", r"\1[REDACTED]", text)
        path.write_text(text, encoding="utf-8")
raise SystemExit(int(exit_code))
