"""Synthetic camera lifecycle and mocked serial only; no physical hardware."""
import os
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch, call

from camera_session import CameraDetectionSession
from esp32_bridge import Esp32Bridge
from experimental_reject_observer import DryRunRejectObservations, OfflineRejectReplay


class DryRunIntegrationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"CAMERA_REJECT_DRY_RUN": "true",
            "CAMERA_PASSAGE_DIAGNOSTICS": "false", "CAMERA_PASSAGE_EXIT_SECONDS": "0.6"})
        env.start()
        self.addCleanup(env.stop)
        self.now = 1000.0
        clock = patch("camera_session.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.camera = CameraDetectionSession()
        self.camera._running = True
        self.camera._session_ref = "TEST-SESSION"
        self.camera._runtime_context = {"runtime_generation": "generation-1", "password": "never-copy"}
        self.camera.capture_min_samples = 3
        self.bridge = Esp32Bridge()
        self.bridge._connected = True
        self.bridge._serial = Mock()
        self.camera.set_reject_handler(self.bridge.reject_egg)

    def frame(self, at, quality="good", occupied=False):
        self.now = 1000 + at
        self.camera._process_auto_capture(quality, .9, passage_occupied=occupied)

    def passage(self, start, quality):
        for delta in (0, .2, .4):
            self.frame(start + delta, quality)
        self.frame(start + .6, "no egg")
        self.frame(start + 1.21, "no egg")

    def replay(self):
        events, dropped = self.camera.experimental_reject_snapshot()
        return OfflineRejectReplay(events, dropped=dropped)

    def test_default_disabled_produces_no_observations(self):
        with patch.dict(os.environ):
            os.environ.pop("CAMERA_REJECT_DRY_RUN", None)
            camera = CameraDetectionSession()
        camera._update_auto_capture("crack", .9)
        self.assertEqual(camera.experimental_reject_snapshot(), ((), 0))

    def test_repeated_detections_dropout_jitter_keep_one_job_and_legacy_write(self):
        self.frame(0, "crack")
        self.frame(.2, "no egg")
        self.frame(.4, "no egg")
        self.frame(.5, "no egg", occupied=True)
        for t in (.6, .8, 1):
            self.frame(t, "rotten")
        self.frame(1.2, "no egg")
        self.frame(1.81, "no egg")
        events, dropped = self.camera.experimental_reject_snapshot()
        self.assertEqual([e.event for e in events],
            ["passage_begin", "first_defect", "quality_locked", "passage_exit"])
        self.assertEqual({e.capture_id for e in events}, {1})
        self.assertEqual(events[-1].first_defect_at, 1000)
        self.assertEqual(events[-1].quality, "crack")
        self.assertEqual(events[-1].session_ref, "TEST-SESSION")
        self.assertNotIn("password", vars(events[-1]))
        with self.assertRaises(FrozenInstanceError):
            events[-1].quality = "good"
        replay = OfflineRejectReplay(events, dropped=dropped)
        result = replay.accept(1, host_send_at=1002, controller_accept_ms=87, forward_latency_ms=100)
        self.assertEqual(result["controller_deadline"], 6587)
        self.assertEqual(len(replay.jobs), 1)
        self.bridge._serial.write.assert_called_once_with(b"REJECT:CRACK\n")
        # Replays and simulated duplicate/stale ACKs cannot reach the bridge.
        self.assertIs(replay.accept(1, host_send_at=1004, controller_accept_ms=90,
                                    forward_latency_ms=100), result)
        self.assertFalse(replay.client.accept_ack("XA:87654321:0000000000000001:1:999:ACCEPTED"))
        self.bridge._serial.write.assert_called_once_with(b"REJECT:CRACK\n")

    def test_defect_pairs_have_independent_controller_deadlines(self):
        for first, second in (("crack", "crack"), ("crack", "rotten"), ("rotten", "crack")):
            with self.subTest(first=first, second=second):
                self.camera._experimental_observations.reset()
                self.bridge._serial.reset_mock()
                self.passage(1.01, first)
                self.passage(3.98, second)
                replay = self.replay()
                a = replay.accept(1, host_send_at=1001.01, controller_accept_ms=1110, forward_latency_ms=100)
                b = replay.accept(2, host_send_at=1003.98, controller_accept_ms=4080, forward_latency_ms=100)
                self.assertEqual((a["controller_deadline"], b["controller_deadline"]), (7610, 10580))
                self.assertEqual(a["source_age_at_accept_ms"], 100)
                self.assertEqual(self.camera._egg_count % 2, 0)
                self.assertEqual(self.bridge._serial.write.call_args_list,
                                 [call(f"REJECT:{first.upper()}\n".encode()), call(f"REJECT:{second.upper()}\n".encode())])

    def test_good_pair_orders_remain_explicitly_unsafe(self):
        for first, second in (("good", "crack"), ("crack", "good")):
            with self.subTest(first=first):
                self.camera._experimental_observations.reset()
                self.bridge._serial.reset_mock()
                self.passage(0, first)
                self.passage(3, second)
                replay = self.replay()
                with self.assertRaisesRegex(ValueError, "passage order"):
                    replay.accept(2, host_send_at=1005, controller_accept_ms=5000, forward_latency_ms=40)
                a = replay.accept(1, host_send_at=1002, controller_accept_ms=2000, forward_latency_ms=40)
                b = replay.accept(2, host_send_at=1005, controller_accept_ms=5000, forward_latency_ms=40)
                self.assertEqual(b["status"], "UNSAFE_GOOD")
                if first == "crack":
                    self.assertEqual(a["controller_deadline"], 8500)
                else:
                    self.assertEqual(a["status"], "UNSAFE_GOOD")
                self.assertTrue(any("Good-route" in warning for warning in replay.warnings))
                self.bridge._serial.write.assert_called_once_with(b"REJECT:CRACK\n")

    def test_provisional_good_does_not_hide_later_defect(self):
        self.frame(0)
        self.frame(.2, "rotten")
        self.frame(.4, "crack")
        self.frame(.6, "no egg")
        self.frame(1.21, "no egg")
        replay = self.replay()
        self.assertEqual(replay.jobs[1].quality, "rotten")
        self.assertEqual(replay.jobs[1].first_defect_at, 1000.2)
        self.assertEqual(replay.final_qualities, {1: "rotten"})
        self.bridge._serial.write.assert_called_once_with(b"REJECT:ROTTEN\n")

    def test_good_scale_claim_lock_and_exit_do_not_duplicate_job_or_count(self):
        for t in (0, .2, .4):
            self.frame(t)
        capture = self.camera.wait_for_captured_quality(allow_loaded_capture=True)
        self.assertEqual(capture["label"], "good")
        self.assertEqual(self.camera._egg_count, 0)
        self.frame(.5, "crack")  # Existing quality lock remains authoritative.
        self.frame(.6, "no egg")
        self.frame(1.21, "no egg")
        self.assertEqual(self.camera._egg_count, 1)
        self.assertEqual(len(self.replay().jobs), 1)
        self.assertEqual(self.replay().final_qualities, {1: "good"})
        self.bridge._serial.write.assert_not_called()

    def test_short_good_capture_id_reuse_has_separate_observation_identity(self):
        self.frame(0)
        self.frame(.2, "no egg")
        self.frame(.81, "no egg")
        self.passage(2, "crack")
        events, _ = self.camera.experimental_reject_snapshot()
        starts = [e for e in events if e.event == "passage_begin"]
        self.assertEqual([e.capture_id for e in starts], [1, 1])
        self.assertEqual([e.passage_id for e in starts], [1, 2])
        self.assertEqual(len(self.replay().jobs), 2)
        self.assertEqual(self.camera._egg_count, 1)

    def test_run_reset_and_overflow_cannot_silently_replay(self):
        self.passage(0, "crack")
        old, _ = self.camera.experimental_reject_snapshot()
        observer = self.camera._experimental_observations
        old_run = observer.run_id
        observer.reset()
        self.assertNotEqual(observer.run_id, old_run)
        self.passage(2, "crack")
        new, _ = self.camera.experimental_reject_snapshot()
        with self.assertRaisesRegex(ValueError, "ordered|mix"):
            OfflineRejectReplay(old + new)
        small = DryRunRejectObservations(enabled=True, capacity=4)
        for e in old + new:
            small.emit(e.event, {"capture_id": e.capture_id, "label": e.quality}, e.at, "test", "1")
        events, dropped = small.snapshot()
        self.assertGreater(dropped, 0)
        with self.assertRaisesRegex(ValueError, "dropped"):
            OfflineRejectReplay(events, dropped=dropped)

    def test_legacy_retry_failure_is_unchanged_and_observed_once(self):
        self.camera.set_reject_handler(Mock(side_effect=[RuntimeError("offline"), None]))
        self.frame(0, "crack")
        self.frame(.2, "crack")
        self.frame(.4, "crack")
        self.assertEqual(self.camera._reject_handler.call_count, 2)
        self.assertEqual(len(self.replay().jobs), 1)

    def test_firmware_defaults_unchanged(self):
        source = Path("esp32/eggsort_controller/eggsort_controller.ino").read_text()
        for definition in ("#define EGGSORT_EXPERIMENTAL_REJECT 0", "#define CRACK_GATE_OPEN_TIME 6500",
                           "#define CRACK_RETRIGGER_CLOSE_TIME 300"):
            self.assertIn(definition, source)

    def test_offline_controller_rollover_and_latency_are_explicit(self):
        self.passage(0, "crack")
        replay = self.replay()
        result = replay.accept(1, host_send_at=1000.025,
                               controller_accept_ms=0xFFFFFFFF - 2000, forward_latency_ms=80)
        self.assertEqual(result["controller_deadline"], 4499)
        self.assertAlmostEqual(result["source_age_at_accept_ms"], 105)
        self.assertEqual(result["physical_safety"], "UNVERIFIED")

    def test_offline_pending_capacity_does_not_send_or_create_fifth_request(self):
        for i in range(5):
            self.passage(i * 3, "crack")
        writes_before = self.bridge._serial.write.call_count
        replay = self.replay()
        for i in range(1, 5):
            replay.accept(i, host_send_at=1015, controller_accept_ms=15000 + i, forward_latency_ms=10)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            replay.accept(5, host_send_at=1015, controller_accept_ms=15005, forward_latency_ms=10)
        self.assertEqual(self.bridge._serial.write.call_count, writes_before)


if __name__ == "__main__":
    unittest.main()
