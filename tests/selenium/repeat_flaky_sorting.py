"""Repeat the timing-sensitive SS-04 and SS-32 cases in isolated runs."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHILD = r'''
import os, sys
from pathlib import Path
import pytest
root = Path.cwd()
sys.path.insert(0, str(root / "tests" / "selenium"))
from evidence_plugin import EvidencePlugin
out = Path(os.environ["EGGSOR_EVIDENCE_DIR"])
args = [
    "tests/selenium/test_sorting_sessions.py",
    "tests/selenium/test_sorting_sessions_review.py",
    "--confcutdir=tests/selenium", "-q", "-p", "no:cacheprovider",
    "--tb=short", "-k",
    "test_ss04_start_is_not_reported_ready_early or test_pending_buttons",
    f"--junitxml={out / 'junit.xml'}",
]
raise SystemExit(pytest.main(args, plugins=[EvidencePlugin(out)]))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    output = (ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    any_failure = False

    for number in range(1, args.repeat + 1):
        run_dir = output / f"iteration-{number:02d}"
        run_dir.mkdir()
        env = os.environ.copy()
        env["EGGSOR_EVIDENCE_DIR"] = str(run_dir)
        env["EGGSORT_SELENIUM_HEADLESS"] = "1"
        result = subprocess.run(
            [sys.executable, "-B", "-c", CHILD], cwd=ROOT, env=env, check=False
        )
        result_path = run_dir / "results.json"
        if not result_path.is_file():
            rows.append({"Iteration": number, "Total": 0, "Passed": 0,
                         "Failed": 0, "Skipped": 0, "Failed Case IDs": "NO RESULTS"})
            any_failure = True
            continue

        data = json.loads(result_path.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        counts = data.get("counts", {})
        failed_ids = [case["test_case_id"] for case in cases if case["status"] == "FAIL"]
        expected = {"SS-04", "SS-32"}
        if {case["test_case_id"] for case in cases} != expected:
            failed_ids.append("HARNESS_CASE_SELECTION_ERROR")
        passed = counts.get("PASS", 0)
        failed = counts.get("FAIL", 0)
        skipped = counts.get("SKIPPED", 0) + counts.get("BLOCKED", 0)
        rows.append({"Iteration": number, "Total": len(cases), "Passed": passed,
                     "Failed": failed, "Skipped": skipped,
                     "Failed Case IDs": ", ".join(failed_ids)})
        print(f"Iteration {number}: {passed} passed, {failed} failed, {skipped} skipped")
        if result.returncode or len(cases) != 2 or failed or skipped:
            any_failure = True

    with (output / "repeat-summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Iteration", "Total", "Passed", "Failed", "Skipped", "Failed Case IDs"])
        writer.writeheader()
        writer.writerows(rows)
    (output / "repeat-summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Repeat summary: {output / 'repeat-summary.csv'}")
    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
