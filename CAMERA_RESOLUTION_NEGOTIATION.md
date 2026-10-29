# Reversible DirectShow negotiation correction

The recorded run requested 1280x720/30 but returned 2560x1440. The old order was
MJPG, width, height, FPS, buffer size. OpenCV's DirectShow FPS setter may stop and
recreate the device stream, and size/format setters can also renegotiate the media
type. Setting FPS last can invalidate an earlier format selection. This is a
source-supported explanation, not proof of this particular driver's failure:
https://github.com/opencv/opencv/blob/4.x/modules/videoio/src/cap_dshow.cpp
(VideoCapture_DShow::setProperty).

With `CAMERA_RESOLUTION_NEGOTIATION_FIX=true` (default), **explicit dshow only**
requests FPS, width, height, MJPG, buffer size. The requested values and camera
backend are unchanged. No retries, backend switches, extra reads, frame resizing,
model arguments, exposure/focus settings, or hardware actions are added.
Other backends retain their old property order.

Every property setter's Boolean result and advisory width/height/FPS/FourCC/backend
readbacks are recorded. The first successful read verifies actual image shape;
any later shape change is checked again. A mismatch emits a warning and is
explicitly marked false in `camera_negotiation` in existing camera status and in
performance JSONL `camera` records when performance diagnostics are enabled.
Capture continues using the real frame, preserving prior failure behavior; a
running camera is **not** a claim that resolution negotiation succeeded. A
successful first-frame check logs at INFO level (visibility depends on application
logging configuration). FPS readback does not establish delivered FPS; use the
performance summary throughput. Unknown readbacks are null.

## Supervised verification (not executed during implementation)

1. Stop the current application normally. In its project PowerShell terminal:

   ```powershell
   $env:CAMERA_RESOLUTION_NEGOTIATION_FIX="true"
   $env:CAMERA_PERFORMANCE_DIAGNOSTICS="true"
   .\.venv\Scripts\python.exe app.py
   ```

2. Start the existing Sorting Session only when prepared to supervise normal
   legacy hardware operation. Do not start a second application/camera instance.
3. Inspect the newest `%TEMP%\eggsort-camera-performance\performance-*.jsonl`:

   ```powershell
   $p = Get-ChildItem "$env:TEMP\eggsort-camera-performance\performance-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
   (Get-Content -LiteralPath $p.FullName -Tail 1 | ConvertFrom-Json).camera | ConvertTo-Json -Depth 5
   ```

   Require actual `frame_width=1280`, `frame_height=720`,
   `resolution_verified=true`, and `requested_resolution_matches_frame=true`.
   Readback/`set_accepted` alone is insufficient. If it still returns 1440p, the
   candidate correction did not work: stop and review reported capabilities,
   rather than treating it as fixed or automatically switching backends.
4. Check framing, zone alignment, brightness, sharpness, and representative
   Good/Crack/Rotten appearance before resuming a long test. A real 720p mode may
   change sensor crop/field of view, pixel detail, or camera processing even with
   the same aspect ratio. Normalized zone coordinates stay unchanged, but may
   correspond to different physical positions. Do not infer equal classification
   behavior merely from equal aspect ratio.
5. If appearance is acceptable, collect a comparable supervised 10-15 minute run
   with existing performance diagnostics. Compare late prediction/frame-age and
   capture rates. This correction does not establish that lag is resolved.

## Rollback

Stop the application normally, then in the startup terminal:

```powershell
$env:CAMERA_RESOLUTION_NEGOTIATION_FIX="false"
.\.venv\Scripts\python.exe app.py
```

This restores the exact old property-setting order while retaining truthful
verification/reporting. Restart Python; camera-only restart does not reload the
flag. The actual `.env` file is not modified. Remove the PowerShell variable to
use the default corrected order again, unless `.env` explicitly overrides it.

No physical camera negotiation can be verified by mocks. Servo timing, rejection,
zone coordinates, quality rules, Egg Exit, calibration, serial protocol, and the
disabled experimental scheduler are unchanged.
