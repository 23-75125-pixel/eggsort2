import unittest
from unittest.mock import Mock, patch, call

from esp32_bridge import Esp32Bridge, Esp32ProtocolParser


class Esp32BridgeDiagnosticsTests(unittest.TestCase):
    def test_reject_waits_for_controller_startup(self) -> None:
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        with self.assertRaises(RuntimeError):
            bridge.reject_egg("Crack")
        bridge._serial.write.assert_called_once_with(b"STATUS\n")
        bridge._serial.write.reset_mock()
        bridge._publish({"type": "pca9685_status", "ready": True})
        bridge.reject_egg("Rotten")
        bridge._serial.write.assert_called_once_with(b"REJECT:ROTTEN\n")

    def test_missing_status_is_retried_without_flooding(self) -> None:
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        with patch("esp32_bridge.monotonic", side_effect=[0.0, 0.2, 3.1]):
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    bridge.reject_egg("Crack")
        self.assertEqual(bridge._serial.write.call_args_list, [call(b"STATUS\n")] * 2)

    def test_connection_recovers_readiness_without_startup_banner(self) -> None:
        bridge = Esp32Bridge()
        connection = Mock()
        clock = [0.0]
        reads = [b"", b"PCA9685 READY : YES\n"]

        def read_line():
            line = reads.pop(0)
            clock[0] += 3.1
            if not reads:
                bridge._stop_event.set()
            return line

        connection.readline.side_effect = read_line
        serial_module = Mock()
        serial_module.Serial.return_value = connection
        with patch.dict("sys.modules", {"serial": serial_module}), \
                patch.object(bridge, "_find_port", return_value="COM_TEST"), \
                patch("esp32_bridge.monotonic", side_effect=lambda: clock[0]):
            bridge._read_loop()
        self.assertTrue(bridge.status()["diagnostics"]["pca9685_ready"])
        self.assertEqual(connection.write.call_args_list, [call(b"STATUS\n")] * 2)
        connection.close.assert_called_once()

    def test_egg_detected_discards_idle_zero_and_accepts_new_live_weight(self) -> None:
        bridge = Esp32Bridge()
        connection = Mock()
        readings = [
            b"LIVE WEIGHT : 0 g\n",
            b"Egg Detected\n",
            b"LIVE WEIGHT : 64 g\n",
        ]

        def read_line():
            line = readings.pop(0)
            if not readings:
                bridge._stop_event.set()
            return line

        connection.readline.side_effect = read_line
        serial_module = Mock()
        serial_module.Serial.return_value = connection
        with patch.dict("sys.modules", {"serial": serial_module}), \
                patch.object(bridge, "_find_port", return_value="COM_TEST"):
            bridge._read_loop()

        diagnostics = bridge.status()["diagnostics"]
        self.assertFalse(diagnostics["awaiting_egg"])
        self.assertEqual(diagnostics["live_weight_grams"], 64)
        self.assertEqual(connection.write.call_args_list, [call(b"STATUS\n")])

    def test_egg_detected_clears_stale_idle_weight(self) -> None:
        bridge = Esp32Bridge()
        bridge._publish({"type": "load_cell_status", "weight_grams": 0})
        bridge._publish({"type": "egg_detected", "message": "Egg Detected"})
        self.assertIsNone(bridge.status()["diagnostics"]["live_weight_grams"])

    def test_disconnected_and_missing_board_have_distinct_errors(self) -> None:
        bridge = Esp32Bridge()
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            bridge.reject_egg("Crack")
        bridge._connected = True
        bridge._serial = Mock()
        bridge._diagnostics["pca9685_ready"] = False
        with self.assertRaisesRegex(RuntimeError, "PCA9685 not found"):
            bridge.reject_egg("Rotten")
        bridge._serial.write.assert_not_called()

    def test_defects_cannot_start_weighing(self) -> None:
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        for quality in ("Crack", "Rotten"):
            with self.assertRaises(ValueError):
                bridge.measure_egg(quality)
        bridge._serial.write.assert_not_called()

    def test_reject_accepts_only_crack_and_rotten(self) -> None:
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        for quality in ("Good", "Undefined", "no egg"):
            with self.assertRaises(ValueError):
                bridge.reject_egg(quality)
        bridge._serial.write.assert_not_called()

    def test_scale_readings_and_final_weight_are_exposed(self) -> None:
        bridge = Esp32Bridge()

        bridge._publish({"type": "egg_detected", "message": "Egg Detected"})
        bridge._publish({
            "type": "measurement_quality",
            "quality": "Crack",
            "message": "CAMERA QUALITY : CRACK",
        })
        bridge._publish({
            "type": "weight_reading",
            "reading_number": 2,
            "weight_grams": 53,
            "message": "Reading 2: 53 g",
        })
        diagnostics = bridge.status()["diagnostics"]
        self.assertEqual(diagnostics["measurement_reading_number"], 2)
        self.assertEqual(diagnostics["measurement_weight_grams"], 53)
        self.assertEqual(diagnostics["measurement_quality"], "Crack")
        self.assertIsNone(diagnostics["final_weight_grams"])

        bridge._publish({
            "type": "final_weight",
            "weight_grams": 54,
            "message": "FINAL WEIGHT : 54 g",
        })
        diagnostics = bridge.status()["diagnostics"]
        self.assertEqual(diagnostics["final_weight_grams"], 54)
        self.assertEqual(diagnostics["measurement_quality"], "Crack")

    def test_camera_quality_serial_message_is_parsed(self) -> None:
        events = Esp32ProtocolParser().parse("CAMERA QUALITY : GOOD")

        self.assertEqual(events[0]["type"], "measurement_quality")
        self.assertEqual(events[0]["quality"], "Good")

    def test_load_cell_gate_messages_are_parsed_and_retained(self) -> None:
        events = Esp32ProtocolParser().parse("LOAD CELL GATE: OPEN")
        self.assertEqual(events[0]["type"], "load_cell_gate")
        self.assertEqual(events[0]["state"], "OPEN")

        bridge = Esp32Bridge()
        bridge._publish(events[0])
        bridge._publish({"type": "egg_left", "message": "Egg Left"})
        diagnostics = bridge.status()["diagnostics"]
        self.assertEqual(diagnostics["load_cell_gate_state"], "OPEN")
        self.assertIn("opened", diagnostics["last_gate_event"])

    def test_routed_egg_retains_quality_after_leaving_scale(self) -> None:
        bridge = Esp32Bridge()
        bridge._publish({"type": "egg_detected", "message": "Egg Detected"})
        bridge._publish({
            "type": "measurement_quality",
            "quality": "Good",
            "message": "CAMERA QUALITY : GOOD",
        })
        bridge._publish({
            "type": "sort_complete",
            "size": "Medium",
            "message": "SERVO SORTED : MEDIUM",
        })
        bridge._publish({"type": "egg_left", "message": "Egg Left"})

        diagnostics = bridge.status()["diagnostics"]
        self.assertIsNone(diagnostics["measurement_quality"])
        self.assertIn("Good", diagnostics["last_gate_event"])
        self.assertIn("Medium size route complete", diagnostics["last_gate_event"])

    def test_measurement_command_exposes_quality_without_firmware_echo(self) -> None:
        bridge = Esp32Bridge()
        bridge._publish({
            "type": "measurement_command",
            "quality": "Rotten",
            "message": "MEASURE:ROTTEN",
        })

        diagnostics = bridge.status()["diagnostics"]
        self.assertEqual(diagnostics["measurement_quality"], "Rotten")

    def test_next_egg_clears_previous_measurement(self) -> None:
        bridge = Esp32Bridge()
        bridge._publish({
            "type": "final_weight",
            "weight_grams": 60,
            "message": "FINAL WEIGHT : 60 g",
        })
        bridge._publish({
            "type": "measurement_quality",
            "quality": "Good",
            "message": "CAMERA QUALITY : GOOD",
        })
        bridge._publish({"type": "egg_detected", "message": "Egg Detected"})

        diagnostics = bridge.status()["diagnostics"]
        self.assertIsNone(diagnostics["measurement_weight_grams"])
        self.assertIsNone(diagnostics["measurement_reading_number"])
        self.assertIsNone(diagnostics["final_weight_grams"])
        self.assertIsNone(diagnostics["measurement_quality"])
        self.assertFalse(diagnostics["awaiting_egg"])

    def test_egg_left_resets_display_to_waiting(self) -> None:
        bridge = Esp32Bridge()
        bridge._publish({
            "type": "measurement_command",
            "quality": "Good",
            "message": "MEASURE:GOOD",
        })
        bridge._publish({
            "type": "final_weight",
            "weight_grams": 58,
            "message": "FINAL WEIGHT : 58 g",
        })
        bridge._publish({"type": "egg_left", "message": "Egg Left"})

        diagnostics = bridge.status()["diagnostics"]
        self.assertTrue(diagnostics["awaiting_egg"])
        self.assertIsNone(diagnostics["final_weight_grams"])
        self.assertIsNone(diagnostics["measurement_quality"])

    def test_capture_number_quality_and_weight_remain_in_latest_event(self) -> None:
        bridge = Esp32Bridge()
        bridge._connected = True
        bridge._serial = Mock()
        bridge.publish_camera_capture({"capture_id": 7, "label": "good"})
        self.assertIn("Egg #7", bridge.status()["diagnostics"]["latest_sensor_event"])
        bridge._publish({"type": "egg_detected", "message": "Egg Detected"})
        bridge.measure_egg("Good", capture_id=7)
        bridge._publish({"type": "final_weight", "weight_grams": 64})
        bridge._publish({"type": "load_cell_gate", "state": "OPEN"})
        bridge._publish({"type": "sort_complete", "size": "Large"})
        bridge._publish({"type": "egg_left", "message": "Egg Left"})
        latest = bridge.status()["diagnostics"]["latest_sensor_event"]
        self.assertIn("Egg #7", latest)
        self.assertIn("Good", latest)
        self.assertIn("64 g", latest)
        self.assertIn("Large", latest)


if __name__ == "__main__":
    unittest.main()
