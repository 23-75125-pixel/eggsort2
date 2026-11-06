"""Default-off, bounded camera telemetry. Only the summary worker touches disk."""
from __future__ import annotations

import json
import logging
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4


class CameraPerformance:
    def __init__(self, enabled=False, interval=5.0, directory=None):
        self.enabled = enabled
        self.interval = max(5.0, interval)
        self.directory = Path(directory or Path(tempfile.gettempdir()) / "eggsort-camera-performance")
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self.path = None
        self._metrics = {}
        self._counts = {}
        self._stages = {}
        self._camera = {}
        self._runtime = {}
        self._started = self._window = monotonic()

    def start(self, gauges):
        if not self.enabled:
            return
        with self._lock:
            self._metrics, self._counts, self._stages, self._camera, self._runtime = {}, {}, {}, {}, {}
            self._started = self._window = monotonic()
        self._stop = Event()
        self.path = self.directory / (datetime.now(timezone.utc).strftime("performance-%Y%m%dT%H%M%S-") + uuid4().hex + ".jsonl")
        self._thread = Thread(target=self._run, args=(gauges, self._stop, self.path), name="eggsort-camera-performance", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self.enabled = False
            logging.getLogger(__name__).warning("Camera performance worker could not start.")

    def stop(self):
        self._stop.set()  # Never join or write while a camera lock may be held.

    def stage(self, worker, stage):
        with self._lock:
            self._stages[worker] = (stage, monotonic())

    def record(self, metrics=None, **counts):
        with self._lock:
            for name, value in (metrics or {}).items():
                if not math.isfinite(value):
                    continue
                n, total, maximum = self._metrics.get(name, (0, 0.0, value))
                self._metrics[name] = (n + 1, total + value, max(maximum, value))
            for name, value in counts.items():
                self._counts[name] = self._counts.get(name, 0) + value

    def camera(self, **values):
        with self._lock:
            self._camera.update(values)

    def runtime(self, values):
        with self._lock:
            self._runtime = dict(values)

    def summary(self):
        now = monotonic()
        with self._lock:
            seconds = max(now - self._window, 1e-9)
            result = {
                "event": "camera_performance", "utc": datetime.now(timezone.utc).isoformat(),
                "monotonic_s": now, "uptime_s": now - self._started, "window_s": seconds,
                "camera": dict(self._camera), "counts": dict(self._counts),
                "inference_runtime": dict(self._runtime),
                "rates_per_s": {k: v / seconds for k, v in self._counts.items()},
                "timings_ms": {k: {"samples": n, "mean": total / n, "max": maximum}
                               for k, (n, total, maximum) in self._metrics.items()},
                "workers": {k: {"stage": stage, "stage_age_ms": (now - stamp) * 1000}
                            for k, (stage, stamp) in self._stages.items()},
            }
            for name in ("reads", "captures", "jpegs", "predictions", "inference_cycles", "skipped_raw_frames"):
                result["counts"].setdefault(name, 0)
                result["rates_per_s"].setdefault(name, 0.0)
            self._metrics, self._counts = {}, {}
            self._window = now
            return result

    def _run(self, gauges, stop, path):
        try:
            from camera_resources import ResourceSampler
            resources = ResourceSampler()
            path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                stopping = stop.wait(self.interval)
                result = self.summary()
                result["gauges"] = gauges()  # Approximate, lock-free counts; no frame copies.
                try:
                    result["resources"] = resources.sample()
                except Exception:
                    result["resources"] = {"available": False, "reason": "resource sampling failed"}
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, allow_nan=False) + "\n")
                if stopping or not any(result["gauges"][k] for k in ("capture_alive", "inference_alive")):
                    break
        except Exception:
            # Do not expose exception text (paths/config) or affect live workers.
            logging.getLogger(__name__).warning("Camera performance export stopped; diagnostic write/sampling failed.")


def camera_readback(capture, cv2, requested):
    """A driver's get() is advisory; actual frame shape is reported separately."""
    values = {"requested": requested}
    for name, prop in (("width", cv2.CAP_PROP_FRAME_WIDTH), ("height", cv2.CAP_PROP_FRAME_HEIGHT), ("fps", cv2.CAP_PROP_FPS)):
        try:
            value = float(capture.get(prop))
            values[name] = value if math.isfinite(value) else None
        except Exception:
            values[name] = None
    return values
