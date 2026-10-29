# Working prototype source baseline ? October 29, 2026

This release preserves the selective rollback recorded against tree
ac7b9b4f3b3da3475f182b1a4db25902e6af990d. Archived October 27 rejection,
recording and COM-fallback changes are not restored.

Working hardware source: reject hold 6500 ms, retrigger close 300 ms;
load-cell calibration 622.0, closed/open positions 305/195, post-weight delay
400 ms; existing gradual movement and stability rules; Roxas boundaries
55/60/65/70 grams and existing servo channels.

Working quality configuration (also in .env.example): zone left/right
0.25/0.75, top/bottom 0.30/0.64, quality samples 2. Egg Exit requires two missed
observations and 600 ms absence, with a 1% occupancy-only margin. Good weighing
claims do not increment exits before confirmed exit. Quality priority and
zone-center eligibility are preserved.

To reproduce the supervised camera configuration in PowerShell after stopping
any existing application:

```powershell
$env:CAMERA_CAPTURE_PROFILE="1080p_yuy2"
$env:EGGSORT_CPU_INFERENCE_THREADS="4"
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
.\.venv\Scripts\python.exe app.py
```

These are opt-in process settings. Native DirectShow YUY2 uses 1920x1080/30 and
the verified FPS/FourCC/width/height order. CPU thread count is checked after
predictor setup and during prediction. Restart Python to revert the thread flag
to 0 and profile to existing. Actual .env, credentials and model weights are not
included: use your existing privately supplied best.pt. See the profile and CPU
thread documentation for verification and rollback.

The supervised run from approximately 16:35:29?16:45:48 Manila time had four
intra-op threads, verified 1080p YUY2 and no repeated multi-second stalls after
startup. Late prediction time increased under higher system CPU load. This is
promising test evidence, not proof that long-session lag is permanently fixed.

Normal Sorting Sessions still operate legacy hardware. No firmware upload,
database migration or experimental rejection activation is part of this release.
Experimental observer/scheduler/protocol/replay files remain local and are not
runtime dependencies of the committed camera source. Approved centered,
1500-ms Restart/Stop success modals are included without endpoint changes.

## Release verification

The exact staged source export passed 150 focused Python camera/quality/exit,
CPU policy, resource, bridge, size, lifecycle and validation tests; 16 focused
JavaScript modal/security/alert-flow tests passed. Arduino CLI 1.5.1 with ESP32
core 3.3.12 compiled the staged sketch (318455 program bytes, 24560 global RAM
bytes), including four stable-weight static assertions. No hardware was operated.

Two unchanged legacy suite limitations remain outside this release's scope:
`test_config` expects automatic startup true although the existing application
default is false; the broad notification template test references sales.html,
which is already absent from HEAD. Those unrelated tests/source were not edited
to conceal the failures. Focused Sorting Sessions inline-script and success-modal
checks are included separately. Full application database integration suites also
retain the existing PostgreSQL/SQLite compatibility limitation noted during
development; no database migration or production database operation was performed.
