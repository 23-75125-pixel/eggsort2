# Experimental reject scheduler — NOT approved for physical activation

This is code preparation, not evidence that consecutive eggs can be rejected
separately. The operator reports that the sloped bin receives the egg DURING the
450 ms closing/pushing movement, with transfer complete by full closure in the
observed operation. This supports overlapping transfer and closure, not an
additional full transfer interval after closure. A worst-case extra-clearance
bound across eggs and operating conditions remains unverified. The existing 6500 ms hold,
300 ms legacy retrigger, servo positions, calibration, load-cell operation,
size routing, camera counting, database, and frontend remain unchanged in the
default build.

## Default-off isolation

`EGGSORT_EXPERIMENTAL_REJECT` is **0** in `eggsort_controller.ino`. The default
firmware runs its original reject functions. The tagged experiment is excluded.
`ExperimentalRejectClient` also defaults to disabled. The camera's separate
observation module imports its definition but does not instantiate it during
camera operation. It builds protocol frames only:
it never opens COM ports, sends commands, starts threads, or reacts to an ACK by
sending another request. Only an explicit offline replay creates an enabled
client with synthetic boot/run acknowledgments. Physical transport integration
is deliberately deferred. Do not upload a test build.

Even an experimental build leaves arrival bounds, initial normal-route
clearance, and extra egg clearance UNKNOWN. Its default configuration cannot
open the servo. Before approval, do not replace those unknowns with guessed
safe values. The 450 ms close and 180 ms open values are measured nominal
movement times, not worst-case bounds. The existing 300 ms interval is never
used by the experimental closing/release state.

## State machine and fixed deadlines

All core times use controller `uint32_t millis()` and rollover-safe comparisons.
On the first valid controller acceptance, `deadline = accepted_at + 6500`.
That immutable deadline remains associated with the run/capture ID while queued,
active, and cached. Opening a queued job does not start another full hold.

`CLOSED -> OPENING -> HOLDING -> CLOSING_RELEASE -> CLOSED_CLEARANCE -> CLOSED`.
`tick()` returns at most one motor action and never blocks. A new request never
closes an active gate or changes its deadline. A late service tick closes an
already-open active gate and reports the missed deadline; it does not pretend
that the close command happened earlier. Future work is inhibited after a
latched conflict. Pending jobs that reach their deadline expire without opening.

Closing waits at least 450 ms from the actual CLOSE action. Reopening is gated
by separately validated additional clearance/dwell, if required BEYOND the
closing movement. Do not count the same observed pushing/transfer time twice.
Some observed eggs may require no extra dwell, while other eggs, bounce-back,
rolling, or loaded servo variation may require more. `extra_clearance_ms = 0`
with `clearance_verified = false` means UNKNOWN, not safe zero clearance.
`SETTLED_UNVERIFIED` reports only a timed mechanical phase; it does not confirm
that an egg reached the Reject Bin.

The firmware FIFO holds four pending jobs plus one active job. The host utility
limits all outstanding requests to four. Overflow, stale requests, deadline
conflicts, insufficient spacing, and unknown Good routing are explicit errors.
Faults preserve the active close/release movement, inhibit subsequent openings,
and cannot stop the continuously moving rollers. Operator/mechanical control is
still necessary when spacing is insufficient. No late queue is automatically
replayed after a fault or reboot.

## IDs, clocks, retries, and acknowledgments

Use the existing camera passage `capture_id`, scoped by a fresh nonzero 64-bit
run token. Preserve the FIRST accepted defect timestamp, not the latest YOLO
frame or delayed completed-capture timestamp. The host record keeps that Python
monotonic time and sends elapsed source age only. It never compares it with
controller millis or computes a new MCU deadline from an ACK.

