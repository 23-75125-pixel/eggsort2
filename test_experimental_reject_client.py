import unittest

from experimental_reject_client import ExperimentalRejectClient


class ExperimentalRejectClientTests(unittest.TestCase):
    def client(self, **kwargs):
        c = ExperimentalRejectClient(enabled=True, run_id=1, **kwargs)
        self.assertEqual(c.hello_frame(), "XH")
        self.assertTrue(c.accept_hello("XH:12345678"))
        self.assertEqual(c.begin_frame(), "XB:12345678:0000000000000001")
        self.assertTrue(c.accept_ack("XA:12345678:0000000000000001:0:0:STARTED"))
        return c

    def ack(self, client, capture_id, deadline, status):
        return client.accept_ack(f"XA:12345678:0000000000000001:{capture_id}:{deadline}:{status}")

    def test_disabled_by_default_and_never_falls_back_to_legacy(self):
        c = ExperimentalRejectClient()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            c.hello_frame()
        c = ExperimentalRejectClient(enabled=True, run_id=1)
        self.assertFalse(c.accept_hello("XE:EXPERIMENT_DISABLED"))
        with self.assertRaises(RuntimeError):
            c.request_frame(1, "crack", detected_at=1, now=1)

    def test_both_defects_get_distinct_tagged_frames(self):
        c = self.client()
        for capture_id, quality in ((1, "Crack"), (2, "Rotten")):
            frame = c.request_frame(capture_id, quality, detected_at=1000, now=1000)
            self.assertEqual(frame, f"XR:12345678:0000000000000001:{capture_id}:{quality[0]}:0")
            self.assertLessEqual(len(frame), 80)

    def test_retry_preserves_identity_original_detection_and_controller_deadline(self):
        c = self.client()
        c.request_frame(1, "crack", detected_at=1000, now=1000)
        self.assertTrue(self.ack(c, 1, 7510, "ACCEPTED"))
        retry = c.request_frame(1, "crack", detected_at=1001, now=1001)
        self.assertEqual(retry, "XR:12345678:0000000000000001:1:C:1000")
        self.assertEqual(c.record(1).detected_at, 1000)
        self.assertEqual(c.record(1).controller_deadline, 7510)
        self.assertTrue(self.ack(c, 1, 7510, "DUPLICATE"))
        self.assertFalse(self.ack(c, 1, 14010, "ACCEPTED"))
        self.assertEqual(c.record(1).controller_deadline, 7510)

    def test_good_is_an_explicit_unverified_notice_not_a_safe_timer(self):
        c = self.client()
        frame = c.request_frame(1, "good", detected_at=1, now=1)
        self.assertIn(":1:G:", frame)
        self.assertTrue(self.ack(c, 1, 0, "UNSAFE_GOOD"))
        self.assertTrue(c.record(1).terminal)
        with self.assertRaises(RuntimeError):
            c.retry_frame(1, now=2)

    def test_stale_boot_run_and_unknown_ack_do_not_create_requests(self):
        c = self.client()
        c.request_frame(1, "crack", detected_at=1, now=1)
        for line in (
            "XA:87654321:0000000000000001:1:7510:ACCEPTED",
            "XA:12345678:0000000000000002:1:7510:ACCEPTED",
            "XA:12345678:0000000000000001:99:7510:ACCEPTED",
            "XA:12345678:0000000000000001:1:7510:MADE_UP",
        ):
            self.assertFalse(c.accept_ack(line))
        self.assertIsNone(c.record(1).controller_deadline)
        self.assertEqual(c.record(1).status, "NEW")

    def test_completed_request_cannot_be_revived_by_stale_ack(self):
        c = self.client()
        c.request_frame(1, "crack", detected_at=1, now=1)
        self.assertTrue(self.ack(c, 1, 7510, "ACCEPTED"))
        self.assertTrue(self.ack(c, 1, 7510, "SETTLED_UNVERIFIED"))
        self.assertFalse(self.ack(c, 1, 7510, "ACCEPTED"))
        self.assertFalse(self.ack(c, 1, 7510, "DUPLICATE"))
        self.assertEqual(c.record(1).status, "SETTLED_UNVERIFIED")
        with self.assertRaises(RuntimeError):
            c.retry_frame(1, now=2)

    def test_queue_overflow_has_no_extra_request(self):
        c = self.client()
        for i in range(1, 5):
            c.request_frame(i, "crack", detected_at=1, now=1)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            c.request_frame(5, "rotten", detected_at=1, now=1)
        with self.assertRaises(KeyError):
            c.record(5)

    def test_expired_and_conflicting_requests_remain_terminal(self):
        c = self.client()
        c.request_frame(1, "crack", detected_at=1, now=1)
        with self.assertRaisesRegex(RuntimeError, "quality"):
            c.request_frame(1, "rotten", detected_at=1, now=1)
        self.assertTrue(self.ack(c, 1, 7510, "EXPIRED"))
        with self.assertRaises(RuntimeError):
            c.request_frame(1, "crack", detected_at=1, now=2)

    def test_controller_reboot_cannot_automatically_replay_pending_work(self):
        c = self.client()
        c.request_frame(1, "crack", detected_at=1, now=1)
        with self.assertRaisesRegex(RuntimeError, "restarted"):
            c.accept_hello("XH:87654321")
        self.assertFalse(c.ready)
        with self.assertRaises(RuntimeError):
            c.retry_frame(1, now=2)

    def test_clock_domains_are_separate_including_zero_wrapped_deadline(self):
        c = self.client()
        frame = c.request_frame(1, "crack", detected_at=900000.0, now=900000.025)
        self.assertNotIn("900000", frame)
        self.assertTrue(self.ack(c, 1, 0, "ACCEPTED"))
        self.assertEqual(c.record(1).controller_deadline, 0)
        self.assertTrue(self.ack(c, 1, 0, "SETTLED_UNVERIFIED"))

    def test_evicted_history_id_cannot_be_reused(self):
        c = self.client(max_pending=1, history_size=1)
        c.request_frame(1, "crack", detected_at=1, now=1)
        self.ack(c, 1, 7510, "SETTLED_UNVERIFIED")
        c.request_frame(2, "rotten", detected_at=2, now=2)
        with self.assertRaisesRegex(RuntimeError, "Retired"):
            c.request_frame(1, "crack", detected_at=1, now=2)

    def test_invalid_input_and_no_unsolicited_begin_ack(self):
        c = self.client()
        self.assertFalse(self.ack(c, 0, 0, "STARTED"))
        for capture_id, quality, detected_at, now in (
            (0, "crack", 1, 1), (True, "crack", 1, 1),
            (1, "unknown", 1, 1), (1, "crack", 2, 1),
            (1, "crack", float("nan"), 1),
        ):
            with self.assertRaises(ValueError):
                c.request_frame(capture_id, quality, detected_at=detected_at, now=now)


if __name__ == "__main__":
    unittest.main()
