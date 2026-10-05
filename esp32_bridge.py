"""Background serial integration for the EggSort ESP32 controller."""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, Callable

from config import env_int, env_text
from egg_standards import classify_egg_size, servo_command


EventHandler = Callable[[dict[str, Any]], None]


class Esp32ProtocolParser:
    """Parse the human-readable EggSort ESP32 serial protocol."""

    READING = re.compile(r"Reading\s+(\d+)\s*:\s*(-?\d+)\s*g", re.I)
    WEIGHT = re.compile(r"WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    FINAL_WEIGHT = re.compile(r"FINAL WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    SIZE = re.compile(r"SIZE\s*:\s*([A-Z _]+)", re.I)
    SORTED = re.compile(r"SERVO SORTED\s*:\s*([A-Z _]+)", re.I)
    LIVE_WEIGHT = re.compile(r"LIVE WEIGHT\s*:\s*(-?\d+)\s*g", re.I)
    HX711_READY = re.compile(r"HX711 READY\s*:\s*(YES|NO)", re.I)
    PCA9685_READY = re.compile(r"PCA9685 READY\s*:\s*(YES|NO)", re.I)
    LOAD_CELL_GATE = re.compile(r"LOAD CELL GATE\s*:\s*(OPEN|CLOSED)", re.I)
    CONTROLLER_STATE = re.compile(r"CONTROLLER STATE\s*:\s*(.+)", re.I)
    CAMERA_QUALITY = re.compile(
        r"CAMERA QUALITY\s*:\s*(CRACK|GOOD|ROTTEN)", re.I
    )
    MEASUREMENT_FAILED = re.compile(r"MEASURE FAILED\s*:\s*(.+)", re.I)

    def __init__(self) -> None:
        self.final_weight: int | None = None
        self.readings: list[int] = []

    def parse(self, line: str) -> list[dict[str, Any]]:
        clean = line.strip()
        if not clean or set(clean) == {"="}:
            return []

        lowered = clean.lower()
        if lowered == "egg sorting ready":
            return [{"type": "ready", "message": clean}]
        hx711_ready = self.HX711_READY.fullmatch(clean)
        if hx711_ready:
            return [{
                "type": "hx711_status",
                "ready": hx711_ready.group(1).upper() == "YES",
                "message": clean,
            }]
        pca9685_ready = self.PCA9685_READY.fullmatch(clean)
        if pca9685_ready:
            return [{
                "type": "pca9685_status",
                "ready": pca9685_ready.group(1).upper() == "YES",
                "message": clean,
            }]
        load_cell_gate = self.LOAD_CELL_GATE.fullmatch(clean)
        if load_cell_gate:
            return [{
                "type": "load_cell_gate",
                "state": load_cell_gate.group(1).upper(),
                "message": clean,
            }]
        live_weight = self.LIVE_WEIGHT.fullmatch(clean)
        if live_weight:
            return [{
                "type": "load_cell_status",
                "weight_grams": int(live_weight.group(1)),
                "message": clean,
            }]
        # Backward-compatible support for controller firmware that still
        # reports WEIGHT instead of the numbered Reading protocol.
        weight = self.WEIGHT.fullmatch(clean)
        if weight:
            value = int(weight.group(1))
            self.readings = [*self.readings[-1:], value]
            return [{
                "type": "weight_reading",
                "reading_number": len(self.readings),
                "weight_grams": value,
                "message": clean,
            }]
        controller_state = self.CONTROLLER_STATE.fullmatch(clean)
        if controller_state:
            return [{
                "type": "controller_state",
                "state": controller_state.group(1).strip(),
                "message": clean,
            }]
        camera_quality = self.CAMERA_QUALITY.fullmatch(clean)
        if camera_quality:
            return [{
                "type": "measurement_quality",
                "quality": camera_quality.group(1).title(),
                "message": clean,
            }]
        measurement_failed = self.MEASUREMENT_FAILED.fullmatch(clean)
        if measurement_failed:
            return [{
                "type": "measurement_failed",
                "reason": measurement_failed.group(1).strip(),
                "message": clean,
            }]
        if lowered == "egg detected":
            self.final_weight = None
            self.readings = []
            return [{"type": "egg_detected", "message": clean}]
        if lowered == "egg left":
            # Removal is never treated as a completed measurement. Only the
            # controller's explicit FINAL WEIGHT + SIZE pair may advance an
            # egg to sorting and persistence.
            self.final_weight = None
            self.readings = []
            return [{"type": "egg_left", "message": clean}]

        reading = self.READING.fullmatch(clean)
        if reading:
            value = int(reading.group(2))
            self.readings = [*self.readings[-2:], value]
            return [{
                "type": "weight_reading",
                "reading_number": int(reading.group(1)),
                "weight_grams": value,
                "message": clean,
            }]

        final_weight = self.FINAL_WEIGHT.fullmatch(clean)
        if final_weight:
            self.final_weight = int(final_weight.group(1))
            return [{
                "type": "final_weight",
                "weight_grams": self.final_weight,
                "message": clean,
            }]

        size = self.SIZE.fullmatch(clean)
        if size:
            event = {
                "type": "egg_complete",
                "weight_grams": self.final_weight,
                "size": size.group(1).strip().replace("_", " ").title(),
                "readings": self.readings.copy(),
                "message": clean,
            }
            self.final_weight = None
            self.readings = []
            return [event]

        sorted_size = self.SORTED.fullmatch(clean)
        if sorted_size:
            return [{
                "type": "sort_complete",
                "size": sorted_size.group(1).strip().replace("_", " ").title(),
                "message": clean,
            }]

        return [{"type": "serial_message", "message": clean}]

    @staticmethod
    def _classify_size(weight_grams: int) -> str:
        return classify_egg_size(weight_grams)


class Esp32Bridge:
    """Maintain a reconnecting controller link without blocking Flask."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._serial: Any | None = None
        self._handler: EventHandler | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._running = False
        self._connected = False
        self._port: str | None = None
        self._error: str | None = None
        self._last_command: str | None = None
        self._last_status_request_at: float | None = None
        self._diagnostics: dict[str, Any] = {
            "hx711_ready": None,
            "pca9685_ready": None,
            "live_weight_grams": None,
            "measurement_weight_grams": None,
            "measurement_reading_number": None,
            "final_weight_grams": None,
            "measurement_quality": None,
            "capture_id": None,
            "awaiting_egg": True,
            "controller_state": None,
            "load_cell_gate_state": None,
            "last_gate_event": None,
            "latest_sensor_event": None,
        }
        self.baud_rate = env_int(
            "ESP32_BAUD_RATE", 115200, minimum=1200, maximum=2_000_000
        )

    def set_event_handler(self, handler: EventHandler) -> None:
        self._handler = handler

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()
            self._stop_event.clear()
            self._running = True
            self._error = None
            self._thread = Thread(
                target=self._read_loop,
                name="eggsort-esp32-reader",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            serial_connection = self._serial
        if serial_connection is not None:
            try:
                serial_connection.close()
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=3)
        with self._lock:
            self._running = False
            self._connected = False
            self._thread = None
            self._serial = None
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "connected": self._connected,
                "port": self._port,
                "baud_rate": self.baud_rate,
                "controller": "ESP32",
                "error": self._error,
                "last_command": self._last_command,
                "diagnostics": dict(self._diagnostics),
                "latest_event": self._events[-1] if self._events else None,
            }

    def sort_egg(self, size: str) -> str:
        command = servo_command(size)
        self._send_command(command)
        self._publish({
            "type": "servo_command",
            "size": size,
            "message": command,
        })
        return command

    def reject_egg(self, quality: str) -> str:
        quality_code = quality.strip().upper()
        if quality_code not in {"CRACK", "ROTTEN"}:
            raise ValueError(f"Unsupported reject quality: {quality}")
        command = f"REJECT:{quality_code}"
        with self._lock:
            if not self._connected or self._serial is None:
                raise RuntimeError(
                    "ESP32 is disconnected; reconnect its USB serial link."
                )
            readiness = self._diagnostics["pca9685_ready"]
            if readiness is False:
                raise RuntimeError(
                    "ESP32 reports PCA9685 not found. Check its power and I2C wiring."
                )
            if readiness is None:
                self._request_hardware_status()
                raise RuntimeError(
                    "Requesting ESP32 hardware status; retrying while egg is "
                    "visible."
                )
            self._send_command(command)
        self._publish({
            "type": "reject_command", "quality": quality_code.title(),
            "message": f"{command} sent: channel 0, 10-second hold; skip weighing.",
        })
        return command

    def measure_egg(self, quality: str, capture_id: int | None = None) -> str:
        quality_code = quality.upper().replace(" ", "_")
        if quality_code != "GOOD":
            raise ValueError(f"Unsupported egg quality: {quality}")
        command = f"MEASURE:{quality_code}"
        self._send_command(command)
        self._publish({
            "type": "measurement_command",
            "quality": quality,
            "capture_id": capture_id,
            "message": command,
        })
        return command

    def publish_camera_capture(self, capture: dict[str, Any]) -> None:
        """Expose the counted one-shot zone capture on the sensor display."""
        self._publish({
            "type": "camera_capture",
            "capture_id": capture["capture_id"],
            "quality": str(capture["label"]).title(),
            "message": f"Egg #{capture['capture_id']} exited auto capture zone.",
        })

    def publish_status(self, message: str, event_type: str = "flow_status") -> None:
        """Expose application-coordinator state in the hardware status feed."""
        self._publish({"type": event_type, "message": message})

    def _request_hardware_status(self, force: bool = False) -> None:
        """Recover missed boot diagnostics without flooding the serial command queue."""
        with self._lock:
            now = monotonic()
            if (
                not force
                and self._last_status_request_at is not None
                and now - self._last_status_request_at < 3.0
            ):
                return
            self._send_command("STATUS")
            self._last_status_request_at = now

    def _send_command(self, command: str) -> None:
        with self._lock:
            connection = self._serial
            if not self._connected or connection is None:
                raise RuntimeError(
                    "The ESP32 controller is not connected; command "
                    "was not sent."
                )
            try:
                connection.write(f"{command}\n".encode("ascii"))
                connection.flush()
                self._last_command = command
            except Exception as exc:
                self._connected = False
                self._error = str(exc)
                try:
                    connection.close()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Unable to send {command} to the ESP32: {exc}"
                ) from exc

    def advance_gate(self) -> None:
        self._send_command("ADVANCE")
        self._publish({
            "type": "gate_command",
            "message": "ADVANCE",
        })

    def _find_port(self) -> str:
        configured = env_text("ESP32_PORT")
        if configured:
            return configured

        from serial.tools import list_ports

        ports = list(list_ports.comports())
        controller_markers = (
            "esp32",
            "cp210",
            "ch340",
            "ch341",
            "usb serial",
            "silicon labs",
        )
        candidates = []
        for port in ports:
            description = (
                f"{port.description} {port.manufacturer or ''} "
                f"{port.hwid or ''}"
            ).lower()
            if any(marker in description for marker in controller_markers):
                candidates.append(port.device)
        if not candidates and len(ports) == 1:
            candidates = [ports[0].device]
        if not candidates:
            raise RuntimeError(
                "No ESP32 controller found. Connect it by USB or set "
                "ESP32_PORT (for example COM5)."
            )
        return candidates[0]

    def _read_loop(self) -> None:
        parser = Esp32ProtocolParser()
        while not self._stop_event.is_set():
            try:
                import serial

                port = self._find_port()
                connection = serial.Serial(
                    port,
                    self.baud_rate,
                    timeout=0.5,
                )
                with self._lock:
                    self._serial = connection
                    self._port = port
                    self._connected = True
                    self._error = None
                    self._diagnostics["pca9685_ready"] = None
                    self._diagnostics["hx711_ready"] = None
                    self._last_status_request_at = None

                # Opening a port may attach to an already running ESP32, or
                # its reset banner may be lost. Do not depend on that banner.
                self._request_hardware_status(force=True)
                while not self._stop_event.is_set():
                    with self._lock:
                        needs_status = self._diagnostics["pca9685_ready"] is None
                        waiting_for_capture = (
                            not self._diagnostics["awaiting_egg"]
                            and self._diagnostics["measurement_quality"] is None
                        )
                    if needs_status or waiting_for_capture:
                        self._request_hardware_status()
                    raw = connection.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    for event in parser.parse(line):
                        self._publish(event)
                        if event.get("type") == "ready":
                            self._request_hardware_status(force=True)
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._serial = None
                    self._error = str(exc)
                if not self._stop_event.wait(2):
                    continue
            finally:
                with self._lock:
                    connection = self._serial
                    self._serial = None
                    self._connected = False
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

        with self._lock:
            self._running = False

    def _publish(self, event: dict[str, Any]) -> None:
        event = {
            **event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            event_type = event.get("type")
            if event_type == "hx711_status":
                self._diagnostics["hx711_ready"] = event.get("ready")
            elif event_type == "pca9685_status":
                self._diagnostics["pca9685_ready"] = event.get("ready")
            elif event_type == "camera_capture":
                capture_id = event.get("capture_id")
                quality = event.get("quality")
                outcome = (
                    "rejected at camera gate"
                    if quality in {"Crack", "Rotten"}
                    else "pending load-cell weighing"
                )
                self._diagnostics["latest_sensor_event"] = (
                    f"Egg #{capture_id} - {quality} - {outcome}"
                )
            elif event_type == "load_cell_status":
                self._diagnostics["live_weight_grams"] = event.get(
                    "weight_grams"
                )
            elif event_type == "egg_detected":
                self._diagnostics["live_weight_grams"] = None
                self._diagnostics["measurement_weight_grams"] = None
                self._diagnostics["measurement_reading_number"] = None
                self._diagnostics["final_weight_grams"] = None
                self._diagnostics["measurement_quality"] = None
                self._diagnostics["capture_id"] = None
                self._diagnostics["awaiting_egg"] = False
                self._diagnostics["load_cell_gate_state"] = None
                self._diagnostics["last_gate_event"] = None
                self._diagnostics["latest_sensor_event"] = (
                    "Egg on load cell; waiting for its zone exit capture"
                )
            elif event_type == "egg_left":
                self._diagnostics["live_weight_grams"] = None
                if self._diagnostics["final_weight_grams"] is None:
                    self._diagnostics["latest_sensor_event"] = (
                        "Egg left load cell without a completed weight"
                    )
                self._diagnostics["measurement_weight_grams"] = None
                self._diagnostics["measurement_reading_number"] = None
                self._diagnostics["final_weight_grams"] = None
                self._diagnostics["measurement_quality"] = None
                self._diagnostics["capture_id"] = None
                self._diagnostics["awaiting_egg"] = True
            elif event_type in {"measurement_quality", "measurement_command"}:
                self._diagnostics["measurement_quality"] = event.get(
                    "quality"
                )
                self._diagnostics["awaiting_egg"] = False
                if event_type == "measurement_command":
                    self._diagnostics["capture_id"] = event.get("capture_id")
                    egg_id = self._diagnostics["capture_id"]
                    label = f"Egg #{egg_id}" if egg_id is not None else "Egg"
                    self._diagnostics["latest_sensor_event"] = (
                        f"{label} - {event.get('quality')} - weighing on load cell"
                    )
            elif event_type == "measurement_failed":
                self._diagnostics["latest_sensor_event"] = (
                    f"ESP32 measurement rejected: {event.get('reason')}"
                )
            elif event_type == "weight_reading":
                weight = event.get("weight_grams")
                self._diagnostics["live_weight_grams"] = weight
                # A scale can report its weight before the camera validates
                # the Good egg. Keep that raw reading visible, but do not
                # present it as an authorized measurement or open the gate.
                if self._diagnostics["measurement_quality"] is None:
                    self._diagnostics["latest_sensor_event"] = (
                        f"Egg on load cell: {weight} g - waiting for a "
                        "validated Good auto-capture; gate is locked"
                    )
                else:
                    self._diagnostics["measurement_weight_grams"] = weight
                    self._diagnostics["measurement_reading_number"] = event.get(
                        "reading_number"
                    )
                    self._diagnostics["final_weight_grams"] = None
                    self._diagnostics["awaiting_egg"] = False
                    egg_id = self._diagnostics["capture_id"]
                    label = f"Egg #{egg_id}" if egg_id is not None else "Egg"
                    self._diagnostics["latest_sensor_event"] = (
                        f"{label} - {self._diagnostics['measurement_quality']} - "
                        f"reading {event.get('reading_number')}: {weight} g - "
                        "waiting for 2 consecutive readings"
                    )
            elif event_type == "final_weight":
                self._diagnostics["measurement_weight_grams"] = event.get(
                    "weight_grams"
                )
                self._diagnostics["final_weight_grams"] = event.get(
                    "weight_grams"
                )
                self._diagnostics["awaiting_egg"] = False
                egg_id = self._diagnostics["capture_id"]
                label = f"Egg #{egg_id}" if egg_id is not None else "Egg"
                quality = self._diagnostics["measurement_quality"] or "Unknown"
                self._diagnostics["last_gate_event"] = (
                    f"{label} - {quality} - {event.get('weight_grams')} g - "
                    "waiting for load-cell gate confirmation"
                )
                self._diagnostics["latest_sensor_event"] = self._diagnostics[
                    "last_gate_event"
                ]
            elif event_type == "load_cell_gate":
                state = event.get("state")
                self._diagnostics["load_cell_gate_state"] = state
                egg_id = self._diagnostics["capture_id"]
                label = f"Egg #{egg_id}" if egg_id is not None else "Egg"
                quality = self._diagnostics["measurement_quality"] or "Unknown"
                weight = self._diagnostics["final_weight_grams"]
                weight_text = f"{weight} g" if weight is not None else "weight pending"
                self._diagnostics["last_gate_event"] = (
                    f"{label} - {quality} - {weight_text} - load-cell gate opened"
                    if state == "OPEN"
                    else f"{label} - {quality} - {weight_text} - load-cell gate closed"
                )
                self._diagnostics["latest_sensor_event"] = self._diagnostics[
                    "last_gate_event"
                ]
            elif event_type == "sort_complete":
                egg_id = self._diagnostics["capture_id"]
                label = f"Egg #{egg_id}" if egg_id is not None else "Egg"
                quality = self._diagnostics["measurement_quality"] or "Unknown"
                weight = self._diagnostics["final_weight_grams"]
                weight_text = f"{weight} g" if weight is not None else "weight unknown"
                self._diagnostics["last_gate_event"] = (
                    f"{label} - {quality} - {weight_text} - "
                    f"{event.get('size', 'selected')} size route complete"
                )
                self._diagnostics["latest_sensor_event"] = self._diagnostics[
                    "last_gate_event"
                ]
            elif event_type == "controller_state":
                self._diagnostics["controller_state"] = event.get("state")
            self._events.append(event)
        handler = self._handler
        if handler is None:
            return
        try:
            handler(event)
        except Exception as exc:
            # Application/database failures must not be treated as serial
            # disconnects. Keep the controller reader alive and expose the
            # failure through diagnostics for the status UI.
            failure = {
                "type": "event_handler_error",
                "message": f"Unable to process {event.get('type')}: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with self._lock:
                self._events.append(failure)
                self._diagnostics["latest_sensor_event"] = failure["message"]


ESP32_BRIDGE = Esp32Bridge()
