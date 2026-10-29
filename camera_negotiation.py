"""Camera property ordering and advisory readback; never resizes frames."""
import math

from camera_performance import camera_readback


def configure_camera(capture, cv2, *, backend, width, height, fps, corrected=True, profile="existing"):
    native = profile == "1080p_yuy2"
    if profile not in {"existing", "1080p_yuy2"}:
        raise ValueError("Unknown camera capture profile")
    if native and (backend, width, height, fps) != ("dshow", 1920, 1080, 30):
        raise ValueError("1080p_yuy2 requires DirectShow 1920x1080 at 30 FPS")
    properties = {
        "fourcc": (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*("YUY2" if native else "MJPG"))),
        "width": (cv2.CAP_PROP_FRAME_WIDTH, width),
        "height": (cv2.CAP_PROP_FRAME_HEIGHT, height),
        "fps": (cv2.CAP_PROP_FPS, fps),
        "buffer_size": (cv2.CAP_PROP_BUFFERSIZE, 1),
    }
    # DSHOW's FPS setter may rebuild the graph, discarding the selected format.
    # Apply it before size/format, with MJPG as the final format negotiation.
    order = ("fps", "width", "height", "fourcc", "buffer_size") if corrected and backend == "dshow" else (
        "fourcc", "width", "height", "fps", "buffer_size")
    if native:
        # Exact successful standalone probe: no extra buffer/property request.
        order = ("fps", "fourcc", "width", "height")
    accepted = {name: bool(capture.set(*properties[name])) for name in order}
    report = camera_readback(capture, cv2, {"width": width, "height": height, "fps": fps})
    report.update(backend_requested=backend, property_order=list(order), set_accepted=accepted,
                  mode="corrected" if corrected and backend == "dshow" else "legacy",
                  resolution_verified=False, profile=profile)
    if native:
        report["requested"]["fourcc"] = "YUY2"
        report["mode"] = "1080p_yuy2"
    try:
        report["backend_actual"] = str(capture.getBackendName())
    except Exception:
        report["backend_actual"] = None
    report["fourcc"] = read_fourcc(capture, cv2)
    return report


def read_fourcc(capture, cv2):
    try:
        value = float(capture.get(cv2.CAP_PROP_FOURCC))
        number = int(value) if math.isfinite(value) and value > 0 else 0
        return "".join(chr((number >> (8 * i)) & 255) for i in range(4)) if number else None
    except Exception:
        return None


def verify_frame(report, frame, capture=None, cv2=None):
    height, width = frame.shape[:2]
    matches = width == report["requested"]["width"] and height == report["requested"]["height"]
    if capture is not None and cv2 is not None:
        report = {**report, "after_frame_readback": camera_readback(capture, cv2, report["requested"]),
                  "fourcc": read_fourcc(capture, cv2)}
    expected_fourcc = report["requested"].get("fourcc")
    format_matches = report.get("fourcc") == expected_fourcc if expected_fourcc else None
    return {**report, "frame_width": int(width), "frame_height": int(height),
            "requested_fourcc_matches": format_matches,
            "mode_verified": matches and (format_matches if expected_fourcc else True),
            "resolution_verified": True, "requested_resolution_matches_frame": matches}
