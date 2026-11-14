"""Optional asynchronous observation export. No camera, serial, or application imports."""
import atexit
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread


EXPORT_DIRECTORY = Path(__file__).resolve().parent / "offline_tests" / "reject_observations"
LOG = logging.getLogger(__name__)


class RejectObservationExporter:
    def __init__(self, *, directory=EXPORT_DIRECTORY, capacity=1024):
        if capacity < 1:
            raise ValueError("Export queue must have a positive bounded capacity")
        self.directory = Path(directory)
        self._queue = Queue(maxsize=capacity)
        self._lock = Lock()
        self._closed = False
        self._latest = None
        self.error = None
        self.dropped = 0
        self._thread = Thread(target=self._write_loop, name="reject-observation-export", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _enqueue(self, item):
        # Only a bounded memory operation on the camera thread. Never wait for I/O.
        with self._lock:
            if self._closed or self.error is not None:
                return False
            if item[0] == "observation":
                event = item[1]
                self._latest = (event.run_id, event.sequence)
            try:
                self._queue.put_nowait(item)
                return True
            except Full:
                self.dropped += 1
                return False

    def submit(self, event):
        return self._enqueue(("observation", event))

    def finish(self, run_id, last_sequence, reason):
        return self._enqueue(("end", (run_id, last_sequence, reason)))

    def close(self):
        """Bounded drain at ordinary interpreter shutdown, never during inference."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            latest = self._latest
        try:
            self._queue.put(("shutdown", latest), timeout=2)
        except Full:
            LOG.warning("Reject observation export could not drain; files may be incomplete.")
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            LOG.warning("Reject observation export still pending; files may be incomplete.")
        atexit.unregister(self.close)

    def _write_loop(self):
        stream = None
        run_id = None
        last_sequence = 0
        contiguous = True
        active = set()

        def write(record):
            stream.write(json.dumps(record, allow_nan=False) + "\n")
            stream.flush()

        def finish(expected, reason):
            nonlocal stream
            if stream is not None:
                complete = contiguous and last_sequence == expected and not active
                write({"type": "run_end", "run_id": run_id,
                       "last_sequence": last_sequence, "expected_sequence": expected,
                       "complete": complete,
                       "unfinished_passage_ids": sorted(active), "reason": reason})
                stream.close()
                stream = None
                if not complete:
                    LOG.warning("Reject observation run saved as incomplete; "
                                "missing events or unfinished passages prevent validated replay.")

        try:
            while True:
                kind, value = self._queue.get()
                if kind == "shutdown":
                    if stream is not None:
                        expected = value[1] if value and value[0] == run_id else -1
                        finish(expected, "application_shutdown")
                    return
                if kind == "end":
                    ended_run, expected, reason = value
                    if stream is not None and ended_run == run_id:
                        finish(expected, reason)
                    continue
                event = value
                if stream is None or event.run_id != run_id:
                    if stream is not None:
                        finish(-1, "missing_end_marker")
                    self.directory.mkdir(parents=True, exist_ok=True)
                    run_id = event.run_id
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    path = self.directory / f"reject-{stamp}-{os.getpid()}-{run_id:016X}.jsonl"
                    stream = path.open("x", encoding="utf-8", newline="\n")
                    last_sequence, contiguous, active = 0, True, set()
                    write({"type": "run_start", "schema_version": 1, "run_id": run_id})
                contiguous = contiguous and event.sequence == last_sequence + 1
                last_sequence = event.sequence
                if event.event == "passage_begin":
                    active.add(event.passage_id)
                elif event.event == "passage_exit":
                    active.discard(event.passage_id)
                # Frozen allowlisted event only: never serialize camera/runtime objects.
                write({"type": "observation", **asdict(event)})
        except Exception as exc:
            # Do not log payloads, arbitrary exception text, or camera context.
            self.error = type(exc).__name__
            LOG.warning("Reject observation export failed (%s); files may be incomplete. "
                        "Legacy camera/rejection continues unchanged.", self.error)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def load_observation_export(path):
    """Strict offline loader: unfinished, truncated, or lossy runs cannot look valid."""
    from experimental_reject_observer import PassageObservation

    with Path(path).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    if (len(records) < 3 or records[0].get("type") != "run_start"
            or records[0].get("schema_version") != 1
            or records[-1].get("type") != "run_end" or records[-1].get("complete") is not True):
        raise ValueError("Incomplete observation export; do not use for validated replay")
    events = []
    for record in records[1:-1]:
        record = dict(record)
        if record.pop("type", None) != "observation":
            raise ValueError("Invalid observation record")
        events.append(PassageObservation(**record))
    run = records[0]["run_id"]
    if (any(e.run_id != run for e in events) or records[-1]["run_id"] != run
            or [e.sequence for e in events] != list(range(1, len(events) + 1))
            or records[-1]["last_sequence"] != len(events)
            or records[-1]["expected_sequence"] != len(events)):
        raise ValueError("Incomplete or inconsistent observation sequence")
    return tuple(events)