The device boot token protects against old frames after reboot. One run is bound
per boot; a delayed BEGIN cannot reset deduplication. First acceptance binds the
controller deadline; retries return the cached decision and cannot refresh it.
Older IDs remain stale even after eviction from the bounded history. Duplicate
IDs with different qualities are rejected as conflicts. A late first delivery
is rejected by the validated source-age budget instead of gaining a new timing
window. Receiver-relative arrival bounds must include measured transport,
source-age, actuator, and scheduling uncertainty. Source age does not include
unmeasured in-flight USB delay; the configuration is unsafe until that delay is
bounded. Do not assume half an ACK round-trip is exact one-way latency.

Frames fit the existing 80-character input limit (no newline shown):

```
XH
XH:<8-hex boot token>                       device reply
XB:<8-hex boot token>:<16-hex run token>
XR:<8-hex boot token>:<16-hex run token>:<capture ID>:C|R|G:<source age ms>
XA:<8-hex boot token>:<16-hex run token>:<capture ID>:<MCU deadline>:<status>
```

The client ignores unknown, stale-boot/run, conflicting-deadline, and unsolicited
ACKs. Terminal requests cannot be revived by an older ACK. Deadline 0 is valid
after rollover. No legacy `REJECT:` fallback is allowed in experimental mode;
legacy `CRACK_TEST` is also blocked there to prevent bypassing the scheduler.

## Good eggs and spacing

A Good notice latches `UNSAFE_GOOD`; no clearance timer is invented. A preceding
Good egg prevents opening for a subsequent Crack/Rotten egg. A Good notice during
an active rejection reports unverified routing while preserving that rejection's
existing deadline. This is deliberately conservative, not a physical Good-routing
solution. There is no automatic Good-clearance command or recovery path yet.

Future physical integration must supply all relevant passage observations in order,
using existing classification policy, or supply independently verified empty-route
evidence. The current physical command path sends only defect commands; wiring the new client
only to those commands would not protect Good -> Crack and is NOT sufficient for
activation. Changing the quality of an already-submitted ID is currently a conflict;
classification/observation integration must be reviewed before activation.

For the measured pair (ideal zero transport/extra clearance), close deadlines
are 7510 and 10480 ms, first full closure 7960 ms, second full opening 8140 ms,
and second arrival 8490 ms. Only 350 ms remains for all additional allowances.
The compile-time model explicitly assumes clearance solely for simulation.
It does not approve that assumption for the prototype.

### Updated sloped-bin close/reopen review

The physical observation makes the nominal sequence more plausible: first
close/push at 7.51 s, full closure/observed transfer by 7.96 s, reopen command
at 7.96 s plus any extra dwell, fully open at 8.14 s plus that dwell, and next
arrival at 8.49 s. The next egg retains its 10.48 s close deadline. The old
300 ms reversal still cannot complete a measured 450 ms close.

Let E be extra dwell after full closure and B be the total timing allowance
for command/supervision delays, movement variation, earlier arrival, measurement
uncertainty, and reserve. The nominal pair requires E + B <= 350 ms. Equality
leaves no unaccounted headroom; it is not blanket approval. The corresponding
nominal detection spacing is at least 2.62 s + E + B using the second egg's
4.51 s travel. Receiver-relative arrival bounds must still be validated.

The new compile-time sensitivity cases include positive extra dwell, excess
budgets that must refuse reopening, and first-command acceptance lag. A
100 ms acceptance lag moves the first controller-based close deadline later
by 100 ms relative to the ideal phone timeline and consumes that headroom;
it must not be compensated by shortening the working 6500 ms hold.

The new observation concerns DEFECTIVE eggs pushed into the Reject Bin. It does
not establish when a Good egg clears the normal path. In Good -> Crack, the
Good egg arrives at 5.87 s, while Crack is detected at 3.98 s. The scheduler
must avoid opening into the Good egg's passage. The next defect requires a
fully open gate by 8.49 s, so a reopening command must occur no later than
8.31 s before guard allowances. Good clearance must precede that command;
any necessary preceding movement/dwell also consumes this window. The current
UNSAFE_GOOD latch intentionally remains unchanged. For Crack -> Good, no second
reject opening is needed, leaving 530 ms between nominal full closure at 7.96 s
and Good arrival at 8.49 s, subject to reset/clearance uncertainty.

