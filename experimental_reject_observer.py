"""Opt-in, bounded camera observations and OFFLINE protocol simulation only.

No hardware transport or command callbacks are provided. Optional observation
export uses a bounded background writer; protocol clients remain offline only.
"""
from collections import deque
from dataclasses import dataclass
from secrets import randbits

from experimental_reject_client import ExperimentalRejectClient


@dataclass(frozen=True)
class PassageObservation:
    run_id: int
    session_ref: str | None
    runtime_generation: str | None
    sequence: int
    passage_id: int  # Observation ordinal; capture_id may be reused after short Good.
    capture_id: int
    event: str
    at: float  # Python monotonic, never an ESP32 timestamp.
    quality: str
    first_defect_at: float | None


class DryRunRejectObservations:
    def __init__(self, *, enabled=False, capacity=512, export_enabled=False):
        if capacity < 4:
            raise ValueError("Observation capacity must be at least four")
        self.enabled = enabled
        self._exporter = None
        if enabled and export_enabled:
            from experimental_reject_export import RejectObservationExporter
            self._exporter = RejectObservationExporter()
        self._events = deque(maxlen=capacity)
        self.reset()

    def reset(self):
        if hasattr(self, "run_id"):
            self.finish("camera_restart")
        self.run_id = randbits(64) or 1
        self._events.clear()
        self.sequence = self.passage_id = self.dropped = 0
        self.first_defect_at = None

    def emit(self, event, capture, at, session_ref, runtime_generation):
        if not self.enabled:
            return
        if event == "passage_begin":
            self.passage_id += 1
            self.first_defect_at = None
        if event == "first_defect":
            if self.first_defect_at is not None:
                return
            self.first_defect_at = at
        self.sequence += 1
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        observation = PassageObservation(
            self.run_id, session_ref,
            str(runtime_generation) if runtime_generation is not None else None,
            self.sequence, self.passage_id, capture["capture_id"], event, at,
            capture["label"], self.first_defect_at,
        )
        self._events.append(observation)
        if self._exporter is not None:
            self._exporter.submit(observation)

    def finish(self, reason="camera_stop"):
        if self._exporter is not None and self.sequence:
            self._exporter.finish(self.run_id, self.sequence, reason)

    def snapshot(self):
        """Caller holds the camera lock. Overflow makes replay incomplete/unsafe."""
        return tuple(self._events), self.dropped


class OfflineRejectReplay:
    """Explicit offline harness, NOT a hardware scheduler or physical approval.

    replay receives the complete bounded snapshot. accept() supplies independent
    simulated MCU acceptance and host send times plus one-way transport latency.
    No Python absolute timestamp is compared to MCU millis().
    """
    def __init__(self, events, *, dropped=0):
        if dropped:
            raise ValueError("Incomplete observation stream: events were dropped")
        self.events = tuple(events)
        if not self.events or [e.sequence for e in self.events] != list(range(1, len(self.events) + 1)):
            raise ValueError("A complete ordered run snapshot is required")
        run = self.events[0].run_id
        if any(e.run_id != run for e in self.events):
            raise ValueError("Cannot mix camera runs")
        self.client = ExperimentalRejectClient(enabled=True, run_id=run)
        self.client.accept_hello("XH:12345678")
        self.client.begin_frame()
        self.client.accept_ack(f"XA:12345678:{run:016X}:0:0:STARTED")
        self.jobs = {}
        self.final_qualities = {}
        self.warnings = ["Physical clearance and arrival bounds remain UNVERIFIED"]
        for e in self.events:
            if e.event == "passage_exit":
                self.final_qualities[e.passage_id] = e.quality
            if e.event == "first_defect" or (e.quality == "good" and e.event in {"quality_locked", "passage_exit"}):
                self.jobs.setdefault(e.passage_id, e)
        if any(e.quality == "good" for e in self.jobs.values()):
            self.warnings.append("UNSAFE_GOOD: Good-route clearance unknown; mixed routes are unverified")
        self._accepted = {}
        self._good_fault = False

    def accept(self, passage_id, *, host_send_at, controller_accept_ms, forward_latency_ms):
        if passage_id in self._accepted:
            return self._accepted[passage_id]  # Never construct a second job/ACK.
        if passage_id != next((key for key in self.jobs if key not in self._accepted), None):
            raise ValueError("Simulated acceptance must follow passage order")
        if forward_latency_ms < 0 or not 0 <= controller_accept_ms <= 0xFFFFFFFF:
            raise ValueError("Invalid simulated controller timing")
        e = self.jobs[passage_id]
        source_at = e.first_defect_at if e.first_defect_at is not None else e.at
        frame = self.client.request_frame(passage_id, e.quality, detected_at=source_at, now=host_send_at)
        self._good_fault = self._good_fault or e.quality == "good"
        status = "UNSAFE_GOOD" if self._good_fault else "ACCEPTED"
        deadline = (controller_accept_ms + 6500) & 0xFFFFFFFF if status == "ACCEPTED" else 0
        ack = f"XA:12345678:{e.run_id:016X}:{passage_id}:{deadline}:{status}"
        if not self.client.accept_ack(ack):
            raise ValueError("Invalid simulated acknowledgment")
        result = dict(frame=frame, ack=ack, controller_deadline=deadline,
                      source_age_at_accept_ms=(host_send_at - source_at) * 1000 + forward_latency_ms,
                      status=status, physical_safety="UNVERIFIED")
        self._accepted[passage_id] = result
        return result
