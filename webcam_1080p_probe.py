"""Manual webcam-only 1080p probe. No EGGSor+, YOLO, serial, or dotenv imports."""
from __future__ import annotations

import argparse
import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic


def readback(capture, cv2):
    result = {}
    for name, prop in (("width", cv2.CAP_PROP_FRAME_WIDTH), ("height", cv2.CAP_PROP_FRAME_HEIGHT),
                       ("fps", cv2.CAP_PROP_FPS), ("fourcc_code", cv2.CAP_PROP_FOURCC)):
        try:
            value = float(capture.get(prop))
            result[name] = value if math.isfinite(value) else None
        except Exception:
            result[name] = None
    code = result["fourcc_code"]
    result["fourcc"] = "".join(chr((int(code) >> (8 * i)) & 255) for i in range(4)) if code and code > 0 else None
    return result


def attempt(cv2, *, index, backend, fourcc, order, seconds, warmup, clock=monotonic):
    """Reject wrong dimensions/format; never resize or accept a fallback mode."""
    row = {"backend": backend, "requested": {"width": 1920, "height": 1080, "fps": 30, "fourcc": fourcc},
           "order": order, "status": "FAILED", "snapshots": [], "frames": 0}
    capture = None
    sample = None
    try:
        capture = cv2.VideoCapture(index, {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}[backend])
        if not capture.isOpened():
            row["reason"] = "Device did not open"
            return row, None
        row["snapshots"].append({"after": "open", **readback(capture, cv2)})
        properties = {"fps": (cv2.CAP_PROP_FPS, 30), "width": (cv2.CAP_PROP_FRAME_WIDTH, 1920),
                      "height": (cv2.CAP_PROP_FRAME_HEIGHT, 1080),
                      "fourcc": (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))}
        row["set_accepted"] = {}
        for name in order:
            row["set_accepted"][name] = bool(capture.set(*properties[name]))
            row["snapshots"].append({"after": name, **readback(capture, cv2)})
        opened = clock()
        measured = None
        first_completed = last_completed = None
        max_gap = 0.0
        while True:
            success, frame = capture.read()
            now = clock()
            if not success:
                row["reason"] = "Camera read failed"
                return row, None
            height, width = frame.shape[:2]
            row["actual_frame"] = {"width": int(width), "height": int(height)}
            actual = readback(capture, cv2)
            row["after_read"] = actual
            if (width, height) != (1920, 1080):
                row["reason"] = "Resolution mismatch; fallback rejected"
                return row, None
            if actual["fourcc"] != fourcc:
                row["reason"] = "FourCC mismatch or unavailable; fallback rejected"
                return row, None
            if now - opened < warmup:
                continue
            if measured is None:
                measured = now
                sample = frame  # Retain one original unannotated frame, no copies/queue.
            row["frames"] += 1
            if last_completed is not None:
                max_gap = max(max_gap, now - last_completed)
            else:
                first_completed = now
            last_completed = now
            if now - measured >= seconds:
                break
        duration = last_completed - first_completed
        row.update(duration_s=duration,
                   delivered_fps=(row["frames"] - 1) / duration if duration else 0,
                   max_frame_gap_ms=max_gap * 1000)
        # Explicit screening tolerance, not proof of an exact native 30 FPS mode.
        row["status"] = "PASS" if 29 <= row["delivered_fps"] <= 31 else "FPS_MISMATCH"
        row["reason"] = "1080p and requested FourCC observed; FPS screening tolerance is 29-31"
        return row, sample
    except Exception as exc:
        row["reason"] = f"Probe exception: {type(exc).__name__}"
        return row, None
    finally:
        if capture is not None:
            capture.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--backend", choices=("dshow", "msmf"), default="dshow")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.seconds < 5 or args.warmup < 0 or args.index < 0:
        parser.error("seconds must be >=5, warmup >=0, index >=0")
    import cv2  # Only explicit CLI execution imports the webcam library.

    output = args.output or Path(tempfile.gettempdir()) / "eggsort-webcam-probes" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=True)
    report = {"utc": datetime.now(timezone.utc).isoformat(), "opencv": cv2.__version__,
              "scope": "Targeted mode probing, NOT complete native capability enumeration",
              "fps_pass_tolerance": "29-31 measured FPS for a 30 FPS request",
              "fourcc_evidence": "OpenCV backend readback, not an independent capture-pin inspection",
              "attempts": []}
    destination = output / "report.json"
    print("Webcam-only test: close Flask and other camera applications first. Ctrl+C stops the test.")
    try:
        # Explicitly identified format alternatives, MJPG always tested first.
        for fourcc in ("MJPG", "YUY2"):
            for order in (("fps", "fourcc", "width", "height"), ("fps", "width", "height", "fourcc")):
                row, sample = attempt(cv2, index=args.index, backend=args.backend, fourcc=fourcc,
                                      order=order, seconds=args.seconds, warmup=args.warmup)
                if sample is not None:
                    image = output / f"{len(report['attempts']) + 1}-{fourcc}.png"
                    try:
                        if cv2.imwrite(str(image), sample):
                            row["sample"] = image.name
                        else:
                            row["sample_error"] = "PNG write failed"
                    except Exception:
                        row["sample_error"] = "PNG write failed"
                report["attempts"].append(row)
                destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
                print(json.dumps({k: v for k, v in row.items() if k not in {"snapshots", "set_accepted"}}))
                if row["status"] == "PASS":
                    print(f"Candidate passed screening; inspect sample and report: {destination}")
                    return 0
        print(f"No candidate passed. No live settings changed. Report: {destination}")
        return 1
    except KeyboardInterrupt:
        print(f"Stopped by user. Completed attempts: {destination}")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
