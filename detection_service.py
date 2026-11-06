"""YOLO inference helpers for uploaded and live OpenCV frames."""

from __future__ import annotations

from pathlib import Path
import math
import sys
from threading import Lock
from time import monotonic
from typing import Any

from config import env_float, env_int, env_text

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = Path(env_text("YOLO_MODEL_PATH", "best.pt")).expanduser()
if not MODEL_PATH.is_absolute():
    MODEL_PATH = BASE_DIR / MODEL_PATH
CONFIDENCE = env_float("YOLO_CONFIDENCE", 0.25, minimum=0.0, maximum=1.0)
IMAGE_SIZE = env_int("YOLO_IMAGE_SIZE", 512, minimum=32, maximum=4096)
CPU_INFERENCE_THREADS = env_int("EGGSORT_CPU_INFERENCE_THREADS", 0, minimum=0, maximum=64)
MAX_FRAME_BYTES = 5 * 1024 * 1024
_model: Any | None = None
_model_lock = Lock()
_inference_lock = Lock()


class DetectorUnavailableError(RuntimeError):
    """Raised when dependencies or model weights are unavailable."""


class InvalidFrameError(ValueError):
    """Raised when an uploaded frame cannot be decoded."""


def _performance_details(result, model, performance, prediction_ms):
    """Best effort only; never import torch, synchronize devices, or set threads."""
    try:
        speed = getattr(result, "speed", None)
        if isinstance(speed, dict):
            stages = {}
            for name in ("preprocess", "inference", "postprocess"):
                value = speed.get(name)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    stages[name] = value
                    performance[f"yolo_{name}_ms"] = value
            if len(stages) == 3:
                total = sum(stages.values())
                performance["yolo_stages_total_ms"] = total
                # Signed residual: profiled intervals are not a separate exact stopwatch.
                performance["prediction_minus_yolo_stages_ms"] = prediction_ms - total
    except Exception:
        pass
    if performance.pop("_collect_runtime", False):
        runtime = {"device": None, "intraop_threads": None, "interop_threads": None}
        try:
            runtime["device"] = str(model.predictor.device)
        except Exception:
            pass
        torch = sys.modules.get("torch")
        if torch is not None:
            for key, name in (("intraop_threads", "get_num_threads"), ("interop_threads", "get_num_interop_threads")):
                try:
                    runtime[key] = int(getattr(torch, name)())
                except Exception:
                    pass
        runtime["sampled_monotonic_s"] = monotonic()
        runtime["configured_intraop_threads"] = CPU_INFERENCE_THREADS or None
        runtime["thread_limit_verified"] = (runtime["intraop_threads"] == CPU_INFERENCE_THREADS
                                            if CPU_INFERENCE_THREADS else None)
        performance["_runtime"] = runtime


