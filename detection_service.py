"""YOLO inference helpers for uploaded and live OpenCV frames."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

from config import env_float, env_int, env_text

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = Path(env_text("YOLO_MODEL_PATH", "best.pt")).expanduser()
if not MODEL_PATH.is_absolute():
    MODEL_PATH = BASE_DIR / MODEL_PATH
CONFIDENCE = env_float("YOLO_CONFIDENCE", 0.25, minimum=0.0, maximum=1.0)
IMAGE_SIZE = env_int("YOLO_IMAGE_SIZE", 512, minimum=32, maximum=4096)
MAX_FRAME_BYTES = 5 * 1024 * 1024
EXPECTED_MODEL_NAMES = (
    "Crack",
    "Good",
    "Rotten",
)

_model: Any | None = None
_model_lock = Lock()
_inference_lock = Lock()


class DetectorUnavailableError(RuntimeError):
    """Raised when dependencies or model weights are unavailable."""


class InvalidFrameError(ValueError):
    """Raised when an uploaded frame cannot be decoded."""


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
            _model = YOLO(str(MODEL_PATH))
        except Exception as exc:
            raise DetectorUnavailableError(
                f"Unable to load YOLO model: {exc}"
            ) from exc

    return _model


def detector_info() -> dict[str, Any]:
    """Load and validate the exact model used by a camera session."""
    model = _load_model()
    names = model.names
    if isinstance(names, dict):
        ordered_names = tuple(
            str(names[index]) for index in sorted(names, key=int)
        )
    else:
        ordered_names = tuple(str(name) for name in names)

    if ordered_names != EXPECTED_MODEL_NAMES:
        raise DetectorUnavailableError(
            "The configured YOLO model has unexpected classes. Expected "
            f"{list(EXPECTED_MODEL_NAMES)}, received {list(ordered_names)}."
        )

    return {
        "path": str(MODEL_PATH.resolve()),
        "class_count": len(ordered_names),
        "classes": list(ordered_names),
        "confidence": CONFIDENCE,
        "image_size": IMAGE_SIZE,
    }


def detect_image(frame: Any) -> dict[str, Any]:
    """Run YOLO on an OpenCV image and return JSON-safe detections."""
    model = _load_model()
    try:
        with _inference_lock:
            result = model.predict(
                source=frame,
                conf=CONFIDENCE,
                imgsz=IMAGE_SIZE,
                verbose=False,
            )[0]
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
