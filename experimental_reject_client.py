"""Default-disabled protocol preparation; never connected to a serial transport.

No serial ports, worker threads, ACK-triggered sends, or hardware activation.
The caller supplies existing camera passage IDs and the FIRST defect timestamp.
Good observations explicitly report unverified routing in the firmware model.
"""
from __future__ import annotations

import math
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, replace


@dataclass
class RequestRecord:
    capture_id: int
    quality: str
    detected_at: float  # Host monotonic; only used for source age, never MCU due time.
    controller_deadline: int | None = None
    status: str = "NEW"
    terminal: bool = False


class ExperimentalRejectClient:
    """Build frames only; camera observations use this exclusively in offline replay."""
    ACK = re.compile(r"XA:([0-9A-F]{8}):([0-9A-F]{16}):(\d{1,10}):(\d{1,10}):([A-Z_]+)")
    STATUSES = frozenset({
        "OK", "DISABLED", "STARTED", "RUN_MISMATCH", "INVALID", "STALE",
        "DUPLICATE", "CONFLICT", "ACCEPTED", "QUEUED", "QUEUE_FULL",
        "UNVERIFIED_TIMING", "UNVERIFIED_CLEARANCE", "UNSAFE_GOOD",
        "SPACING_CONFLICT", "EXPIRED", "DEADLINE_MISSED", "SETTLED_UNVERIFIED",
    })
    TERMINAL = STATUSES - {"OK", "STARTED", "DUPLICATE", "ACCEPTED", "QUEUED"}

    def __init__(self, *, enabled: bool = False, run_id: int | None = None,
                 max_pending: int = 4, history_size: int = 32):
        self.enabled = enabled
        self.run_id = run_id if run_id is not None else secrets.randbits(64) or 1
        if not 0 < self.run_id <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("A nonzero 64-bit run identity is required")
        if not 1 <= max_pending <= 4 or history_size < max_pending:
            raise ValueError("Pending capacity is 1..4; history must cover it")
        self.boot: int | None = None
        self.ready = False
        self._waiting_begin = False
        self._restarted = False
        self._max_pending = max_pending
        self._history_size = history_size
        self._high_water = 0
        self._records: OrderedDict[int, RequestRecord] = OrderedDict()

    def _require_enabled(self):
        if not self.enabled:
            raise RuntimeError("Experimental reject mode is disabled")
        if self._restarted:
            raise RuntimeError("Controller restarted; no automatic replay is allowed")

    def hello_frame(self) -> str:
        self._require_enabled()
        return "XH"

    def accept_hello(self, line: str) -> bool:
        self._require_enabled()
        match = re.fullmatch(r"XH:([0-9A-F]{8})", line.strip())
        if not match or int(match[1], 16) == 0:
            return False  # Includes XE:EXPERIMENT_DISABLED; never use legacy fallback.
        boot = int(match[1], 16)
        if self.boot is not None and boot != self.boot:
            self.ready = False
            self._waiting_begin = False
            self._restarted = True
            raise RuntimeError("Controller restarted; create a fresh supervised run")
        self.boot = boot
        return True

    def begin_frame(self) -> str:
        self._require_enabled()
        if self.boot is None:
            raise RuntimeError("Controller boot handshake is required")
        self._waiting_begin = True
        return f"XB:{self.boot:08X}:{self.run_id:016X}"

    def request_frame(self, capture_id: int, quality: str, *, detected_at: float, now: float) -> str:
        self._require_enabled()
        if not self.ready:
            raise RuntimeError("Experimental run has not been acknowledged")
        if isinstance(capture_id, bool) or not isinstance(capture_id, int) or not 0 < capture_id <= 0xFFFFFFFF:
            raise ValueError("A positive 32-bit camera capture ID is required")
        quality = quality.strip().upper()
        if quality not in {"CRACK", "ROTTEN", "GOOD"}:
            raise ValueError("Only existing camera passage qualities are supported")
        if not math.isfinite(detected_at) or not math.isfinite(now) or detected_at > now:
            raise ValueError("Invalid source observation time")
        record = self._records.get(capture_id)
        if record is None:
            if capture_id <= self._high_water:
                raise RuntimeError("Retired passage ID cannot be reused")
            if sum(not r.terminal for r in self._records.values()) >= self._max_pending:
                raise RuntimeError("Experimental pending request capacity exceeded")
            if len(self._records) == self._history_size:
                retired = next((key for key, r in self._records.items() if r.terminal), None)
                if retired is None:
                    raise RuntimeError("Experimental request history is full")
                del self._records[retired]
            if (now - detected_at) * 1000 > 60000:
                raise RuntimeError("Source observation is stale")
            record = RequestRecord(capture_id, quality, detected_at)
            self._records[capture_id] = record
            self._high_water = capture_id
        elif quality != record.quality:
            raise RuntimeError("A retry cannot change the original reject quality")
        return self.retry_frame(capture_id, now=now)

    def retry_frame(self, capture_id: int, *, now: float) -> str:
        self._require_enabled()
        if not self.ready or self.boot is None:
            raise RuntimeError("Experimental run has not been acknowledged")
        record = self._records[capture_id]
        if record.terminal:
            raise RuntimeError("Terminal passage cannot be automatically replayed")
        if not math.isfinite(now) or now < record.detected_at:
            raise ValueError("Invalid retry time")
        age = int((now - record.detected_at) * 1000)
        if age > 60000:
            raise RuntimeError("Source observation is stale")
        frame = f"XR:{self.boot:08X}:{self.run_id:016X}:{capture_id}:{record.quality[0]}:{age}"
        assert len(frame) <= 80  # Firmware's existing line-buffer limit.
        return frame

    def accept_ack(self, line: str) -> bool:
        self._require_enabled()
        match = self.ACK.fullmatch(line.strip())
        if not match:
            return False
        boot, run, capture_id, deadline, status = (
            int(match[1], 16), int(match[2], 16), int(match[3]), int(match[4]), match[5]
        )
        if boot != self.boot or run != self.run_id or status not in self.STATUSES or deadline > 0xFFFFFFFF:
            return False
        if capture_id == 0:
            if status != "STARTED" or deadline != 0 or not self._waiting_begin:
                return False
            self._waiting_begin = False
            self.ready = True
            return True
        record = self._records.get(capture_id)
        if record is None or record.terminal:
            return False  # ACKs never construct requests or reopen completed work.
        if record.controller_deadline is not None and record.controller_deadline != deadline:
            return False
        if status in {"OK", "ACCEPTED", "QUEUED", "DUPLICATE", "SETTLED_UNVERIFIED", "DEADLINE_MISSED", "EXPIRED", "SPACING_CONFLICT"}:
            record.controller_deadline = deadline  # Zero is valid after millis wrap.
        record.status = status
        record.terminal = status in self.TERMINAL
        return True

    def record(self, capture_id: int) -> RequestRecord:
        return replace(self._records[capture_id])
