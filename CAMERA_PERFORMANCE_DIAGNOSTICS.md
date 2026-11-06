# Supervised camera performance measurement

This patch measures performance; it does not fix lag or change camera/model,
quality, passage, serial, or servo decisions. Diagnostics are **off by default**.
No camera, Flask server, model inference, or hardware was started during tests.

## Enable for one supervised long session

Stop the existing application normally. In the project PowerShell terminal:

```powershell
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
$env:CAMERA_PERFORMANCE_INTERVAL_SECONDS="5"
.\.venv\Scripts\python.exe app.py
```

Start Sorting Session through the existing UI only when ready to supervise the
prototype. **Normal legacy servo actions remain active during normal sorting.**
The performance flag does not activate experimental scheduling, disable hardware,
or alter any other diagnostic/export flags. Do not launch a second app instance.
The process reads the new flags at startup; restarting only the camera does not
reload environment configuration. The actual `.env` was not edited.

Summaries appear approximately every five seconds in:

```text
%TEMP%\eggsort-camera-performance\performance-<UTC date/time>-<unique ID>.jsonl
```

A new file is used for each camera start. This is direct Python JSONL output,
independent of PowerShell formatting. No images, credentials, environment dump,
session identity, detection contents, or personal data are exported. Normal stop
signals a final background summary without waiting on disk in the camera lock.
Abrupt process termination can lose the last interval. Files are not automatically
deleted; disk usage grows with test duration, while in-memory aggregation remains
constant in size. There is no per-frame logging or export queue.

In a second PowerShell terminal, follow the newest file:

```powershell
$perf = Get-ChildItem "$env:TEMP\eggsort-camera-performance\performance-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $perf.FullName -Tail 2 -Wait
```

Keep the session running long enough to cover the previously observed failure
period (at least 30 minutes if safe to supervise). Record the wall-clock time of
visible lag/missed eggs and any camera restart. Compare early, middle, and late
windows using `utc`. Keep other diagnostic flags consistent between comparisons;
existing passage diagnostics continue independently; experimental exports are excluded from this release.

## Read the summaries

- `camera.requested`: unchanged requested width/height/FPS. `camera.width`,
  `height`, `fps` are driver readbacks, potentially unavailable or inaccurate.
  `frame_width`, `frame_height`, and `requested_resolution_matches_frame` use
  actual returned image shape. A false match confirms resolution mismatch.
  Readback FPS is a negotiated/advisory value, not measured delivered throughput.
- `timings_ms`: count, mean, and maximum for **completed** operations in this
  window: `read_ms`, `annotation_ms`, `jpeg_encode_ms`, `model_load_ms`,
  `inference_lock_wait_ms`, `prediction_ms`, `result_conversion_ms`,
  `detection_total_ms`, `frame_age_at_result_ms`, `passage_and_callbacks_ms`,
  and `inference_cycle_ms`. Prediction includes the existing model.predict call;
  conversion includes the existing CPU result conversion. These are host wall
  durations, not CUDA kernel profiling; no extra GPU synchronization is introduced.
- High lock wait suggests competing inference calls. High prediction/conversion
  with low lock wait localizes model/result work. High passage/callback time
  points to work after detection, including existing diagnostic/reject callbacks;
  this does not isolate which callback blocked. Existing synchronous passage
  diagnostic writes are unchanged by this patch.
- `frame_age_at_result_ms` spans **OpenCV read completion to publication of the
  inference result**, including handoff/copy, prediction and camera-lock waits.
  It excludes sensor exposure/driver-buffer age, which this code cannot measure.
- `rates_per_s.captures`, `jpegs`, `predictions`, and `inference_cycles` are actual
  completed counts divided by the summary window duration, not the reciprocal of
  one prediction. `skipped_raw_frames` counts frames intentionally superseded in
  the existing single latest-frame slot, not a growing queue or proof of missed eggs.
  A startup window includes camera negotiation/model warmup; compare sustained windows.
- `workers` gives current stage and time spent in it, even before a slow operation
  returns. Combine a growing stage age, zero completion throughput, and
  `gauges.capture_alive`/`inference_alive` to distinguish a blocked live worker
  from an exited thread. `detect_image_including_lock_wait` cannot distinguish an
  in-progress lock wait from prediction until that call returns.
- `gauges` gives approximate lock-free snapshots of latest-frame pending work,
  sequence progress and bounded quality queues/history. `passage_samples` is the current passage's existing sample list;
  the monitor does not retain samples. No application/database backlog, GPU
  utilization, or driver queue depth is measured.

## Resource and internal YOLO stage measurements

The same default-off `CAMERA_PERFORMANCE_DIAGNOSTICS` flag now enables these
additive fields; no extra flag, model/thread changes, or package installation is
needed. The prototype already has `psutil`. If unavailable elsewhere, resource
records say `available=false`; camera and timing diagnostics continue.

`resources` is sampled once per summary on the existing background thread, outside
camera, inference and telemetry locks. Its `monotonic_s` aligns with the summary
timestamp (sample is taken shortly afterward); `sample_interval_s` gives its real
interval. Counter baselines are primed on that thread before the first interval.
No subprocess, WMI, per-frame OS polling, sensor probing or unbounded history is
introduced. Each unavailable/denied/nonfinite metric is null, not a false zero.

