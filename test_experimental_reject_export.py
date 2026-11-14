"""File export tests use temporary directories, synthetic events, and no hardware."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from camera_session import CameraDetectionSession
from experimental_reject_export import RejectObservationExporter, load_observation_export
from experimental_reject_observer import DryRunRejectObservations, OfflineRejectReplay


class RejectExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def writer(self, **kwargs):
        writer = RejectObservationExporter(directory=self.root, **kwargs)
        self.addCleanup(writer.close)
        return writer

    def observer(self, writer):
        observer = DryRunRejectObservations(enabled=True)
        observer._exporter = writer
        return observer

    def passage(self, observer, capture_id=1, quality="crack"):
        capture = {"capture_id": capture_id, "label": quality,
                   "password": "DO_NOT_EXPORT", "unrelated_personal_data": "DO_NOT_EXPORT"}
        for index, event in enumerate(("passage_begin", "first_defect", "quality_locked", "passage_exit")):
            if quality == "good" and event == "first_defect":
                continue
            observer.emit(event, capture, 1000.0 + index, "TEST", "generation-1")

    def files(self):
        return sorted(self.root.glob("*.jsonl"))

    def test_export_off_by_default_and_requires_collection(self):
        with patch("experimental_reject_export.RejectObservationExporter") as factory:
            DryRunRejectObservations(enabled=True)
            DryRunRejectObservations(export_enabled=True)
            factory.assert_not_called()
        with patch.dict(os.environ, {"CAMERA_REJECT_DRY_RUN": "true"}):
            os.environ.pop("CAMERA_REJECT_DRY_RUN_EXPORT", None)
            camera = CameraDetectionSession()
            self.assertIsNone(camera._experimental_observations._exporter)

    def test_save_all_fields_valid_json_and_offline_replay(self):
        writer = self.writer()
        observer = self.observer(writer)
        self.passage(observer)
        expected, _ = observer.snapshot()
        observer.finish()
        writer.close()
        self.assertEqual(len(self.files()), 1)
        loaded = load_observation_export(self.files()[0])
        self.assertEqual(loaded, expected)
        self.assertEqual(loaded[-1].first_defect_at, 1001.0)
        self.assertEqual(loaded[-1].quality, "crack")
        self.assertEqual(len(OfflineRejectReplay(loaded).jobs), 1)
        text = self.files()[0].read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_EXPORT", text)
        self.assertNotIn("password", text)
        for line in text.splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_restart_saves_old_run_and_creates_distinct_new_file(self):
        writer = self.writer()
        observer = self.observer(writer)
        self.passage(observer)
        old_run = observer.run_id
        observer.reset()
        self.passage(observer, quality="good")
        observer.finish()
        writer.close()
        self.assertEqual(len(self.files()), 2)
        loaded = [load_observation_export(path) for path in self.files()]
        self.assertEqual({events[0].run_id for events in loaded}, {old_run, observer.run_id})
        self.assertEqual({events[0].capture_id for events in loaded}, {1})

    def test_actual_camera_stop_start_hooks_do_not_write_extra_rejects(self):
        writer = self.writer()
        env = {"CAMERA_REJECT_DRY_RUN": "true", "CAMERA_REJECT_DRY_RUN_EXPORT": "true",
               "CAMERA_PASSAGE_DIAGNOSTICS": "false"}
        with patch.dict(os.environ, env), patch("experimental_reject_export.RejectObservationExporter", return_value=writer):
            camera = CameraDetectionSession()
        rejects = Mock()
        camera.set_reject_handler(rejects)
        def frames():
            with patch("camera_session.monotonic", return_value=100) as clock:
                camera._process_auto_capture("crack", .9)
                camera._process_auto_capture("crack", .9)
                clock.return_value = 101
                camera._process_auto_capture("no egg", 0)
                clock.return_value = 112
                camera._process_auto_capture("no egg", 0)
        frames()
        camera.stop()
        with patch("camera_session.detector_info", return_value={}), patch("camera_session.Thread") as threads:
            threads.return_value.is_alive.return_value = False
            camera.start(context={"session_ref": "TEST", "runtime_generation": "2"})
            frames()
            camera.stop()
        writer.close()
        self.assertEqual(rejects.call_count, 2)  # Exactly one legacy command per run.
        self.assertEqual(len(self.files()), 2)
        for path in self.files():
            self.assertTrue(load_observation_export(path))

    def test_normal_shutdown_drains_complete_run_without_explicit_finish(self):
        writer = self.writer()
        self.passage(self.observer(writer), quality="good")
        writer.close()
        self.assertTrue(load_observation_export(self.files()[0]))
        footer = json.loads(self.files()[0].read_text().splitlines()[-1])
        self.assertEqual(footer["reason"], "application_shutdown")

    def test_unfinished_passage_is_saved_but_refused_for_validated_replay(self):
        writer = self.writer()
        observer = self.observer(writer)
        observer.emit("passage_begin", {"capture_id": 1, "label": "good"}, 1, None, None)
        observer.finish()
        writer.close()
        footer = json.loads(self.files()[0].read_text().splitlines()[-1])
        self.assertFalse(footer["complete"])
        self.assertEqual(footer["unfinished_passage_ids"], [1])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_observation_export(self.files()[0])

    def test_truncated_and_missing_footer_exports_are_rejected(self):
        writer = self.writer()
        self.passage(self.observer(writer))
        writer.close()
        lines = self.files()[0].read_text().splitlines()
        damaged = self.root / "damaged.jsonl"
        damaged.write_text("\n".join(lines[:-1]) + "\n")
        with self.assertRaises(ValueError):
            load_observation_export(damaged)
        damaged.write_text("\n".join(lines[:-1]) + '\n{"type":')
        with self.assertRaises(json.JSONDecodeError):
            load_observation_export(damaged)

    def test_in_memory_snapshot_overflow_does_not_lose_exported_events(self):
        writer = self.writer()
        observer = self.observer(writer)
        observer._events = __import__("collections").deque(maxlen=4)
        self.passage(observer)
        self.passage(observer, capture_id=2)
        observer.finish()
        writer.close()
        self.assertGreater(observer.dropped, 0)
        self.assertEqual(len(load_observation_export(self.files()[0])), 8)

    def test_slow_writer_and_queue_overflow_do_not_block_camera_producer(self):
        release, entered = Event(), Event()
        original = RejectObservationExporter._write_loop
        def held(writer):
            entered.set()
            release.wait(2)
            original(writer)
        with patch.object(RejectObservationExporter, "_write_loop", held):
            writer = self.writer(capacity=1)
            self.assertTrue(entered.wait(1))
            observer = self.observer(writer)
            self.passage(observer)  # Returns while writer is still held.
            self.assertGreater(writer.dropped, 0)
            self.assertEqual(observer.sequence, 4)
            release.set()
            writer.close()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_observation_export(self.files()[0])

    def test_file_failure_is_reported_without_payload_or_camera_exception(self):
        writer = self.writer()
        with patch.object(Path, "open", side_effect=OSError("DO_NOT_EXPORT")):
            with self.assertLogs("experimental_reject_export", level="WARNING") as logs:
                observer = self.observer(writer)
                self.passage(observer)
                writer.close()
        self.assertEqual(writer.error, "OSError")
        self.assertNotIn("DO_NOT_EXPORT", " ".join(logs.output))
        self.assertEqual(observer.sequence, 4)
        self.assertFalse(writer.submit(observer.snapshot()[0][0]))

    def test_shutdown_without_events_creates_no_file(self):
        writer = self.writer()
        writer.close()
        self.assertEqual(self.files(), [])

    def test_failure_after_header_leaves_detectably_incomplete_file(self):
        writer = self.writer()
        original = json.dumps
        def fail_after_header(record, **kwargs):
            if record.get("type") == "observation":
                raise OSError("DO_NOT_EXPORT")
            return original(record, **kwargs)
        with patch("experimental_reject_export.json.dumps", side_effect=fail_after_header):
            with self.assertLogs("experimental_reject_export", level="WARNING"):
                self.passage(self.observer(writer))
                writer.close()
        self.assertEqual(writer.error, "OSError")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_observation_export(self.files()[0])


if __name__ == "__main__":
    unittest.main()