## Verification and required next measurements

`reject_gate_scheduler_tests.cpp` contains compile-time simulated assertions for
single defects, all defect pairs, both Good pair orders, duplicates, overflow,
stale work, late service, unknown clearance, parsing, and millis rollover. They
generate no runtime test instances or motor commands. Python tests cover the
isolated frame/ACK client. Existing camera/bridge regressions remain applicable.

Before physical approval, validate whether ANY additional loaded egg clearance
is required after full closure over repeated eggs and conditions; measure
required dwell, timing variability, earliest arrivals, Good-egg passage/clearance,
USB/controller latency, and supervision jitter with a shared timing reference.
Set validated arrival bounds, guard and source-age budgets only from that review.
Then review live-observation wiring and a bounded, supervised rollout separately.

## Phase 1: camera observation integration (dry-run ONLY)

The live `camera -> app.py callback -> ESP32_BRIDGE.reject_egg()` path is
unchanged. `CAMERA_REJECT_DRY_RUN` defaults to **false**. To collect observations
in a supervised existing application process, set this before starting it:

```powershell
$env:CAMERA_REJECT_DRY_RUN = "true"
```

Disable by removing it or setting it to `false`, then restarting the application.
This flag enables observation collection ONLY. Normal application operation can
still send its existing legacy hardware commands; dry-run does not replace or
disable those commands. The automated tests use only mocked serial objects.
Do not enable the firmware experiment or upload firmware for this phase.

`CameraDetectionSession.experimental_reject_snapshot()` returns an immutable
tuple of observations plus a dropped-event count, under the camera lock. There
is no new endpoint, transport, or frontend change. Optional file export is
documented below and remains disabled by default.
The buffer retains 512 lifecycle events; overflow is explicit and offline replay
refuses incomplete snapshots. Without optional export, snapshots must be copied
from the existing process; do not start another application merely to retrieve them.

Events are `passage_begin`, `first_defect`, `quality_locked`, and `passage_exit`.
Defect acceptance locks the existing first-defect quality; Good locks at an
existing in-zone scale claim or qualified exit. Every confirmed exit also exposes
the final label, including a short unqualified Good passage. A provisional Good
at passage start does NOT create an offline Good job before quality lock/exit.
Nothing changes sample eligibility, classification locks, counting, claim queues,
rejection retries, or the existing camera callback signatures.

Only allowlisted metadata is copied: a fresh observation run token per camera
start, session reference, runtime generation, existing capture ID, observation
passage ordinal, ordered event sequence, label, host monotonic event time, and
first accepted defect time. No runtime-context dictionary or credentials are
copied. Reacquisition retains the same identity. A short unqualified Good can
leave the existing capture ID reusable; the additional ordinal distinguishes
these observations without altering camera capture IDs or Egg Exit behavior.
This mapping requires explicit review before any future physical protocol use.

`OfflineRejectReplay(events, dropped=dropped)` consumes the snapshot separately.
It prepares at most one job per observation passage ordinal, retains the original
capture ID in its event, includes Good passages, and records final classifications.
It creates a protocol client with synthetic boot/run ACKs exclusively in memory.
`accept(passage_id, host_send_at=..., controller_accept_ms=...,
forward_latency_ms=...)` supplies simulated acceptance in passage order.
The result contains inert frame/ACK strings, source age including explicitly
supplied one-way latency, and an immutable MCU deadline of acceptance + 6500 ms
(including rollover). Reaccepting the same passage returns its existing result.
Stale ACKs cannot create jobs. There is no writer or bridge reference to send
those strings anywhere. Good notices latch `UNSAFE_GOOD` in the harness, retaining
an already accepted defect's deadline and refusing subsequent defect acceptance.

This Python harness checks lifecycle/protocol integration, NOT the entire C++
servo state machine, validated arrival windows, or physical separation. The C++
simulation remains the gate-motion model. No 350 ms safety allowance is approved.
Actual one-way latency, source-age budget, loaded movement variation, earliest
and latest gate arrivals, extra clearance and Good-route clearance remain unknown.
Future physical integration still requires validated timing configuration, real
handshake/ACK/retry routing, bounded fault recovery and reviewed Good occupancy.