- `process_cpu_percent_one_core`: 100 means one logical CPU's worth of work; a
  multithreaded process can exceed 100. `process_cpu_percent_machine` divides by
  `logical_cpus` for comparison with total machine capacity.
- `process_private_bytes`: Windows private allocation from psutil.memory_info;
  `process_rss_bytes`: resident working set. These measure different things.
  Private bytes stay null on platforms without that inexpensive field; RSS is
  never substituted as private memory. `process_threads` is total process threads.
- `system_cpu_percent`, `system_available_bytes`, `system_memory_percent`,
  `swap_used_bytes` and `swap_percent` provide resource-pressure context. Swap
  figures use psutil/OS definitions; allocation is not proof of active paging.
- `process_page_faults` and `process_page_faults_per_s` use the Windows total
  fault counter where available. They include soft faults and do not measure
  hard disk page reads. First rate is null. Windows swap-in/out byte counters
  are deliberately null because psutil's Windows values are not meaningful.
- `reported_clock_mhz` and `reported_max_clock_mhz` come from psutil.cpu_freq.
  On Windows, `clock_kind=reported_nominal_not_effective`: this value is not a
  reliable thermal-throttling measurement. `effective_clock_mhz` and
  `temperature_c` are explicitly null; no portable reliable sensor source is
  available in this lightweight monitor. Correlate those separately if needed.

`inference_runtime` contains actual `device`, `intraop_threads`, `interop_threads`
and `sampled_monotonic_s`. They are read from the active predictor and PyTorch
getters on the inference thread after a completed prediction, at most once per
summary interval. During an ongoing stall the last snapshot remains present;
the timestamp reveals its age. There are no thread setters, model imports for
monitoring, or additional device synchronization. Metadata remains empty until a
prediction completes; unsupported getters produce null.

Additional `timings_ms` aggregates contain `yolo_preprocess_ms`,
`yolo_inference_ms`, `yolo_postprocess_ms`, `yolo_stages_total_ms`, and
`prediction_minus_yolo_stages_ms`. These copy Results.speed for each already
completed single-image prediction; missing/invalid stages are omitted. The signed
residual is outer model.predict wall time minus the three stage timings for that
same prediction. It can include source setup, callbacks, initial warmup and other
unprofiled work, and can be slightly negative because the profiled boundaries
differ. It is not automatically a measure of CPU scheduling or a proven leak.
Existing `prediction_ms` and classification results retain their meaning.

Interpret correlated changes: high internal inference time localizes model work;
high preprocessing/postprocessing localizes those stages; a large residual
localizes unprofiled prediction work. Rising private memory plus falling available
memory and fault activity supports investigating pressure, without proving a
leak. Rising system CPU and process thread contention supports a separate
thread-count experiment, not an automatic thread-setting change. Nominal clock
readings cannot confirm or rule out thermal/power throttling.

### Initial supervised run without eggs

Stop the current app normally. Keep the physical inspection and sorting path
empty, with no eggs on the load cell. From the project PowerShell terminal:

```powershell
$env:CAMERA_CAPTURE_PROFILE="1080p_yuy2"
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
$env:CAMERA_PERFORMANCE_INTERVAL_SECONDS="5"
.\.venv\Scripts\python.exe app.py
```

Start the normal Sorting Session only when prepared to supervise it. **This is
not a hardware-isolation mode:** the existing controller initialization, rollers,
and legacy servo behavior remain enabled. Do not feed eggs initially. If YOLO
reports a false defect on the empty scene, normal rejection behavior can still
occur. This patch does not change or suppress it.

Observe 10-15 minutes without feeding eggs, spanning the previous six-minute
failure point. Check actual 1080p/YUY2 verification, record the time of any lag,
and compare early/stalled `resources`, `inference_runtime`, internal stages and
outer prediction duration in the same JSONL. If no lag occurs, an empty scene
alone has not validated loaded sorting; a separate supervised egg trial remains
necessary. Stop normally to obtain the final summary. Disable the performance
flag and restart Python to return to diagnostics-off operation. The actual .env
and the working camera profile's negotiation/settings are not edited.

Resource semantics reference: https://psutil.readthedocs.io/stable/index.html
Hardware-free additions:

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_camera_resources test_camera_performance -q
```

If the writer fails, one generic warning is emitted and that run's writer stops;
camera decisions continue. The telemetry mutex protects only small primitive
aggregates. Summary formatting, filesystem operations and queue inspection happen
on a separate daemon thread outside camera and inference locks. No frames are
copied by the monitor. An unhealthy/blocked diagnostic disk cannot create an
unbounded per-frame memory queue.

## Disable

Stop the application normally, then run:

```powershell
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="false"
Remove-Item Env:CAMERA_PERFORMANCE_INTERVAL_SECONDS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe app.py
```

An explicit `false` overrides an enabled value in `.env`. For the default-off
behavior, remove the variable instead if `.env` does not enable it. Existing
passage diagnostics use their own unchanged flags. Experimental observation exports are not part of this release.

Hardware-free regression command:

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_camera_performance test_camera_exit test_camera_capture test_camera_diagnostics test_esp32_bridge -q
```
