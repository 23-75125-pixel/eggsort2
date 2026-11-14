"""Execute the unchanged C++ core on a PC/WSL. No hardware or live-app imports."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experimental_reject_client import ExperimentalRejectClient
from experimental_reject_export import RejectObservationExporter, load_observation_export
from experimental_reject_observer import DryRunRejectObservations, PassageObservation
from offline_reject_replay import Assumptions, NativeLoopback, build_runner, replay


NOMINAL = Assumptions(4510, 4860, True, 0, 0, 100)


def events_for(*qualities, separation=2.97):
    events = []
    for pid, quality in enumerate(qualities, 1):
        at = 1.01 + (pid - 1) * separation
        defect = at if quality != "good" else None
        kinds = ["passage_begin", "quality_locked", "passage_exit"]
        if defect is not None:
            kinds.insert(1, "first_defect")
        for kind in kinds:
            events.append(PassageObservation(1, "TEST", "1", len(events) + 1, pid, pid,
                          kind, at + (0.1 if kind == "passage_exit" else 0), quality,
                          defect if kind != "passage_begin" else None))
    return events


class NativeReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        # Compile the runner AND all existing static_assert tests. Missing compiler fails clearly.
        cls.runner = build_runner(Path(cls.temp.name) / "runner")

    def native(self, assumptions=NOMINAL):
        native = NativeLoopback(self.runner, assumptions)
        self.addCleanup(native.close)
        client = ExperimentalRejectClient(enabled=True, run_id=1)
        self.assertTrue(client.accept_hello(native.exchange(0, client.hello_frame())["reply"]))
        self.assertTrue(client.accept_ack(native.exchange(0, client.begin_frame())["reply"]))
        return native, client

    def submit(self, native, client, pid, at, quality="crack"):
        frame = client.request_frame(pid, quality, detected_at=at / 1000, now=at / 1000)
        row = native.exchange(at, frame)
        self.assertTrue(client.accept_ack(row["reply"]))
        return row

    def test_defect_pairs_real_core_separate_closes_and_immutable_deadlines(self):
        for pair in (("crack", "crack"), ("crack", "rotten"), ("rotten", "crack")):
            with self.subTest(pair=pair):
                report = replay(events_for(*pair), self.runner, assumptions=NOMINAL,
                                controller_origin_ms=1010, tick_ms=10)
                closes = [r for r in report["timeline"] if r.get("motion") == "CLOSE"]
                opens = [r for r in report["timeline"] if r.get("motion") == "OPEN"]
                self.assertEqual([r["controller_ms"] for r in closes], [7510, 10480])
                self.assertEqual([r["controller_ms"] for r in opens], [1010, 7970])
                self.assertEqual([report["client_records"][i]["controller_deadline"] for i in (1, 2)], [7510, 10480])
                self.assertTrue(all(r["ack_accepted"] for r in closes))

    def test_real_core_movement_boundaries_450_close_180_open(self):
        native, client = self.native()
        self.submit(native, client, 1, 1010)
        self.assertEqual(native.exchange(1010, "TICK")["state"], "OPENING")
        self.assertEqual(native.exchange(1189, "TICK")["state"], "OPENING")
        self.assertEqual(native.exchange(1190, "TICK")["state"], "HOLDING")
        self.submit(native, client, 2, 3980)
        self.assertEqual(native.exchange(7510, "TICK")["motion"], "CLOSE")
        self.assertEqual(native.exchange(7810, "TICK")["state"], "CLOSING_RELEASE")
        self.assertEqual(native.exchange(7959, "TICK")["state"], "CLOSING_RELEASE")
        self.assertEqual(native.exchange(7960, "TICK")["state"], "CLOSED_CLEARANCE")
        self.assertEqual(native.exchange(7960, "TICK")["motion"], "OPEN")

    def test_good_pair_orders_block_without_cancelling_active_close(self):
        for pair in (("good", "crack"), ("crack", "good")):
            with self.subTest(pair=pair):
                report = replay(events_for(*pair), self.runner, assumptions=NOMINAL,
                                controller_origin_ms=1010, tick_ms=10)
                self.assertEqual(report["final_state"]["fault"], "UNSAFE_GOOD")
                closes = [r for r in report["timeline"] if r.get("motion") == "CLOSE"]
                self.assertEqual(len(closes), 0 if pair[0] == "good" else 1)
                if closes:
                    self.assertEqual(closes[0]["controller_ms"], 7510)

    def test_unknown_bounds_default_to_no_motion(self):
        report = replay(events_for("crack", "rotten"), self.runner, tick_ms=10)
        self.assertFalse(any(r.get("motion") in {"OPEN", "CLOSE"} for r in report["timeline"]))
        self.assertEqual(report["client_records"][1]["status"], "UNVERIFIED_TIMING")

    def test_retry_and_duplicate_id_do_not_restart_deadline(self):
        native, client = self.native()
        self.submit(native, client, 1, 1010)
        native.exchange(1010, "TICK")
        frame = client.retry_frame(1, now=1.1)
        duplicate = native.exchange(1100, frame)
        self.assertTrue(client.accept_ack(duplicate["reply"]))
        self.assertTrue(duplicate["reply"].endswith(":7510:DUPLICATE"))
        self.assertEqual(duplicate["active_deadline"], 7510)
        conflict = native.exchange(1100, frame.replace(":C:", ":R:"))
        self.assertTrue(conflict["reply"].endswith(":7510:CONFLICT"))
        self.assertTrue(client.accept_ack(conflict["reply"]))
        self.assertFalse(client.accept_ack(duplicate["reply"]))  # Terminal CONFLICT cannot revive.
        self.assertEqual(native.exchange(7510, "TICK")["motion"], "CLOSE")

    def test_spacing_conflict_preserves_active_close(self):
        native, client = self.native()
        self.submit(native, client, 1, 1010)
        native.exchange(1010, "TICK")
        row = self.submit(native, client, 2, 1200)
        self.assertTrue(row["reply"].endswith(":SPACING_CONFLICT"))
        self.assertEqual(row["active_deadline"], 7510)
        self.assertEqual(native.exchange(7510, "TICK")["motion"], "CLOSE")

    def test_queued_expiry_and_unknown_clearance(self):
        # Arrival/route bounds assumed solely to exercise queue; clearance remains UNKNOWN.
        native, client = self.native(Assumptions(4510, 4860, True))
        self.submit(native, client, 1, 1010)
        native.exchange(1010, "TICK")
        self.submit(native, client, 2, 3980)
        native.exchange(7510, "TICK")
        native.exchange(7960, "TICK")
        blocked = native.exchange(7960, "TICK")
        self.assertTrue(blocked["reply"].endswith(":UNVERIFIED_CLEARANCE"))
        expired = native.exchange(10480, "TICK")
        self.assertTrue(expired["reply"].endswith(":10480:EXPIRED"))
        self.assertEqual(expired["motion"], "NONE")
        self.assertTrue(client.accept_ack(expired["reply"]))

    def test_controller_overflow_and_host_capacity_are_distinct(self):
        native, client = self.native(Assumptions(4510, 4860, True))
        self.submit(native, client, 1, 1000)
        native.exchange(1000, "TICK")
        for pid in range(2, 5):
            self.submit(native, client, pid, 1000 + pid)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            client.request_frame(5, "crack", detected_at=1.005, now=1.005)
        # Explicit controller robustness input bypasses the host ONLY in this test.
        five = native.exchange(1005, "XR:12345678:0000000000000001:5:C:0")
        self.assertTrue(five["reply"].endswith(":QUEUED"))
        six = native.exchange(1006, "XR:12345678:0000000000000001:6:R:0")
        self.assertTrue(six["reply"].endswith(":QUEUE_FULL"))
        self.assertFalse(client.accept_ack(six["reply"]))
        self.assertEqual(native.exchange(7500, "TICK")["motion"], "CLOSE")

    def test_rollover_real_responses_and_actions(self):
        report = replay(events_for("crack"), self.runner, assumptions=NOMINAL,
                        controller_origin_ms=0xFFFFFFFF - 2000, tick_ms=10)
        self.assertEqual(report["client_records"][1]["controller_deadline"], 4499)
        close = next(r for r in report["timeline"] if r.get("motion") == "CLOSE")
        self.assertEqual(close["controller_ms"], 4499)

    def test_source_age_late_service_and_stale_boot(self):
        native, client = self.native()
        stale = native.exchange(100, "XR:12345678:0000000000000001:1:C:101")
        self.assertTrue(stale["reply"].endswith(":STALE"))
        wrong_boot = native.exchange(100, "XR:87654321:0000000000000001:2:C:0")
        self.assertEqual(wrong_boot["reply"], "XE:INVALID_OR_STALE_BOOT")
        self.assertFalse(client.accept_ack(wrong_boot["reply"]))
        self.submit(native, client, 2, 1010)
        native.exchange(1010, "TICK")
        late = native.exchange(7520, "TICK")
        self.assertEqual(late["motion"], "CLOSE")
        self.assertTrue(late["reply"].endswith(":7510:DEADLINE_MISSED"))

    def test_export_loader_to_real_cpp_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = RejectObservationExporter(directory=directory)
            observer = DryRunRejectObservations(enabled=True)
            observer._exporter = writer
            for e in events_for("crack", "rotten"):
                observer.emit(e.event, {"capture_id": e.capture_id, "label": e.quality}, e.at, "TEST", "1")
            observer.finish()
            writer.close()
            path = next(Path(directory).glob("*.jsonl"))
            loaded = load_observation_export(path)
            report = replay(loaded, self.runner, assumptions=NOMINAL, tick_ms=10)
            self.assertEqual(len([r for r in report["timeline"] if r.get("motion") == "CLOSE"]), 2)
            self.assertEqual(report["passages"][2]["final_quality"], "rotten")
            json.dumps(report, allow_nan=False)
            path.write_text('{"type":"run_start"}\n')
            with self.assertRaises(ValueError):
                load_observation_export(path)

    def test_capture_id_reuse_and_first_defect_time_are_preserved(self):
        events = events_for("crack", "rotten")
        from dataclasses import replace
        events = [replace(e, capture_id=1) for e in events]
        report = replay(events, self.runner, assumptions=NOMINAL, tick_ms=10,
                        latency_ms=50, queue_delay_ms=20, retry_after_ms=100)
        self.assertEqual([report["passages"][i]["capture_id"] for i in (1, 2)], [1, 1])
        self.assertAlmostEqual(report["passages"][2]["first_defect_at"], 3.98)
        self.assertEqual(report["client_records"][1]["controller_deadline"], 6570)
        self.assertTrue(any(r.get("reply", "").endswith(":DUPLICATE") for r in report["timeline"]))
        # Source age in the frame is host queuing only; one-way latency shifts acceptance.
        first = next(r for r in report["timeline"] if r.get("input", "").startswith("XR:"))
        self.assertIn(int(first["input"].rsplit(":", 1)[1]), (19, 20))

    def test_invalid_simulation_input_fails_before_transport(self):
        with self.assertRaises(ValueError):
            replay(events_for("crack"), self.runner, latency_ms=-1)
        with self.assertRaises(ValueError):
            replay(events_for("crack")[1:], self.runner)
        with self.assertRaises(ValueError):
            Assumptions(clearance_ms=-1).config_line()

    def test_cli_demo_default_block_and_assumed_timing_report(self):
        command = [sys.executable, "-B", "offline_reject_replay.py", "--no-build", "--runner", str(self.runner),
                   "--demo", "crack-rotten", "--tick-ms", "10"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["input_source"], "SYNTHETIC DEMO")
        self.assertEqual(report["client_records"]["1"]["status"], "UNVERIFIED_TIMING")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            flags = ["--assume-arrival-ms", "4510", "4860", "--assume-route-clear",
                     "--assume-clearance-ms", "0", "--max-source-age-ms", "100", "--output", str(path)]
            result = subprocess.run(command + flags, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(path.read_text())
            self.assertEqual(len([r for r in report["timeline"] if r.get("motion") == "CLOSE"]), 2)
            original = path.read_bytes()
            result = subprocess.run(command + flags, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
