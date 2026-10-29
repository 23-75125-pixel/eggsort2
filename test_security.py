import unittest
from unittest.mock import patch

from flask import Flask, jsonify

from security import AttemptLimiter, csrf_token, init_web_security


class WebSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config.update(SECRET_KEY="test-secret-key", TESTING=True)
        init_web_security(app)

        @app.get("/token")
        def token():
            return jsonify(token=csrf_token())

        @app.post("/api/action")
        def action():
            return jsonify(ok=True)

        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1

    def test_unsafe_request_without_token_is_rejected(self) -> None:
        response = self.client.post("/api/action", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("security token", response.get_json()["error"])

    def test_valid_header_allows_unsafe_request(self) -> None:
        token = self.client.get("/token").get_json()["token"]
        response = self.client.post(
            "/api/action",
            json={},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_api_request_returns_json_401(self) -> None:
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post("/api/action", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Authentication is required.")

    def test_security_headers_are_applied(self) -> None:
        response = self.client.get("/token")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_non_ascii_and_incorrect_tokens_are_400(self) -> None:
        token = self.client.get("/token").get_json()["token"]
        for supplied in ("é" * 32, "wrong", "", token + "x"):
            with self.subTest(supplied=supplied):
                response = self.client.post("/api/action", data={"_csrf_token": supplied})
                self.assertEqual(response.status_code, 400)
                self.assertIn("security token", response.get_json()["error"])

    def test_malformed_session_token_is_400(self) -> None:
        for expected in (123, ["token"], "é" * 32):
            with self.client.session_transaction() as session:
                session["_csrf_token"] = expected
            self.assertEqual(self.client.post(
                "/api/action", headers={"X-CSRF-Token": "ascii-token"}
            ).status_code, 400)

    def test_non_ascii_form_token_returns_html_400(self) -> None:
        self.client.get("/token")
        self.assertEqual(self.client.post(
            "/form", data={"_csrf_token": "é"}
        ).status_code, 400)


class AttemptLimiterTests(unittest.TestCase):
    def test_capacity_for_actual_login_sequence_and_read_only_checks(self) -> None:
        limiter = AttemptLimiter()
        for i in range(limiter.MAX_TRACKED_KEYS + 100):
            key = f"unknown-{i}"
            self.assertFalse(limiter.is_limited(key))
            limiter.record_failure(key)
            self.assertLessEqual(len(limiter._attempts), limiter.MAX_TRACKED_KEYS)
        self.assertNotIn("unknown-0", limiter._attempts)
        for i in range(100):
            limiter.is_limited(f"check-only-{i}")
        self.assertEqual(len(limiter._attempts), limiter.MAX_TRACKED_KEYS)

    def test_expired_entries_pruned_across_identifiers(self) -> None:
        with patch("security.monotonic", return_value=100) as clock:
            limiter = AttemptLimiter(window_seconds=300)
            limiter.record_failure("old")
            clock.return_value = 399
            limiter.record_failure("new")
            clock.return_value = 400
            limiter.is_limited("unrelated")
            self.assertNotIn("old", limiter._attempts)
            self.assertIn("new", limiter._attempts)
            clock.return_value = 700
            limiter.record_failure("fresh")
            self.assertEqual(set(limiter._attempts), {"fresh"})

    def test_identifier_normalization_and_blank_identifier(self) -> None:
        limiter = AttemptLimiter(max_attempts=2)
        limiter.record_failure(" Operator ")
        limiter.record_failure("OPERATOR")
        self.assertTrue(limiter.is_limited("operator"))
        limiter.reset(" Operator ")
        self.assertFalse(limiter.is_limited("operator"))
        limiter.record_failure(" ")
        limiter.record_failure("")
        self.assertTrue(limiter.is_limited("  "))

    def test_limiter_blocks_after_configured_failures(self) -> None:
        limiter = AttemptLimiter(max_attempts=2, window_seconds=60)
        limiter.record_failure("operator")
        self.assertFalse(limiter.is_limited("operator"))
        limiter.record_failure("Operator")
        self.assertTrue(limiter.is_limited("operator"))
        limiter.reset("operator")
        self.assertFalse(limiter.is_limited("operator"))


if __name__ == "__main__":
    unittest.main()
