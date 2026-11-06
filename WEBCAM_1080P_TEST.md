# Standalone UGREEN 1080p/30 evaluation

Nothing in this tool imports Flask, EGGSor+ camera/session modules, YOLO, serial,
dotenv, or a database. It never writes live settings. Importing the module or
running `--help` does not open a camera. Only explicitly running the test opens it.

Stop the Flask application normally and close other webcam apps first. Have a
representative egg/test target positioned in the field of view if you want the
saved sample to show it; the probe has no preview or detection.

From the project directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -B .\webcam_1080p_probe.py --index 0 --backend dshow --seconds 20
```

The tool opens one camera at a time and tries MJPG first, then explicitly labeled
YUY2 candidates if MJPG does not pass. Each format gets two separate attempts:
FPS -> FourCC -> width -> height, then FPS -> width -> height -> FourCC. Each
attempt uses a fresh capture handle; no backend switch is automatic. It rejects
the first wrong-size or wrong-FourCC frame and never resizes/crops/pads frames.
All tests request 1920x1080 at 30 FPS. Optional buffer settings are not changed.

For each candidate it records property acceptance/readbacks after every setting,
actual image dimensions and FourCC readback after every read, plus delivered FPS
over at least 20 seconds after a two-second warmup. Read failures, unknown FourCC
and fallback modes are failures. `FPS_MISMATCH` preserves the actual measurement;
`PASS` requires matching resolution/format and delivered FPS between 29 and 31
(explicit screening tolerance, not proof of exact native 30 FPS or unique frames).
No per-frame console/disk writes, preview rendering or inference distort this
throughput measurement. A single original unannotated PNG is written afterward
for valid-resolution/format candidates, including those with an FPS mismatch.

Outputs are printed and stored in:

```text
%TEMP%\eggsort-webcam-probes\<UTC timestamp>\report.json
%TEMP%\eggsort-webcam-probes\<UTC timestamp>\<attempt>-<format>.png
```

The report states exactly which mode/order passed. A YUY2 pass is never reported
as MJPG support. All candidates failing exits with code 1; success exits 0;
Ctrl+C exits 130. A native driver read can block and delay Ctrl+C, because OpenCV
does not offer a portable DirectShow read timeout. The camera is released on
ordinary completion, exceptions and interruption.

This checks **observed acceptance of targeted modes**, not the complete list of
advertised formats. OpenCV cannot enumerate all DirectShow media types. Neither
this script nor mocked tests establish the webcam's native capability catalogue
or whether its own firmware internally scales images. FourCC is backend-reported,
not independently inspected from the capture pin. Full catalogue verification
would require a separately approved DirectShow GetStreamCaps/capture-pin tool.
FFmpeg was not found on the current PATH; nothing was installed.

If DSHOW fails, a separate optional backend comparison is:

```powershell
.\.venv\Scripts\python.exe -B .\webcam_1080p_probe.py --index 0 --backend msmf --seconds 20
```

This opens the webcam when you run it. Backend indices may identify different
devices; ensure index 0 still refers to the same UGREEN webcam. Backend changes
may alter color, exposure and buffering, even when resolution matches.

## Adoption and rollback (recommendation only)

Do not adopt based on setters or nominal FPS alone. Inspect report and image,
repeat a passing candidate, then review its exact winning backend/format/order.
If approved afterward, the minimal live change is a timestamped external `.env`
backup, temporary CAMERA_WIDTH=1920/CAMERA_HEIGHT=1080/CAMERA_FPS=30 overrides,
and the exact proven negotiation profile if it differs from the live order.
The first tested order is **not** the currently corrected live order: copying
only width/height settings does not necessarily reproduce a pass. No such live
changes or profile wiring are implemented here.

Current baseline is requested 1280x720/30, corrected DirectShow order, observed
2560x1440 YUY2. That distinction matters: rollback means restoring the existing
requested settings and order, not inventing a new explicit 1440p profile. They
remain unchanged by this standalone tool. Actual driver persistence after a test
is not guaranteed: restart the live app and verify its returned shape/FourCC
before feeding eggs, even though its configuration files are unchanged.

Compare 1080p with the previously saved 1440p baseline at the same physical camera
position, lighting and egg orientation. Check visible crack detail, candling,
blur during motion, contrast, framing and normalized-zone alignment. Reduced
resolution may change sensor crop/field of view and detail. Preserve the model,
confidence, image size, classification and zone rules for any later offline YOLO
comparison. Use several Good/Crack/Rotten samples in both modes and distinguish
image-clarity differences from performance. The probe's delivered FPS has **no
YOLO workload**; it does not predict long-session inference performance. After
explicit adoption approval, validate a supervised 10-15-minute run with existing
performance diagnostics, comparing prediction-only timing, frame age, capture
throughput and late stalls. Normal sorting would still operate legacy hardware.

No servo timing, calibration, serial protocol, or experimental activation changes
are involved. Passing this probe does not establish that the camera lag is fixed.
