# EGGSor+ Selenium evidence runner

Run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -B tests\selenium\run_evidence.py
```

After editing existing cases, the 13 November 9 failures can be reviewed
without replacing the full-run evidence by setting a separate output folder and
adding `--affected`:

```powershell
$env:EGGSOR_EVIDENCE_DIR = Join-Path (Get-Location) 'EGGSor_Test_Evidence\review-20261109'
.\.venv\Scripts\python.exe -B tests\selenium\run_evidence.py --affected
```

The runner forces `--headless=new`, runs the existing Login, Dashboard, Sorting
Sessions, Egg Records, Alerts, Reports Management, and User Management suites,
and writes the consolidated HTML, CSV, JUnit XML, JSON and per-case screenshots
to `EGGSor_Test_Evidence/`. Open `EGGSor_Test_Evidence/index.html` in a browser
or double-click it in File Explorer. The report links to each available case
screenshot. The output directory is ignored by Git.

The repository virtual environment must have pytest, Selenium, and the
application dependencies installed. Google Chrome and a compatible
ChromeDriver are required; Selenium Manager may need network access if no local
driver is available. The runner does not install dependencies. No headed
fallback is used. Test fixtures use local disposable databases and fake camera
and ESP32 services; they do not connect to production Supabase or hardware.

The report records observed assertion outcomes. Setup errors are BLOCKED,
pytest skips are SKIPPED, and failed assertions remain FAIL. A rerun replaces
the consolidated report and same-ID screenshots with the newest execution;
timestamped fixture diagnostics remain in the evidence directory.
