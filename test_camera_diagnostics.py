"""File-only diagnostic tests; no camera, model, serial, or database access."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import camera_session
from camera_session import CameraDetectionSession


class CameraDiagnosticJsonlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "camera.jsonl"
        environment = patch.dict(os.environ, {
            "CAMERA_PASSAGE_DIAGNOSTICS": "true",
            "CAMERA_PASSAGE_DIAGNOSTICS_FRAMES": "true",
            "CAMERA_PASSAGE_DIAGNOSTICS_FILE": str(self.path),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.session = CameraDetectionSession()
        self.session.capture_min_samples = 3
        self.session.passage_exit_samples = 2
        self.session._session_ref = "SES-test"
        self.clock = 100.0
        timer = patch("camera_session.monotonic", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        original = CameraDetectionSession._update_auto_capture

        def timed_observation(session, *args, **kwargs):
            self.clock += 0.7
            return original(session, *args, **kwargs)

        observations = patch.object(CameraDetectionSession, "_update_auto_capture", timed_observation)
        observations.start()
        self.addCleanup(observations.stop)

    def records(self):
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        lines = raw.decode("utf-8").splitlines()
        self.assertTrue(lines)
        records = [json.loads(line) for line in lines]
        for record in records:
            self.assertIsInstance(record, dict)
            for key in ("event", "monotonic_s", "utc", "session_ref",
                        "capture_id", "counter_before", "counter_after"):
                self.assertIn(key, record)
        return records

    def feed(self, labels):
        for label in labels:
            self.session._update_auto_capture(label, .9 if label != "no egg" else 0)

    def test_default_disabled_does_not_create_file(self):
        with patch.dict(os.environ):
            os.environ.pop("CAMERA_PASSAGE_DIAGNOSTICS", None)
            session = CameraDetectionSession()
        session._log_passage_diagnostic("disabled")
        self.assertFalse(session.passage_diagnostics)
        self.assertFalse(self.path.exists())

    def test_long_event_is_one_complete_json_object_without_console_capture(self):
        note = "rotation \u2022 egg\nnext observation " * 1000
        with patch.object(camera_session._DIAGNOSTIC_LOG, "warning") as console:
            self.session._log_passage_diagnostic("long_record", note=note, capture_id=7)
        console.assert_not_called()
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertGreater(self.path.stat().st_size, 30000)
        self.assertEqual(records[0]["note"], note)
        self.assertEqual(records[0]["capture_id"], 7)

    def test_exit_durations_misses_and_actual_counters_survive_serialization(self):
        now = [100.0]
        with patch.object(camera_session, "monotonic", side_effect=lambda: now[0]):
            self.feed(["good"] * 3)
            now[0] = 100.2
            self.feed(["no egg"])
            now[0] = 100.3
            self.feed(["good"])
            now[0] = 100.5
            self.feed(["no egg"])
            now[0] = 101.2
            self.feed(["no egg"])
            self.session.wait_for_captured_quality(0)
        records = self.records()
        self.assertTrue({"passage_begin", "quality_detection", "missing_begin",
                         "missing_observation", "detection_reacquired", "passage_exit",
                         "exit_counter_increment", "good_capture_claim"}.issubset(
                             {r["event"] for r in records}))
        exit_record = next(r for r in records if r["event"] == "passage_exit")
        self.assertEqual(exit_record["capture_id"], 1)
        self.assertEqual(exit_record["missing_ms"], 700)
        self.assertEqual(exit_record["since_last_accepted_ms"], 900)
        self.assertEqual(exit_record["consecutive_misses"], 2)
        self.assertEqual(exit_record["disposition"], "completed")
        self.assertTrue(exit_record["counter_after_is_expected"])
        actual = next(r for r in records if r["event"] == "exit_counter_increment")
        self.assertEqual((actual["counter_before"], actual["counter_after"]), (0, 1))
        claim = next(r for r in records if r["event"] == "good_capture_claim")
        self.assertEqual(claim["source"], "completed_queue")
        self.assertFalse(claim["before_exit"])
        self.assertEqual(claim["counter_increment"], 0)
        self.assertEqual((claim["counter_before"], claim["counter_after"]), (1, 1))

    def test_visible_claim_counts_only_on_exit_and_logs_actual_delta(self):
        self.feed(["good"] * 3)
        capture = self.session.wait_for_captured_quality(0, allow_loaded_capture=True)
        self.assertTrue(capture["claimed_on_scale"])
        self.assertEqual(self.session._egg_count, 0)
        self.feed(["no egg"] * 2)
        records = self.records()
        claim = next(r for r in records if r["event"] == "good_capture_claim")
        self.assertTrue(claim["before_exit"])
        self.assertEqual(claim["source"], "visible_loaded_capture")
        self.assertEqual((claim["counter_before"], claim["counter_after"]), (0, 0))
        exit_record = next(r for r in records if r["event"] == "passage_exit")
        self.assertEqual(exit_record["disposition"], "claimed_completed")
        self.assertEqual((exit_record["counter_before"], exit_record["counter_after"]), (0, 1))
        self.assertTrue(any(r["event"] == "exit_counter_increment" for r in records))
        self.assertEqual(self.session._egg_count, 1)

    def test_concurrent_append_preserves_all_lines_and_existing_records(self):
        self.session._log_passage_diagnostic("existing")
        other = CameraDetectionSession()
        def write(i):
            session = self.session if i % 2 else other
            session._log_passage_diagnostic("concurrent", observation=i, boxes=[list(range(30))] * 10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(240)))
        records = self.records()
        self.assertEqual(len(records), 241)
        self.assertEqual(records[0]["event"], "existing")
        self.assertEqual({r["observation"] for r in records[1:]}, set(range(240)))

    def test_environment_and_runtime_secrets_are_not_dumped(self):
        secret = "credential-sentinel-must-not-be-recorded"
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": secret}):
            self.session._runtime_context = {"session_ref": "SES-test", "password": secret}
            self.feed(["good"] * 3 + ["no egg"] * 2)
        self.records()
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("password", text)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", text)

    def test_file_failure_warns_once_without_interrupting_existing_count(self):
        self.session.passage_diagnostic_file = self.path / "missing-parent.jsonl"
        with patch.object(camera_session._DIAGNOSTIC_LOG, "warning") as warning:
            self.feed(["good"] * 3 + ["no egg"] * 2)
        warning.assert_called_once()
        self.assertEqual(self.session._egg_count, 1)
        self.assertEqual(len(self.session._captured_qualities), 1)
        self.assertIsNone(self.session._error)


if __name__ == "__main__":
    unittest.main()
