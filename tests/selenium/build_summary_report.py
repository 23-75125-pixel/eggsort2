"""Build a one-page, print-ready summary from one Selenium evidence run."""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

MODULES = (
    "Login",
    "Dashboard",
    "Sorting Sessions",
    "Egg Records",
    "Alerts",
    "Reports Management",
    "User Management",
)


def build(evidence_dir: Path) -> Path:
    evidence_dir = evidence_dir.resolve()
    data = json.loads((evidence_dir / "results.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    counts = Counter(case["status"].upper() for case in cases)
    total = len(cases)
    passed = counts["PASS"]
    failed = counts["FAIL"]
    skipped = counts["SKIPPED"]
    blocked = counts["BLOCKED"]
    pass_rate = (passed / total * 100) if total else 0.0

    executed = datetime.fromisoformat(data["execution_date"])
    date_label = executed.strftime("%B %d, %Y · %I:%M %p").replace(" 0", " ")
    duration = float(data["duration_seconds"])
    minutes = int(duration // 60)
    seconds = duration - minutes * 60
    duration_label = f"{minutes}m {seconds:05.2f}s"

    rows = []
    for module in MODULES:
        group = [case for case in cases if case.get("module") == module]
        statuses = Counter(case.get("status", "").upper() for case in group)
        rows.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(module)}</th>"
            f"<td>{len(group)}</td><td>{statuses['PASS']}</td>"
            f"<td>{statuses['FAIL']}</td><td>{statuses['SKIPPED']}</td>"
            "</tr>"
        )

    failed_ids = [case["test_case_id"] for case in cases if case.get("status", "").upper() == "FAIL"]
    failure_text = ", ".join(html.escape(case_id) for case_id in failed_ids) or "None"
    blocked_text = (
        f'<p class="blocked">Blocked: {blocked}</p>' if blocked else ""
    )

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EGGSor+ Selenium Summary</title>
<style>
  @page {{ size: A4 portrait; margin: 15mm; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: #17212b; font: 11pt/1.4 Arial, Helvetica, sans-serif; }}
  main {{ max-width: 760px; margin: 18px auto; }}
  h1 {{ margin: 0 0 14px; font-size: 20pt; font-weight: 650; letter-spacing: -.3px; }}
  .meta {{ display: flex; gap: 28px; margin: 0 0 18px; }}
  .meta p {{ margin: 0; }}
  .label {{ display: block; margin-bottom: 2px; color: #526170; font-size: 9pt; }}
  .value {{ font-weight: 600; }}
  .totals {{ display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid #cbd4dc; margin-bottom: 17px; }}
  .metric {{ padding: 10px 12px; border-right: 1px solid #cbd4dc; }}
  .metric:last-child {{ border-right: 0; }}
  .metric strong {{ display: block; font-size: 17pt; line-height: 1.15; font-weight: 650; }}
  h2 {{ margin: 17px 0 7px; font-size: 12pt; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 10pt; }}
  th, td {{ padding: 7px 9px; border-bottom: 1px solid #d9e0e6; text-align: left; }}
  thead th {{ background: #f1f4f6; color: #334354; font-weight: 600; }}
  td:not(:first-child), thead th:not(:first-child) {{ text-align: center; width: 12%; }}
  tbody th {{ font-weight: 500; }}
  .failed {{ margin: 0 0 12px; }}
  .blocked {{ margin: 0 0 12px; }}
  a {{ color: #145b85; text-decoration: underline; }}
  .detail {{ margin-top: 20px; font-size: 9.5pt; }}
  @media print {{
    body {{ font-size: 10.5pt; }}
    main {{ max-width: none; margin: 0; }}
    h1 {{ font-size: 18pt; }}
    .totals, table, tr {{ break-inside: avoid; }}
    a {{ color: #17212b; }}
  }}
</style>
</head>
<body>
<main>
  <h1>EGGSor+ Selenium Test Summary</h1>
  <section class="meta" aria-label="Execution details">
    <p><span class="label">Execution date (Asia/Manila)</span><span class="value">{html.escape(date_label)} · UTC+08:00</span></p>
    <p><span class="label">Total duration</span><span class="value">{duration_label}</span></p>
  </section>
  <section class="totals" aria-label="Test totals">
    <div class="metric"><span class="label">Total Tests</span><strong>{total}</strong></div>
    <div class="metric"><span class="label">Passed</span><strong>{passed}</strong></div>
    <div class="metric"><span class="label">Failed</span><strong>{failed}</strong></div>
    <div class="metric"><span class="label">Skipped</span><strong>{skipped}</strong></div>
  </section>
  <p class="value">Overall pass rate: {pass_rate:.2f}%</p>
  <h2>Results by module</h2>
  <table>
    <thead><tr><th scope="col">Module</th><th scope="col">Total</th><th scope="col">Passed</th><th scope="col">Failed</th><th scope="col">Skipped</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>Failed test case IDs</h2>
  <p class="failed">{failure_text}</p>
  {blocked_text}
  <p class="detail"><a href="index.html">Open full detailed report</a></p>
</main>
</body>
</html>
"""
    output = evidence_dir / "Summary_Report.html"
    output.write_text(document, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_dir", type=Path, help="folder containing this run's results.json")
    args = parser.parse_args()
    print(build(args.evidence_dir))


if __name__ == "__main__":
    main()
