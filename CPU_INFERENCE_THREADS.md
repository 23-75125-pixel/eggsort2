# Controlled CPU inference thread experiment

This is an actual intra-op scheduling change, not another timing-only feature.
`EGGSORT_CPU_INFERENCE_THREADS=4` limits PyTorch intra-op CPU work to four threads.
Default is **0/unset**, preserving the original inference initialization and
callbacks. Values 1-64 enable the experiment; 0 disables it. Restart Python to
apply or revert; changing an environment variable in an already running process
or restarting only the camera cannot unload the cached model/thread policy.

## Initialization and verification

The installed Ultralytics 8.4.164 CPU select_device path explicitly runs
`torch.set_num_threads(NUM_THREADS)` during predictor setup. Setting only PyTorch
or OMP_NUM_THREADS beforehand can therefore be overwritten.

When enabled, before constructing YOLO in detection_service._load_model, the
policy sets `ultralytics.utils.torch_utils.NUM_THREADS` **in this process's memory**
to the requested count and calls `torch.set_num_threads()` once. No vendor file,
PyTorch package, model weight or training configuration is edited. Subsequent
Ultralytics CPU predictor initialization/recreation also uses four instead of its
default value. The policy is process-wide for CPU selectors in this application;
it is not a per-model isolation mechanism. This application performs inference,
not training. No inter-op setting, affinity, priority, or GPU selection is changed.

Read-only verification callbacks run at `on_predict_start` (after predictor/model
setup and device selection, before preprocessing/warmup/forward) and
`on_predict_batch_end`. They check the actual inference worker's
`torch.get_num_threads()` on CPU. They do **not** set thread counts per frame. A
mismatch logs an error and fails the camera's existing inference-error path;
it is not silently corrected or reported as a successful experiment. Initialization
and the first verified CPU callback on each worker log INFO messages. GPU
predictors are not CPU-verified and no GPU device is forced.

Existing performance summaries provide independent low-frequency worker snapshots:
`inference_runtime.configured_intraop_threads=4`, `intraop_threads=4`,
`thread_limit_verified=true`, and `device=cpu`. `interop_threads` is measured only
and remains unchanged. Total OS process threads can exceed four because camera,
Flask, monitoring and runtime helper threads still exist.

The prediction call's source/conf/imgsz/verbose arguments and all classification,
preprocessing, NMS, camera and passage decisions remain unchanged. Callback
registration occurs once with the cached model. With the option disabled no
policy import, thread setter, selector override or callback registration occurs.

## Enable for a supervised run

Stop the existing Python application normally. In the project PowerShell terminal:

```powershell
$env:EGGSORT_CPU_INFERENCE_THREADS="4"
$env:CAMERA_CAPTURE_PROFILE="1080p_yuy2"
$env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
$env:CAMERA_PERFORMANCE_INTERVAL_SECONDS="5"
.\.venv\Scripts\python.exe app.py
```

Do not start another app instance. Start Sorting Session when ready to supervise
normal legacy hardware operation. Begin with the path and load cell empty, no
eggs fed. This is not hardware isolation: initialization, rollers and legacy
servo behavior remain active. Verify the first successful summaries:

```powershell
$p = Get-ChildItem "$env:TEMP\eggsort-camera-performance\performance-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$r = Get-Content -LiteralPath $p.FullName -Tail 1 | ConvertFrom-Json
$r.inference_runtime | ConvertTo-Json
$r.camera | ConvertTo-Json -Depth 5
```

Require configured/actual intra-op counts of four, verification true, CPU device,
and actual 1920x1080 YUY2 mode verified. The runtime snapshot remains empty until
the first prediction finishes. A stopped camera with a thread-limit error is a
failed experiment; review the message instead of feeding eggs or bypassing it.

Collect 10-15 minutes, spanning the previous 4-6-minute failure point. Compare
prediction/internal inference duration, annotation, frame age, CPU utilization,
private memory, paging indicators and thread count with the previous baseline.
Lower thread count may lower initial FPS or change CPU/power usage; record that
tradeoff rather than claiming improvement from startup FPS alone. If the empty
run passes, perform a separate supervised representative-egg trial. Classification
and hardware behavior require physical validation even with unchanged rules.

## Disable / rollback

Stop Python normally and restart from the same terminal:

```powershell
$env:EGGSORT_CPU_INFERENCE_THREADS="0"
.\.venv\Scripts\python.exe app.py
```

This disables only the thread experiment while keeping your current camera and
diagnostic flags. The new process uses the original Ultralytics thread behavior.
Alternatively remove `Env:EGGSORT_CPU_INFERENCE_THREADS` if `.env` does not enable
it. Explicit 0 overrides an enabled `.env` value. The actual `.env` was not edited.

No servo timer/movement/retrigger, Auto Capture, quality, Egg Exit, calibration,
size sorting, database, serial protocol, or experimental scheduler changes are
part of this experiment. It is not a confirmed resolution of long-session lag.

Hardware-free policy tests:

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_cpu_inference_threads -q
```

PyTorch API reference:
https://docs.pytorch.org/docs/stable/generated/torch.set_num_threads.html
