"""Persistent, low-latency OpenCV camera and YOLO detection session."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event, Lock, RLock, Thread
from time import monotonic
from typing import Any, Callable

from config import env_bool, env_float, env_int, env_text
from camera_performance import CameraPerformance
from camera_negotiation import configure_camera, verify_frame
from detection_service import (
    CONFIDENCE,
    DetectorUnavailableError,
    annotate_image,
    detect_image,
    detector_info,
)


# Exact class set used by the trained three-class egg-quality model. A missing
# detection is treated as "no egg"; it is never stored as an egg quality.
EGG_QUALITY_LABELS = {"crack", "good", "rotten"}
NO_EGG_LABEL = "no egg"
_DIAGNOSTIC_LOG = logging.getLogger(__name__)
_DIAGNOSTIC_FILE_LOCK = Lock()


def normalize_model_label(value: Any) -> str:
    """Normalize model spelling/case without changing its class meaning."""
    return " ".join(
        str(value).strip().casefold().replace("_", " ").replace("-", " ").split()
    )


class CameraSessionError(RuntimeError):
    """Raised when the server camera cannot be started."""


class CameraDetectionSession:
    """Capture smoothly while YOLO independently processes the newest frame."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._frame_ready = Condition(self._lock)
        self._raw_frame_ready = Condition(self._lock)
        self._quality_ready = Condition(self._lock)
        self._stop_event = Event()
        self._capture_thread: Thread | None = None
        self._inference_thread: Thread | None = None
        self._capture: Any | None = None
        self._running = False
        self._performance = CameraPerformance(
            enabled=env_bool("CAMERA_PERFORMANCE_DIAGNOSTICS", False),
            interval=env_float("CAMERA_PERFORMANCE_INTERVAL_SECONDS", 5.0, minimum=0.1, maximum=3600.0),
        )
        self._latest_raw_returned_at: float | None = None
        self._performance_processed_sequence = 0

        self._latest_raw_frame: Any | None = None
        self._raw_sequence = 0
        self._inspection_min_sequence = 0
        self._latest_jpeg: bytes | None = None
        self._frame_sequence = 0
        self._latest_result: dict[str, Any] | None = None
        self._quality_history: deque[tuple[float, str, float]] = deque(
            maxlen=200
        )
        self._captured_qualities: deque[dict[str, Any]] = deque(maxlen=50)
        self._passage_samples: list[tuple[str, float]] = []
        self._passage_misses = 0
        self._passage_missing_since: float | None = None
        self._passage_quality_present = False
        self._passage_defect: str | None = None
        self._passage_reject_sent = False
        self._active_capture: dict[str, Any] | None = None
        self._last_capture: dict[str, Any] | None = None
        self._egg_count = 0
        self._quality_counts = {label: 0 for label in EGG_QUALITY_LABELS}
        self._reject_handler: Callable[[str], Any] | None = None
        self._capture_handler: Callable[[dict[str, Any]], Any] | None = None
        self._reject_error: str | None = None

        self._error: str | None = None
        self._started_at: str | None = None
        self._session_ref: str | None = None
        self._runtime_context: dict[str, Any] = {}
        self._lifecycle_handler: Callable[[str, dict[str, Any]], Any] | None = None
        self._stream_fps = 0.0
        self._detection_fps = 0.0
        self._inference_ms = 0.0
        self._model_info: dict[str, Any] | None = None
        self.camera_index = env_int("CAMERA_INDEX", 0, minimum=0)
        self.camera_backend = env_text(
            "CAMERA_BACKEND",
            "dshow" if os.name == "nt" else "auto",
        ).lower()
        self.camera_width = env_int("CAMERA_WIDTH", 1280, minimum=160, maximum=7680)
        self.camera_height = env_int("CAMERA_HEIGHT", 720, minimum=120, maximum=4320)
        self.camera_fps = env_int("CAMERA_FPS", 30, minimum=1, maximum=240)
        self.camera_resolution_negotiation_fix = env_bool("CAMERA_RESOLUTION_NEGOTIATION_FIX", True)
        self.camera_capture_profile = env_text("CAMERA_CAPTURE_PROFILE", "existing").lower()
        if self.camera_capture_profile not in {"existing", "1080p_yuy2"}:
            raise ValueError("CAMERA_CAPTURE_PROFILE must be existing or 1080p_yuy2")
        if self.camera_capture_profile == "1080p_yuy2":
            self.camera_backend = "dshow"
            self.camera_width, self.camera_height, self.camera_fps = 1920, 1080, 30
        self._camera_negotiation: dict[str, Any] | None = None
        self.jpeg_quality = env_int(
            "CAMERA_JPEG_QUALITY", 72, minimum=1, maximum=100
        )
        self.inspection_left = env_float(
            "CAMERA_INSPECTION_LEFT", 0.40, minimum=0.0, maximum=1.0
        )
        self.inspection_right = env_float(
            "CAMERA_INSPECTION_RIGHT", 0.60, minimum=0.0, maximum=1.0
        )
        self.inspection_top = env_float(
            "CAMERA_INSPECTION_TOP", 0.36, minimum=0.0, maximum=1.0
        )
        self.inspection_bottom = env_float(
            "CAMERA_INSPECTION_BOTTOM", 0.64, minimum=0.0, maximum=1.0
        )
        if not self.inspection_left < self.inspection_right:
            raise ValueError(
                "CAMERA_INSPECTION_LEFT must be less than "
                "CAMERA_INSPECTION_RIGHT."
            )
        if not self.inspection_top < self.inspection_bottom:
            raise ValueError(
                "CAMERA_INSPECTION_TOP must be less than "
                "CAMERA_INSPECTION_BOTTOM."
            )
        self.passage_exit_samples = env_int(
            "CAMERA_PASSAGE_EXIT_SAMPLES", 2, minimum=1, maximum=120
        )
        # Start timing at the first unoccupied inference observation, not at
        # the previous detection. Both time and sample evidence are required.
        self.passage_exit_seconds = env_float(
            "CAMERA_PASSAGE_EXIT_SECONDS", 0.60, minimum=0.01, maximum=10.0
        )
        self.passage_occupancy_margin = env_float(
            "CAMERA_PASSAGE_OCCUPANCY_MARGIN", 0.01, minimum=0.0, maximum=0.10
        )
        self.capture_min_samples = env_int(
            "CAMERA_QUALITY_SAMPLES", 1, minimum=1, maximum=120
        )
        self.capture_max_age = env_float(
            "CAMERA_CAPTURE_MAX_AGE_SECONDS", 60.0, minimum=1.0, maximum=3600.0
        )
        # Temporary, opt-in observers only: never used by passage decisions.
        self.passage_diagnostics = env_bool("CAMERA_PASSAGE_DIAGNOSTICS", False)
        self.passage_diagnostic_frames = env_bool("CAMERA_PASSAGE_DIAGNOSTICS_FRAMES", False)
        self.passage_diagnostic_file = Path(env_text(
            "CAMERA_PASSAGE_DIAGNOSTICS_FILE",
            str(Path(tempfile.gettempdir()) / f"eggsort-camera-diagnostics-{os.getpid()}.jsonl"),
        )).expanduser().resolve()
        self._diag_write_error_reported = False
        self._reset_passage_diagnostics()

    def _reset_passage_diagnostics(self) -> None:
        self._diag_passage_number = 0
        self._diag_missing_since: float | None = None
        self._diag_last_accepted_at: float | None = None
        self._diag_last_exit_at: float | None = None

    def _log_passage_diagnostic(self, event: str, *, at: float | None = None, **fields: Any) -> None:
        if not self.passage_diagnostics:
            return
        # Logging must not interrupt inference, claims, or gate callbacks.
        try:
            payload = {
                "event": event,
                "monotonic_s": round(monotonic() if at is None else at, 6),
                "utc": datetime.now(timezone.utc).isoformat(),
                "session_ref": self._session_ref,
                "diagnostic_passage": self._diag_passage_number,
                "capture_id": self._active_capture.get("capture_id") if self._active_capture else None,
                "counter_before": self._egg_count,
                "counter_after": self._egg_count,
                **fields,
            }
            # Write plain JSON directly, independently of PowerShell's stderr
            # formatting. One locked append and close per event flushes the
            # complete UTF-8 record; embedded newlines remain JSON escapes.
            # Only the explicit event fields above are saved, never environment
            # variables, runtime context, credentials, images, or model data.
            line = json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n"
            with _DIAGNOSTIC_FILE_LOCK:
                with self.passage_diagnostic_file.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line)
        except Exception as exc:
            if not self._diag_write_error_reported:
                self._diag_write_error_reported = True
                try:
                    _DIAGNOSTIC_LOG.warning(
                        "EGGSORT_CAMERA_DIAG: JSONL event could not be saved (%s). "
                        "Check CAMERA_PASSAGE_DIAGNOSTICS_FILE and available disk space.",
                        type(exc).__name__,
                    )
                except Exception:
                    pass

    def _observe_passage_diagnostic(
        self, label: str, confidence: float, passage_occupied: bool = False
    ) -> None:
        if not self.passage_diagnostics:
            return
        now = monotonic()
        active = self._active_capture
        capture_id = active["capture_id"] if active else None
        if label in EGG_QUALITY_LABELS:
            if active is None:
                self._diag_passage_number += 1
                capture_id = self._egg_count + 1
                self._log_passage_diagnostic(
                    "passage_begin", at=now, capture_id=capture_id,
                    gap_since_previous_exit_ms=(
                        round((now - self._diag_last_exit_at) * 1000, 3)
                        if self._diag_last_exit_at is not None else None
                    ),
                    gap_since_previous_accepted_ms=(
                        round((now - self._diag_last_accepted_at) * 1000, 3)
                        if self._diag_last_accepted_at is not None else None
                    ),
                )
                self._diag_missing_since = None
            elif self._diag_missing_since is not None:
                self._log_passage_diagnostic(
                    "detection_reacquired", at=now, capture_id=capture_id,
                    missing_ms=round((now - self._diag_missing_since) * 1000, 3),
                    since_last_accepted_ms=round((now - self._diag_last_accepted_at) * 1000, 3)
                    if self._diag_last_accepted_at is not None else None,
                    consecutive_misses=self._passage_misses,
                )
                self._diag_missing_since = None
            self._diag_last_accepted_at = now
        elif active is not None and passage_occupied:
            if self._diag_missing_since is not None:
                self._log_passage_diagnostic(
                    "passage_occupancy_reacquired", at=now, capture_id=capture_id,
                    missing_ms=round((now - self._diag_missing_since) * 1000, 3),
                    quality_eligible=False,
                )
            self._diag_missing_since = None
        elif active is not None:
            if self._diag_missing_since is None:
                self._diag_missing_since = now
                self._log_passage_diagnostic(
                    "missing_begin", at=now, capture_id=capture_id,
                    missing_ms=0.0,
                    since_last_accepted_ms=round((now - self._diag_last_accepted_at) * 1000, 3)
                    if self._diag_last_accepted_at is not None else None,
                )
        if self.passage_diagnostic_frames:
            result = self._latest_result or {}
            zone = result.get("inspection_zone")
            if result.get("image_width") and result.get("image_height"):
                zone = [result["image_width"] * self.inspection_left,
                        result["image_height"] * self.inspection_top,
                        result["image_width"] * self.inspection_right,
                        result["image_height"] * self.inspection_bottom]
            boxes = []
            for detection in result.get("detections", []):
                box = detection.get("box", ())
                if normalize_model_label(detection.get("label", "")) not in EGG_QUALITY_LABELS or len(box) != 4:
                    continue
                cx = (float(box[0]) + float(box[2])) / 2
                cy = (float(box[1]) + float(box[3])) / 2
                boxes.append({
                    "label": detection["label"], "confidence": detection.get("confidence"),
                    "box_px": list(box), "center_px": [cx, cy],
                    # Negative distance identifies the crossed boundary.
                    "edge_distances_px_ltrb": [cx-zone[0], cy-zone[1], zone[2]-cx, zone[3]-cy]
                    if zone is not None else None,
                })
            self._log_passage_diagnostic(
                "quality_detection" if label in EGG_QUALITY_LABELS else "missing_observation",
                at=now, capture_id=capture_id, label=label, confidence=confidence,
                passage_occupied=passage_occupied or label in EGG_QUALITY_LABELS,
                claimed_on_scale=bool(active and active.get("claimed_on_scale")),
                used_for_quality_samples=label in EGG_QUALITY_LABELS
                and not bool(active and active.get("claimed_on_scale")),
                consecutive_misses=self._passage_misses + 1
                if active and label not in EGG_QUALITY_LABELS and not passage_occupied else 0,
                missing_ms=round((now-self._diag_missing_since)*1000, 3)
                if self._diag_missing_since is not None else None,
                inspection_zone_px=zone, boxes=boxes,
                inference_ms=round(self._inference_ms, 3),
            )

    def _observe_exit_diagnostic(self, capture: dict[str, Any], sample_count: int) -> None:
        if not self.passage_diagnostics:
            return
        now = monotonic()
        self._diag_last_exit_at = now
        claimed = bool(capture.get("claimed_on_scale"))
        insufficient = sample_count < self.capture_min_samples and capture["label"] == "good"
        self._log_passage_diagnostic(
            "passage_exit", at=now, capture_id=capture["capture_id"],
            label=capture["label"], consecutive_misses=self._passage_misses,
            missing_ms=round((now-self._diag_missing_since)*1000, 3)
            if self._diag_missing_since is not None else None,
            since_last_accepted_ms=round((now-self._diag_last_accepted_at)*1000, 3)
            if self._diag_last_accepted_at is not None else None,
            sample_count=sample_count, exit_samples_required=self.passage_exit_samples,
            exit_seconds_required=self.passage_exit_seconds,
            claimed_on_scale=claimed, counter_before=self._egg_count,
            # This observer runs before the existing counter branch. The
            # actual mutation is recorded separately as exit_counter_increment.
            counter_after=self._egg_count + int(not insufficient),
            counter_after_is_expected=True,
            existing_exit_will_increment=not insufficient,
            disposition="claimed_completed" if claimed else "insufficient_good_samples" if insufficient else "completed",
        )
        self._diag_missing_since = None

    def set_lifecycle_handler(self, handler):
        self._lifecycle_handler = handler

    def start(self, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()
            if any(t and t.is_alive() for t in (self._capture_thread, self._inference_thread)):
                raise CameraSessionError("Previous camera workers have not finished.")

        try:
            import cv2
        except ImportError as exc:
            raise CameraSessionError(
                "OpenCV is not installed. Run: pip install -r requirements.txt"
            ) from exc

        # Fail camera startup immediately when YOLO_MODEL_PATH points to the
        # wrong weights or the weights do not contain the trained three-class
        # label set. This avoids showing a running session that can never
        # produce a valid egg record.
        try:
            model_info = detector_info()
        except DetectorUnavailableError as exc:
            raise CameraSessionError(str(exc)) from exc

        with self._lock:
            # Multiple dashboard/status requests can arrive immediately after
            # login. Re-check while holding the lock so only one request may
            # create capture/inference threads and open the physical camera.
            if self._running:
                return self.status()
            self._capture = None
            self._stop_event.clear()
            self._running = True
            self._latest_raw_frame = None
            self._camera_negotiation = None
            self._latest_raw_returned_at = None
            self._raw_sequence = 0
            self._inspection_min_sequence = 0
            self._latest_jpeg = None
            self._frame_sequence = 0
            self._latest_result = None
            self._quality_history.clear()
            self._captured_qualities.clear()
            self._passage_samples.clear()
            self._passage_misses = 0
            self._passage_missing_since = None
            self._passage_quality_present = False
            self._passage_defect = None
            self._passage_reject_sent = False
            self._active_capture = None
            self._last_capture = None
            self._egg_count = 0
            self._quality_counts = {label: 0 for label in EGG_QUALITY_LABELS}
            self._reset_passage_diagnostics()
            self._reject_error = None
            self._error = None
            self._stream_fps = 0.0
            self._detection_fps = 0.0
            self._inference_ms = 0.0
            self._model_info = model_info
            self._started_at = datetime.now(timezone.utc).isoformat()
            if not context or not context.get("session_ref") or not context.get("runtime_generation"):
                self._running = False
                raise CameraSessionError("A committed sorting-session context is required.")
            self._runtime_context = dict(context)
            self._session_ref = context["session_ref"]
            self._log_passage_diagnostic(
                "diagnostics_started", frames_enabled=self.passage_diagnostic_frames,
                exit_samples=self.passage_exit_samples, quality_samples=self.capture_min_samples,
                exit_seconds=self.passage_exit_seconds,
                occupancy_margin_fraction=self.passage_occupancy_margin,
                zone_fractions=[self.inspection_left, self.inspection_top,
                                self.inspection_right, self.inspection_bottom],
            )
            self._capture_thread = Thread(
                target=self._capture_loop,
                name="eggsort-camera-capture",
                daemon=True,
            )
            self._inference_thread = Thread(
                target=self._inference_loop,
                name="eggsort-yolo-inference",
                daemon=True,
            )
            if self._performance.enabled:
                previous = self._performance
                previous.stop()
                # Separate accumulator per run: a slow old writer cannot consume
                # the next camera run's measurements.
                self._performance = CameraPerformance(True, previous.interval, previous.directory)
                self._performance_processed_sequence = 0
            self._performance.start(self._performance_gauges)
            self._capture_thread.start()
            self._inference_thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            capture_thread = self._capture_thread
            inference_thread = self._inference_thread
            capture = self._capture
            self._stop_event.set()
            self._frame_ready.notify_all()
            self._raw_frame_ready.notify_all()
            self._quality_ready.notify_all()

        # ``VideoCapture.read()`` can block while a USB webcam is waiting for
        # its next frame.  Releasing the handle first makes that read return,
        # so a manual Stop Session does not wait for the camera thread timeout.
        # The capture loop may release it again during its own cleanup; OpenCV
        # accepts that safely.
        if capture is not None:
            try:
                capture.release()
            except Exception:
                # The shutdown state still depends on the worker threads, not
                # on whether a driver accepts a second/late release call.
                pass

        if capture_thread and capture_thread.is_alive():
            capture_thread.join(timeout=5)
        if inference_thread and inference_thread.is_alive():
            inference_thread.join(timeout=15)

        with self._lock:
            threads_alive = any(
                thread and thread.is_alive()
                for thread in (capture_thread, inference_thread)
            )
            if not threads_alive:
                self._performance.stop()
                self._running = False
                self._capture_thread = None
                self._inference_thread = None
            return self.status()

    def _performance_gauges(self) -> dict[str, Any]:
        # Summary thread only. Approximate snapshots, deliberately no camera lock
        # or application callbacks: telemetry must not wait behind a stalled worker.
        return {
            "capture_alive": bool(self._capture_thread and self._capture_thread.is_alive()),
            "inference_alive": bool(self._inference_thread and self._inference_thread.is_alive()),
            "quality_history": len(self._quality_history), "quality_history_capacity": 200,
            "quality_queue": len(self._captured_qualities), "quality_queue_capacity": 50,
            "passage_samples": len(self._passage_samples),
            "raw_slot_capacity": 1, "raw_sequence": self._raw_sequence,
            "raw_pending": int(self._raw_sequence > self._performance_processed_sequence),
            "jpeg_sequence": self._frame_sequence,
            "stop_requested": self._stop_event.is_set(),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = self._latest_result or {}
            detections = result.get("detections", [])
            counts: dict[str, int] = {}
            for detection in detections:
                label = normalize_model_label(detection.get("label", ""))
                counts[label] = counts.get(label, 0) + 1
            zone_quality, zone_confidence = self._select_frame_observation(
                detections, result.get("inspection_zone")
            )
            return {
                "running": self._running,
                "error": self._error,
                "started_at": self._started_at,
                "session_ref": self._session_ref,
                "frame_ready": self._latest_jpeg is not None,
                "total": len(detections),
                "counts": counts,
                "detections": detections,
                "detection_ready": self._latest_result is not None,
                "zone_quality": zone_quality,
                "zone_confidence": zone_confidence,
                "confidence_threshold": result.get(
                    "confidence_threshold", CONFIDENCE
                ),
                "stream_fps": round(self._stream_fps, 1),
                "detection_fps": round(self._detection_fps, 1),
                "inference_ms": round(self._inference_ms),
                "model": dict(self._model_info) if self._model_info else None,
                "quality_queue": len(self._captured_qualities),
                "inspection_active": self._active_capture is not None,
                "egg_count": self._egg_count,
                "quality_counts": dict(self._quality_counts),
                "active_capture": (
                    dict(self._active_capture) if self._active_capture else None
                ),
                "last_capture": (
                    dict(self._last_capture) if self._last_capture else None
                ),
                "reject_error": self._reject_error,
                "camera_negotiation": dict(self._camera_negotiation) if self._camera_negotiation else None,
            }

    def wait_for_captured_quality(
        self,
        timeout: float = 1.0,
        is_current: Callable[[], bool] | None = None,
        allow_loaded_capture: bool = False,
    ) -> dict[str, Any] | None:
        """Return the oldest center-zone capture for the next load-cell egg."""
        deadline = monotonic() + timeout
        with self._quality_ready:
            while True:
                if is_current is not None and not is_current():
                    return None
                cutoff = monotonic() - self.capture_max_age
                if (
                    self._captured_qualities
                    and self._captured_qualities[0]["captured_at"] < cutoff
                ):
                    raise CameraSessionError("Unmatched camera capture expired; clear the station before restarting.")
                if self._captured_qualities:
                    result = dict(self._captured_qualities.popleft())
                    result.pop("captured_at", None)
                    self._log_passage_diagnostic(
                        "good_capture_claim", capture_id=result["capture_id"],
                        source="completed_queue", before_exit=False,
                        counter_increment=0, counter_before=self._egg_count,
                        counter_after=self._egg_count,
                    )
                    return result
                # A load-cell egg may remain in the camera zone until its gate
                # opens. Finish its inspection after the configured Good frames
                # instead of requiring movement that authorization prevents.
                required = self.capture_min_samples
                if (allow_loaded_capture and self._active_capture is not None
                        and not self._active_capture.get("claimed_on_scale")
                        and self._passage_defect is None
                        and self._passage_misses == 0
                        and self._passage_quality_present
                        and len(self._passage_samples) >= required
                        and all(label == "good" for label, _ in self._passage_samples[-required:])):
                    capture = self._active_capture
                    capture["claimed_on_scale"] = True
                    capture["confidence"] = round(min(
                        confidence for _, confidence in self._passage_samples[-required:]
                    ), 4)
                    self._log_passage_diagnostic(
                        "good_capture_claim", capture_id=capture["capture_id"],
                        source="visible_loaded_capture", before_exit=True,
                        counter_increment=0, counter_before=self._egg_count,
                        counter_after=self._egg_count,
                    )
                    # Keep the passage occupied until exit, preventing another
                    # camera identity for an already inspected, released egg.
                    return dict(capture)
                if not self._running or self._error:
                    return None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._quality_ready.wait(timeout=remaining)

    def quality_snapshot(self, window_seconds: float = 3.0) -> dict[str, Any]:
        """Return the strongest recent camera quality classification."""
        cutoff = monotonic() - window_seconds
        with self._lock:
            recent = [
                (label, confidence)
                for captured_at, label, confidence in self._quality_history
                if captured_at >= cutoff
            ]
            return self._summarize_quality(recent)

    def begin_egg_inspection(self) -> None:
        """Discard detections from earlier eggs before inspecting this egg."""
        with self._quality_ready:
            self._quality_history.clear()
            # Exclude an inference already in progress on a frame captured
            # before the load cell announced this egg.
            self._inspection_min_sequence = self._raw_sequence + 1
            self._quality_ready.notify_all()

    def wait_for_egg_quality(
        self,
        timeout: float = 1.0,
        min_samples: int = 3,
    ) -> dict[str, Any] | None:
        """Wait until consecutive frames agree on one quality for this egg."""
        deadline = monotonic() + timeout
        with self._quality_ready:
            while True:
                # Inference stores exactly one observation per frame. Looking
                # only at the tail means a later "no egg", missing detection,
                # or conflicting class invalidates an older partial match.
                recent = list(self._quality_history)[-min_samples:]
                if len(recent) == min_samples:
                    labels = [sample[1] for sample in recent]
                    label = labels[-1]
                    if label in EGG_QUALITY_LABELS and all(
                        candidate == label for candidate in labels
                    ):
                        confidences = [sample[2] for sample in recent]
                        return {
                            "label": label,
                            "confidence": round(max(confidences), 4),
                        }
                if not self._running or self._error:
                    return None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._quality_ready.wait(timeout=remaining)

    @staticmethod
    def _summarize_quality(
        samples: list[tuple[str, float]],
    ) -> dict[str, Any]:
        if not samples:
            return {"label": "unknown", "confidence": 0.0}
        scores: dict[str, float] = {}
        peaks: dict[str, float] = {}
        for label, confidence in samples:
            scores[label] = scores.get(label, 0.0) + confidence
            peaks[label] = max(peaks.get(label, 0.0), confidence)
        label = max(scores, key=scores.get)
        return {"label": label, "confidence": round(peaks[label], 4)}

    @staticmethod
    def _select_frame_observation(
        detections: list[dict[str, Any]],
        inspection_zone: tuple[float, float, float, float] | None = None,
    ) -> tuple[str, float]:
        """Choose the quality only when the egg's box center is in the zone.

        Detections outside the auto-capture-zone bounding box are display-only:
        they cannot reject an egg, authorize weighing, or create a record.
        The smaller centered target is sized for the egg. Its YOLO bounding-box
        center must enter that target, so a partly entered egg cannot trigger
        the crack gate, load cell, or size gates. Requiring the whole detected
        rectangle to fit is unreliable because YOLO boxes vary with egg size
        and camera angle. A frame without a fully centered recognized egg is
        treated as ``no egg``. If overlapping model boxes are present for the
        same egg, a defect still wins over Good for safe sorting.
        """
        recognized: list[tuple[str, float]] = []
        for detection in detections:
            label = normalize_model_label(detection.get("label", ""))
            if label not in EGG_QUALITY_LABELS:
                continue
            if inspection_zone is not None:
                box = detection.get("box", ())
                if len(box) != 4:
                    continue
                left_box, top_box, right_box, bottom_box = (
                    float(value) for value in box
                )
                center_x = (left_box + right_box) / 2
                center_y = (top_box + bottom_box) / 2
                left, top, right, bottom = inspection_zone
                if not (left <= center_x <= right and top <= center_y <= bottom):
                    continue
            recognized.append((label, float(detection.get("confidence", 0.0))))
        if not recognized:
            return NO_EGG_LABEL, 0.0
        # Safety defects win over confidence. In particular, one accepted
        # Crack observation can never be overwritten by later Good frames.
        return max(
            recognized,
            key=lambda item: (
                item[0] == "crack", item[0] == "rotten",
                item[0] in EGG_QUALITY_LABELS, item[1],
            ),
        )

    def set_reject_handler(self, handler: Callable[[str], Any]) -> None:
        with self._lock:
            self._reject_handler = handler



    def set_capture_handler(
        self, handler: Callable[[dict[str, Any]], Any]
    ) -> None:
        with self._lock:
            self._capture_handler = handler

    def _has_passage_occupancy(
        self, detections: list[dict[str, Any]],
        inspection_zone: tuple[float, float, float, float],
        width: float, height: float,
    ) -> bool:
        """Presence-only hysteresis; never provide a quality or start an egg."""
        left, top, right, bottom = inspection_zone
        mx, my = width * self.passage_occupancy_margin, height * self.passage_occupancy_margin
        expanded = (max(0.0, left - mx), max(0.0, top - my),
                    min(width, right + mx), min(height, bottom + my))
        label, _ = self._select_frame_observation(detections, expanded)
        return label in EGG_QUALITY_LABELS

    def _process_auto_capture(
        self, label: str, confidence: float, *, passage_occupied: bool = False
    ) -> None:
        """Send a defect immediately, outside the camera lock; retry while visible."""
        with self._quality_ready:
            completed = self._update_auto_capture(label, confidence, passage_occupied=passage_occupied)
            defect = self._passage_defect
            handler = self._reject_handler
            capture_handler = self._capture_handler
            should_send = defect is not None and not self._passage_reject_sent
            self._quality_ready.notify_all()
        if should_send:
            try:
                if handler is None:
                    raise RuntimeError(
                        "Camera reject command handler is not connected."
                    )
                handler(defect)
            except RuntimeError as exc:
                with self._lock:
                    self._reject_error = f"Channel 0 reject command failed: {exc}"
            else:
                with self._lock:
                    self._passage_reject_sent = True
                    self._reject_error = None
        if completed is not None and capture_handler is not None:
            capture_handler(completed)

    def _update_auto_capture(
        self, label: str, confidence: float, *, passage_occupied: bool = False
    ) -> dict[str, Any] | None:
        """Capture one egg per zone passage; queue Good eggs on exit."""
        self._observe_passage_diagnostic(label, confidence, passage_occupied)
        self._passage_quality_present = label in EGG_QUALITY_LABELS
        if label in EGG_QUALITY_LABELS:
            self._passage_missing_since = None
            if self._active_capture is not None and self._active_capture.get("claimed_on_scale"):
                self._passage_misses = 0
                return None
            if self._active_capture is None:
                self._active_capture = {
                    **self._runtime_context,
                    "capture_id": self._egg_count + 1,
                    "label": label,
                    "confidence": round(confidence, 4),
                    "entered_at": monotonic(),
                }
            self._passage_samples.append((label, confidence))
            self._passage_misses = 0
            if label in {"crack", "rotten"} and self._passage_defect is None:
                self._passage_defect = label
                self._active_capture["label"] = label
                self._active_capture["confidence"] = round(confidence, 4)
            return None

        if self._active_capture is None:
            return None
        if passage_occupied:
            # A box just outside the quality zone can keep this identity alive,
            # but cannot add quality evidence, reject, or authorize a claim.
            self._passage_misses = 0
            self._passage_missing_since = None
            return None
        now = monotonic()
        if self._passage_missing_since is None:
            self._passage_missing_since = now
        self._passage_misses += 1
        if (self._passage_misses < self.passage_exit_samples
                or now - self._passage_missing_since < self.passage_exit_seconds):
            return None

        sample_count = len(self._passage_samples)
        capture = dict(self._active_capture)
        self._observe_exit_diagnostic(capture, sample_count)
        self._passage_samples = []
        self._passage_misses = 0
        self._passage_missing_since = None
        self._active_capture = None
        self._passage_defect = None
        self._passage_reject_sent = False
        if sample_count < self.capture_min_samples and capture["label"] == "good":
            return None

        capture["captured_at"] = monotonic()
        self._egg_count += 1
        self._quality_counts[capture["label"]] += 1
        self._last_capture = dict(capture)
        self._log_passage_diagnostic(
            "exit_counter_increment", capture_id=capture["capture_id"],
            label=capture["label"], counter_before=self._egg_count - 1,
            counter_after=self._egg_count, counter_increment=1,
            before_exit=False,
        )
        # An in-zone scale claim was already delivered. Complete its exit count
        # without queueing it or invoking the recording callback a second time.
        if capture.get("claimed_on_scale"):
            return None
        # Rejected eggs are counted, but never assigned to a load-cell egg.
        if capture["label"] == "good":
            if len(self._captured_qualities) == self._captured_qualities.maxlen:
                raise CameraSessionError("Camera capture queue is full; stop and clear the station.")
            self._captured_qualities.append(dict(capture))
        return capture

    def wait_for_frame(
        self, previous_sequence: int, timeout: float = 2.0
    ) -> tuple[int, bytes | None, bool]:
        with self._frame_ready:
            self._frame_ready.wait_for(
                lambda: (
                    self._frame_sequence != previous_sequence
                    or not self._running
                ),
                timeout=timeout,
            )
            return (
                self._frame_sequence,
                self._latest_jpeg,
                self._running,
            )

    def _capture_loop(self) -> None:
        import cv2

        perf = self._performance if self._performance.enabled else None
        fps_started = monotonic()
        fps_frames = 0
        readiness_sent = False
        last_frame_dimensions = None
        next_mode_check = 0.0
        try:
            backend_codes = {
                "auto": cv2.CAP_ANY,
                "dshow": cv2.CAP_DSHOW,
                "msmf": cv2.CAP_MSMF,
            }
            if self.camera_backend not in backend_codes:
                raise CameraSessionError(
                    "CAMERA_BACKEND must be auto, dshow, or msmf."
                )

            capture = cv2.VideoCapture(
                self.camera_index,
                backend_codes[self.camera_backend],
            )
            if not capture.isOpened():
                capture.release()
                raise CameraSessionError(
                    f"Unable to open camera index {self.camera_index} with "
                    f"the {self.camera_backend} backend. Close other camera "
                    "apps or change CAMERA_INDEX/CAMERA_BACKEND."
                )

            negotiation = configure_camera(
                capture, cv2, backend=self.camera_backend, width=self.camera_width,
                height=self.camera_height, fps=self.camera_fps,
                corrected=self.camera_resolution_negotiation_fix,
                profile=self.camera_capture_profile,
            )
            if perf:
                perf.camera(**negotiation)
            with self._lock:
                self._capture = capture
                self._camera_negotiation = negotiation

            while not self._stop_event.is_set():
                with self._lock:
                    capture = self._capture
                if capture is None:
                    break

                if perf:
                    perf.stage("capture", "read")
                    read_started = monotonic()
                success, frame = capture.read()
                returned_at = monotonic() if perf else None
                if perf:
                    perf.record({"read_ms": (returned_at - read_started) * 1000}, reads=1)
                if self._stop_event.is_set():
                    break
                if not success:
                    raise CameraSessionError(
                        "The camera stopped returning frames."
                    )
                dimensions = tuple(frame.shape[:2])
                native_profile = self.camera_capture_profile == "1080p_yuy2"
                check_at = monotonic() if native_profile else 0.0
                shape_changed = dimensions != last_frame_dimensions
                if shape_changed or (native_profile and check_at >= next_mode_check):
                    last_frame_dimensions = dimensions
                    next_mode_check = check_at + 5.0
                    previous_verified = negotiation.get("mode_verified")
                    negotiation = verify_frame(negotiation, frame, capture, cv2)
                    with self._lock:
                        self._camera_negotiation = negotiation
                    if perf:
                        perf.camera(**negotiation)
                    # Only first frame / resolution changes; never per-frame logging.
                    if native_profile and not negotiation["mode_verified"]:
                        _DIAGNOSTIC_LOG.warning("Camera 1080p YUY2 MODE MISMATCH; stopping capture: %s", json.dumps(negotiation))
                        raise CameraSessionError("1080p YUY2 camera mode failed verification; stop and review camera_negotiation before restarting.")
                    if negotiation["requested_resolution_matches_frame"]:
                        if shape_changed or previous_verified != negotiation["mode_verified"]:
                            _DIAGNOSTIC_LOG.info("Camera resolution verified: %s", json.dumps(negotiation))
                    else:
                        _DIAGNOSTIC_LOG.warning("Camera RESOLUTION MISMATCH; continuing with actual dimensions: %s", json.dumps(negotiation))
                if perf:
                    height, width = frame.shape[:2]
                    perf.record(captures=1)
                    perf.camera(frame_width=int(width), frame_height=int(height),
                                requested_resolution_matches_frame=(width == self.camera_width and height == self.camera_height))
                    perf.stage("capture", "ready_callback_and_frame_handoff")
                if not readiness_sent and self._lifecycle_handler:
                    self._lifecycle_handler("ready", dict(self._runtime_context))
                    readiness_sent = True

                # Give inference only the newest frame. No queue means no
                # increasing detection delay when the model is slower than video.
                with self._raw_frame_ready:
                    self._latest_raw_frame = frame
                    self._latest_raw_returned_at = returned_at
                    self._raw_sequence += 1
                    result = self._latest_result
                    self._raw_frame_ready.notify()

                if perf:
                    perf.stage("capture", "annotation")
                    annotation_started = monotonic()
                display_frame = (
                    annotate_image(frame, result)
                    if result is not None
                    else frame
                )
                if perf:
                    encoding_started = monotonic()
                    perf.record({"annotation_ms": (encoding_started - annotation_started) * 1000})
                    perf.stage("capture", "jpeg_encoding")
                encoded, jpeg = cv2.imencode(
                    ".jpg",
                    display_frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                )
                if perf:
                    perf.record({"jpeg_encode_ms": (monotonic() - encoding_started) * 1000})
                    perf.stage("capture", "publish_jpeg")
                if not encoded:
                    raise CameraSessionError(
                        "OpenCV could not encode the camera frame."
                    )

                fps_frames += 1
                elapsed = monotonic() - fps_started
                with self._frame_ready:
                    if elapsed >= 1.0:
                        self._stream_fps = fps_frames / elapsed
                        fps_started = monotonic()
                        fps_frames = 0
                    self._latest_jpeg = jpeg.tobytes()
                    self._frame_sequence += 1
                    self._frame_ready.notify_all()
                if perf:
                    perf.record(jpegs=1)
        except Exception as exc:
            self._fail(str(exc))
        finally:
            if perf:
                perf.stage("capture", "stopped")
            with self._frame_ready:
                if self._capture is not None:
                    self._capture.release()
                self._capture = None
                self._running = False
                self._capture_thread = None
                self._stop_event.set()
                self._raw_frame_ready.notify_all()
                self._quality_ready.notify_all()
                self._frame_ready.notify_all()

    def _inference_loop(self) -> None:
        processed_sequence = 0
        next_runtime_sample = 0.0
        perf = self._performance if self._performance.enabled else None
        try:
            while not self._stop_event.is_set():
                if perf:
                    perf.stage("inference", "waiting_for_frame")
                with self._raw_frame_ready:
                    self._raw_frame_ready.wait_for(
                        lambda: (
                            self._raw_sequence != processed_sequence
                            or self._stop_event.is_set()
                        ),
                        timeout=1.0,
                    )
                    if self._stop_event.is_set():
                        break
                    if (
                        self._raw_sequence == processed_sequence
                        or self._latest_raw_frame is None
                    ):
                        continue
                    if perf:
                        perf.stage("inference", "copying_frame")
                    frame = self._latest_raw_frame.copy()
                    returned_at = self._latest_raw_returned_at
                    skipped = max(0, self._raw_sequence - processed_sequence - 1)
                    processed_sequence = self._raw_sequence
                    if perf:
                        self._performance_processed_sequence = processed_sequence

                started = monotonic()
                if perf:
                    perf.stage("inference", "detect_image_including_lock_wait")
                    timings = {"_collect_runtime": started >= next_runtime_sample}
                    result = detect_image(frame, performance=timings)
                    runtime = timings.pop("_runtime", None)
                    timings.pop("_collect_runtime", None)
                    if runtime is not None:
                        perf.runtime(runtime)
                        next_runtime_sample = monotonic() + perf.interval
                    perf.record(timings, predictions=1, skipped_raw_frames=skipped)
                    perf.stage("inference", "publishing_result")
                else:
                    result = detect_image(frame)
                elapsed = monotonic() - started
                with self._quality_ready:
                    if self._stop_event.is_set():
                        break
                    width = float(result.get("image_width", frame.shape[1]))
                    height = float(result.get("image_height", frame.shape[0]))
                    inspection_zone = (
                        width * self.inspection_left,
                        height * self.inspection_top,
                        width * self.inspection_right,
                        height * self.inspection_bottom,
                    )
                    result["inspection_zone"] = [
                        round(value) for value in inspection_zone
                    ]
                    self._latest_result = result
                    captured_at = monotonic()
                    label, confidence = self._select_frame_observation(
                        result["detections"], inspection_zone
                    )
                    passage_occupied = self._has_passage_occupancy(
                        result["detections"], inspection_zone, width, height
                    )
                    if processed_sequence >= self._inspection_min_sequence:
                        self._quality_history.append(
                            (captured_at, label, confidence)
                        )
                    self._quality_ready.notify_all()
                    self._inference_ms = elapsed * 1000
                    self._detection_fps = 1 / elapsed if elapsed else 0.0
                if perf:
                    if returned_at is not None:
                        perf.record({"frame_age_at_result_ms": (monotonic() - returned_at) * 1000})
                    perf.stage("inference", "passage_and_callbacks")
                    post_started = monotonic()
                self._process_auto_capture(label, confidence, passage_occupied=passage_occupied)
                if perf:
                    perf.record({"passage_and_callbacks_ms": (monotonic() - post_started) * 1000,
                                 "inference_cycle_ms": (monotonic() - started) * 1000}, inference_cycles=1)
        except Exception as exc:
            self._fail(str(exc))
        finally:
            if perf:
                perf.stage("inference", "stopped")
            with self._lock:
                self._inference_thread = None

    def _fail(self, message: str) -> None:
        with self._frame_ready:
            self._error = message
            self._stop_event.set()
            self._frame_ready.notify_all()
            self._raw_frame_ready.notify_all()
            self._quality_ready.notify_all()
        if self._lifecycle_handler:
            self._lifecycle_handler("failed", dict(self._runtime_context))


CAMERA_SESSION = CameraDetectionSession()
