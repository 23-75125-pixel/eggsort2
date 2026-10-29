# Opt-in verified UGREEN 1080p YUY2 profile

The standalone physical probe reported DirectShow, 1920x1080 YUY2, 601 frames
over 20 seconds, and approximately 29.99755 measured FPS. This integration is
prepared for supervised validation; it has not been run on hardware by the agent.

## Enable temporarily

Stop the existing application normally. In the project PowerShell terminal:

```powershell
$env:CAMERA_CAPTURE_PROFILE="1080p_yuy2"
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
.\.venv\Scripts\python.exe app.py
```

This explicit profile selects DSHOW, 1920x1080, 30 FPS and YUY2 regardless of the
generic CAMERA_BACKEND/WIDTH/HEIGHT/FPS settings or the older negotiation-fix
flag. It uses exactly four setters, in the successful probe's order:
**FPS -> FourCC -> Width -> Height**. It does not add a buffer-size setting,
change the camera index, resize images, change model arguments, or touch `.env`.
Default profile is `existing`; no live mode is enabled by this patch itself.
Restart the Python application to reload the profile; camera-only restart does
not reload environment settings. Never start a second application instance.

Start the existing Sorting Session only when ready to supervise normal physical
operation. The existing legacy reject and load-cell mechanisms remain active.
Before feeding eggs, inspect the newest performance JSONL:

```powershell
$p = Get-ChildItem "$env:TEMP\eggsort-camera-performance\performance-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
(Get-Content -LiteralPath $p.FullName -Tail 1 | ConvertFrom-Json).camera | ConvertTo-Json -Depth 6
```

Require `profile=1080p_yuy2`, `frame_width=1920`, `frame_height=1080`,
`fourcc=YUY2`, `requested_fourcc_matches=true`, and `mode_verified=true`.
The existing camera status also includes `camera_negotiation`.

Actual image shape and fresh backend FourCC readback are checked after the first
read, then approximately every five seconds and immediately on a shape change.
Readback is advisory; it does not independently inspect the physical capture pin.
A missing/mismatched FourCC or resolution in this opt-in profile emits a warning,
fails camera startup (or the running camera), and stops capture **before handing
the offending frame to inference**. No automatic resolution/backend fallback or
resize is accepted. Use the existing lifecycle recovery procedure before restarting.
The old profile retains its previous warn-and-continue mismatch behavior.

Thirty FPS remains a request, not a startup guarantee. Compare actual
`rates_per_s.captures`/`jpegs` over sustained windows; these rates include the
existing annotation/encoding workload, unlike the standalone probe. Prediction
throughput will be lower than capture throughput. All existing performance,
passage diagnostics remain available independently. Experimental observation exports are excluded from this release.

Check framing, candling, blur and visible cracks on representative Good, Crack
and Rotten eggs before a longer trial. Even with unchanged normalized zone
coordinates, a sensor-mode crop or changed sharpness/color can affect the physical
inspection area and classification. Collect at least 10-15 minutes of supervised
performance summaries and compare late prediction-only duration and frame age
against the recorded 1440p run. Do not infer that lag is fixed from resolution alone.

## Revert

Stop the app normally, then:

```powershell
$env:CAMERA_CAPTURE_PROFILE="existing"
.\.venv\Scripts\python.exe app.py
```

This removes the profile overrides and restores the original generic camera
settings/order controlled by CAMERA_RESOLUTION_NEGOTIATION_FIX. Do not change
that flag or generic camera variables if the goal is the immediately preceding
baseline. The prior baseline requested 1280x720/30 but delivered 2560x1440 YUY2;
rollback preserves those requests and behavior, not a guarantee of driver state.
Verify returned dimensions/FourCC after restarting. No `.env` backup is needed
for these process-only overrides because this patch does not edit the actual file.

Protected servo hold/retrigger/movement, Auto Capture, quality rules, Egg Exit,
calibration, size sorting, serial protocol, and disabled experimental scheduler
are unchanged. No upload, deployment, commit, or push is involved.