def _load_model() -> Any:
    global _model

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        if not MODEL_PATH.is_file():
            raise DetectorUnavailableError(
                f"YOLO model not found at {MODEL_PATH}. Add best.pt to the "
                "project root or set YOLO_MODEL_PATH."
            )

        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise DetectorUnavailableError(
                "Ultralytics is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            verify_threads = None
            if CPU_INFERENCE_THREADS:
                from cpu_inference_threads import initialize_cpu_threads
                verify_threads = initialize_cpu_threads(CPU_INFERENCE_THREADS)
            model = YOLO(str(MODEL_PATH))
            if verify_threads is not None:
                # on_predict_start runs after setup_model/select_device and
                # before preprocessing/warmup/forward. Batch-end checks again.
                model.add_callback("on_predict_start", verify_threads)
                model.add_callback("on_predict_batch_end", verify_threads)
            _model = model
        except Exception as exc:
            raise DetectorUnavailableError(
                f"Unable to load YOLO model: {exc}"
            ) from exc

    return _model


def detector_info() -> dict[str, Any]:
    """Load and report the exact class names embedded in the configured model."""
    model = _load_model()
    names = model.names
    if isinstance(names, dict):
        ordered_names = tuple(
            str(names[index]) for index in sorted(names, key=int)
        )
    else:
        ordered_names = tuple(str(name) for name in names)

    return {
        "path": str(MODEL_PATH.resolve()),
        "class_count": len(ordered_names),
        "classes": list(ordered_names),
        "confidence": CONFIDENCE,
        "image_size": IMAGE_SIZE,
    }


def detect_image(frame: Any, *, performance: dict | None = None) -> dict[str, Any]:
    """Run YOLO on an OpenCV image and return JSON-safe detections."""
    timing_started = monotonic() if performance is not None else 0.0
    model = _load_model()
    lock_requested = monotonic() if performance is not None else 0.0
    try:
        with _inference_lock:
            prediction_started = monotonic() if performance is not None else 0.0
            result = model.predict(
                source=frame,
                conf=CONFIDENCE,
                imgsz=IMAGE_SIZE,
                verbose=False,
            )[0]
            prediction_finished = monotonic() if performance is not None else 0.0
    except Exception as exc:
        raise DetectorUnavailableError(f"YOLO inference failed: {exc}") from exc

    names = result.names
    detections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    if result.boxes is not None:
        boxes = result.boxes.xyxy.cpu().tolist()
        confidences = result.boxes.conf.cpu().tolist()
        class_ids = result.boxes.cls.cpu().tolist()

        for coordinates, confidence, class_id_value in zip(
            boxes, confidences, class_ids
        ):
            class_id = int(class_id_value)
            if isinstance(names, dict):
                label = str(names.get(class_id, class_id))
            else:
                label = str(names[class_id])

            counts[label] = counts.get(label, 0) + 1
            detections.append(
                {
                    "box": [round(float(value), 2) for value in coordinates],
                    "class_id": class_id,
                    "label": label,
                    "confidence": round(float(confidence), 4),
                }
            )

    height, width = frame.shape[:2]
    if performance is not None:
        finished = monotonic()
        performance.update(
            model_load_ms=(lock_requested - timing_started) * 1000,
            inference_lock_wait_ms=(prediction_started - lock_requested) * 1000,
            prediction_ms=(prediction_finished - prediction_started) * 1000,
            result_conversion_ms=(finished - prediction_finished) * 1000,
            detection_total_ms=(finished - timing_started) * 1000,
        )
        _performance_details(result, model, performance, performance["prediction_ms"])
    return {
        "detections": detections,
        "counts": counts,
        "total": len(detections),
        "image_width": width,
        "image_height": height,
        "confidence_threshold": CONFIDENCE,
    }


def detect_frame(frame_bytes: bytes) -> dict[str, Any]:
    """Decode one JPEG/PNG frame and return JSON-safe YOLO detections."""
    if not frame_bytes:
        raise InvalidFrameError("The uploaded camera frame is empty.")
    if len(frame_bytes) > MAX_FRAME_BYTES:
        raise InvalidFrameError("The camera frame exceeds the 5 MB limit.")

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise DetectorUnavailableError(
            "OpenCV and NumPy are not installed. Run: pip install -r requirements.txt"
        ) from exc

    encoded_frame = np.frombuffer(frame_bytes, dtype=np.uint8)
    frame = cv2.imdecode(encoded_frame, cv2.IMREAD_COLOR)
    if frame is None:
        raise InvalidFrameError("The uploaded file is not a valid image frame.")

    return detect_image(frame)


def annotate_image(frame: Any, result: dict[str, Any]) -> Any:
    """Draw YOLO boxes and labels onto a copy of an OpenCV image."""
    try:
        import cv2
    except ImportError as exc:
        raise DetectorUnavailableError(
            "OpenCV is not installed. Run: pip install -r requirements.txt"
        ) from exc

    annotated = frame.copy()
    inspection_zone = result.get("inspection_zone")
    if inspection_zone and len(inspection_zone) == 4:
        left, top, right, bottom = (int(value) for value in inspection_zone)
        zone_color = (255, 255, 0)
        cv2.rectangle(annotated, (left, top), (right, bottom), zone_color, 3)
        zone_label = "AUTO CAPTURE ZONE"
        zone_scale = 1.0
        zone_thickness = 2
        (zone_text_width, zone_text_height), zone_baseline = cv2.getTextSize(
            zone_label, cv2.FONT_HERSHEY_SIMPLEX, zone_scale, zone_thickness
        )
        zone_label_left = left + 6
        zone_label_top = top + 6
        cv2.rectangle(
            annotated,
            (zone_label_left, zone_label_top),
            (
                zone_label_left + zone_text_width + 16,
                zone_label_top + zone_text_height + zone_baseline + 14,
            ),
            (20, 45, 45),
            -1,
        )
        cv2.putText(
            annotated,
            zone_label,
            (zone_label_left + 8, zone_label_top + zone_text_height + 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            zone_scale,
            zone_color,
            zone_thickness,
            cv2.LINE_AA,
        )
    for detection in result["detections"]:
        x1, y1, x2, y2 = (int(value) for value in detection["box"])
        class_id = detection["class_id"]
        color = (
            int((class_id * 83 + 40) % 255),
            int((class_id * 47 + 150) % 255),
            int((class_id * 113 + 220) % 255),
        )
        label = (
            f"{detection['label']} "
            f"{round(detection['confidence'] * 100)}%"
        )

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        label_scale = 1.15
        label_thickness = 2
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, label_scale, label_thickness
        )
        label_height = text_height + baseline + 12
        label_left = min(x1, max(0, annotated.shape[1] - text_width - 16))
        label_top = y1 - label_height if y1 >= label_height else y1
        cv2.rectangle(
            annotated,
            (label_left, label_top),
            (label_left + text_width + 16, label_top + label_height),
            color,
            -1,
        )
        cv2.putText(
            annotated,
            label,
            (label_left + 8, label_top + text_height + 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            label_scale,
            (20, 24, 32),
            label_thickness,
            cv2.LINE_AA,
        )

    return annotated