Run the software-only regressions (no COM port or firmware connection):

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_experimental_reject_observer test_experimental_reject_client test_camera_exit test_camera_capture test_camera_diagnostics test_esp32_bridge -q
```

## Optional asynchronous JSONL export

`CAMERA_REJECT_DRY_RUN_EXPORT` defaults to **false**. Export requires BOTH it
and `CAMERA_REJECT_DRY_RUN=true`; export alone does not enable collection.
In the PowerShell terminal that starts the existing application, run:

```powershell
Set-Location 'C:\Users\Lira Angela Ata\eggsort2\eggsortcapstone'
$env:CAMERA_REJECT_DRY_RUN = "true"
$env:CAMERA_REJECT_DRY_RUN_EXPORT = "true"
.\.venv\Scripts\python.exe -B app.py
```

Then operate your existing application normally. This starts one application;
the exporter never opens another camera, connects serial, or sends commands.
Legacy physical rejection and ordinary application recording remain active.
The observer/exporter does not activate the experimental firmware scheduler.

Files appear under the project-root directory
`offline_tests/reject_observations/` with names
`reject-<UTC timestamp>-<process ID>-<run token>.jsonl`. This generated directory
is excluded from Git. Each line is a complete JSON object: `run_start`, existing
allowlisted `observation` records, then `run_end`. Event fields preserve run and
session identity, runtime generation, capture ID, observation passage ID,
sequence, monotonic timestamp, quality and first-defect timestamp. A
`passage_exit` event carries final passage quality; the footer lists unfinished
passages rather than inventing a final label. No full runtime context, frames,
database configuration, passwords, or model settings are exported.

The camera producer uses a bounded, non-waiting queue (1024 items); formatting,
directory creation, disk writes and per-line flushes happen on a daemon worker.
Events already queued remain available when the in-memory observer resets.
Normal camera stop queues an end marker after its workers stop; restart ends
the old run before establishing the next identity/file. Ordinary interpreter
shutdown drains the queue with bounded waits and closes an active file. Export
errors are logged by exception type without payloads and do not affect legacy
camera/rejection decisions. A full queue drops export items instead of blocking
inference. Sequence gaps/end counts make affected runs incomplete.

Use the normal Stop action, allow the writer to finish, and exit normally.
Forced process termination, power loss, disk failure or the bounded shutdown
timeout can still leave missing/truncated files; this is not a durable safety
interlock. A file without a valid closing marker, with sequence loss, or with
an unfinished passage must not be treated as a complete replay. Raw JSONL may
still be inspected diagnostically. In-memory buffer overflow does not imply
export loss when the independent export queue kept up.

Load a completed file without importing the application or opening hardware:

```powershell
$env:EGGSORT_REPLAY_FILE = 'offline_tests/reject_observations/YOUR-FILE.jsonl'
@'
import os
from experimental_reject_export import load_observation_export
from experimental_reject_observer import OfflineRejectReplay
events = load_observation_export(os.environ["EGGSORT_REPLAY_FILE"])
replay = OfflineRejectReplay(events)
print(replay.final_qualities)
print(replay.warnings)
'@ | .\.venv\Scripts\python.exe -B -
```

This validates the saved stream and prepares offline protocol replay, not the
C++ servo state machine or physical clearance. No new arrival/clearance/timing
assumptions are approved by exporting observations.

For actual PC execution of the existing C++ parser/state machine and return of
its replies through the Python ACK handler, see [offline_reject_replay.md](offline_reject_replay.md).

To disable export but retain collection, set export to false before restarting.
To disable both for the next application start:

```powershell
$env:CAMERA_REJECT_DRY_RUN = "false"
$env:CAMERA_REJECT_DRY_RUN_EXPORT = "false"
```

Do not disable by force-killing a running process; finish the current run normally.
No changes to the actual `.env` are required for these terminal-local settings.
