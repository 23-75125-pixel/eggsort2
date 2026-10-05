import unittest

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


class AttemptLimiterTests(unittest.TestCase):
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
