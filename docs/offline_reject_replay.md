# Offline C++ reject scheduler loopback

This PC-only tool reuses `reject_gate_protocol.h`, `reject_gate_scheduler.h`,
`ExperimentalRejectClient`, and `load_observation_export()`. It does not import
Flask, open cameras/COM ports, or compile/upload an ESP32 image. No live default
is changed. `EGGSORT_EXPERIMENTAL_REJECT` remains 0.

Ubuntu WSL g++ builds an ordinary Linux executable. Windows Python starts it
through `wsl.exe -d Ubuntu`, exchanging anonymous stdin/stdout messages, without
shell evaluation or device transport. On Linux the CLI uses native g++. Neither
path installs software. The build also compiles all 23 existing static assertions.

## Project-root PowerShell commands

Build only (not firmware):

```powershell
.\.venv\Scripts\python.exe -B offline_reject_replay.py --build-only
```

Replay a completed export with UNKNOWN physical bounds (no opening):

```powershell
.\.venv\Scripts\python.exe -B offline_reject_replay.py 'offline_tests/reject_observations/YOUR-FILE.jsonl' --no-build
```

Synthetic default-blocked demonstration:

```powershell
.\.venv\Scripts\python.exe -B offline_reject_replay.py --demo crack-crack --no-build
```

Explicitly assumed nominal sensitivity case, **SIMULATION ONLY**:

```powershell
.\.venv\Scripts\python.exe -B offline_reject_replay.py --demo crack-crack --no-build --assume-arrival-ms 4510 4860 --assume-route-clear --assume-clearance-ms 0 --max-source-age-ms 100 --controller-origin-ms 1010 --output offline_tests/reject_replay/build/crack-crack-report.json
```

Other demos: `crack-rotten`, `rotten-crack`, `crack-good`, `good-crack`.
Replace `--demo ...` with a JSONL path to replay recorded decisions. Reports print
as JSON unless `--output` names a NEW file; existing files are never overwritten.
Build artifacts/reports under `build/` are Git-ignored. `--no-build` reuses the
previous executable; rebuild after source changes.

Sensitivity options: `--latency-ms 100` (one-way transport), `--queue-delay-ms 20`
(before transmission), `--retry-after-ms 100`, `--guard-ms 100`,
`--assume-clearance-ms 100` (EXTRA dwell after closing), `--tick-ms 10`
(default 1), and `--controller-origin-ms 4294965295` (rollover).

All physical inputs are assumptions, never approvals. Arrival bounds are relative
to CONTROLLER ACCEPTANCE. The old 4510/4860 phone measurements were relative to
DETECTION: they cannot simply be reused as verified receiver-relative bounds
when transport/source delay exists. Current camera settings and the old nominal
350 ms allowance establish neither clearance nor these bounds. Hold remains
6500 ms, closing 450 ms, opening 180 ms.

## Actual loopback and limitations

Complete contiguous single-run exports are validated before replay. Observation
ordinals become protocol IDs; original capture IDs, run identity, first-defect
monotonic timestamps and final labels remain in the report. The ordinal prevents
short-Good camera-ID reuse from causing collisions.

Only `first_defect` creates a defective request. Good is submitted at its existing
quality lock or exit, never backdated to provisional passage-start Good. The
report warns that preceding Good occupancy before that notice is UNKNOWN; a late
Good notice cannot prove earlier motion was safe. Exports do not snapshot camera
zone settings: record those separately rather than attributing today's settings
to an older file.

Host intervals map onto an explicitly simulated controller clock. Actual tagged
frames pass through the existing C++ parser and `submit()`. Each service instant
calls the real `tick()`; commands precede the tick at that instant. Replies use
real C++ statuses/IDs/deadlines and return to the Python `accept_ack()` handler.
The tool never invents successful acceptance. Its trace records whether each
response was accepted; terminal/stale replies can be ignored by the handler
without hiding the actual C++ result.

First acceptance fixes `deadline = accepted_at + 6500` modulo uint32. Retries
preserve source identity/time and deadline. The frame's source age includes host
queuing; the report separately includes assumed in-flight latency. Python absolute
monotonic values are never directly compared against MCU millis(). Trace ordering
uses nonwrapping elapsed time, while the scheduler receives wrapped controller time.

Assumed-feasible defect pairs receive separate closes. `SETTLED_UNVERIFIED` is a
timed phase, not verified physical release. Good latches `UNSAFE_GOOD`: Good ->
Crack cannot subsequently open, while Crack -> Good preserves its active close
but reports unsafe routing. Unknown timing/clearance, spacing conflicts, stale
sources, overflow and expiry remain blocked. Host four-outstanding and core
one-active/four-pending limits differ; tests explicitly inject controller-only
stress frames to reach core overflow, never through the live bridge.

## Tests

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_offline_reject_replay test_experimental_reject_export test_experimental_reject_observer test_experimental_reject_client test_camera_exit test_camera_capture test_camera_diagnostics test_esp32_bridge -q
```

Tests require a compiler and fail clearly rather than silently skipping native
checks. They compile in a temporary directory and exercise five pairs, movement
boundaries, retries, conflicting IDs, stale boot, expiry, queue limits, late
service, unknown clearance, export roundtrip, CLI reports and rollover.

Before live integration: measure timing under the actual current camera zone,
resolve Good occupancy/clearance and feeding conflicts, review ordinal/run mapping,
then separately implement bridge handshake/ACK/retry handling and fault recovery.
No software test proves that accumulated eggs can be released separately.
