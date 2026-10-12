import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SUPABASE_DB_URL"] = ""

from app import CameraSessionError, SORTING_RUNTIME_INSTANCE, app, start_sorting_runtime
from security import CSRF_SESSION_KEY


class ApplicationSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_health_check_and_security_headers(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok", "database": "connected"})
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_login_page_contains_csrf_integration(self) -> None:
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="csrf-token"', response.data)
        self.assertIn(b"security.js", response.data)

    def test_disabled_hardware_never_starts_camera_or_serial_bridge(self) -> None:
        with (
            patch.dict(app.config, {"LOCAL_HARDWARE_ENABLED": False}),
            patch("app.CAMERA_SESSION.start") as camera,
            patch("app.ESP32_BRIDGE.start") as hardware,
        ):
            with self.assertRaises(CameraSessionError):
                start_sorting_runtime()
        camera.assert_not_called()
        hardware.assert_not_called()

    def test_cloud_camera_requests_return_json_without_loading_detector(self) -> None:
        staff = SimpleNamespace(
            id=99, username="staff", email="staff@example.com", display_name="Staff",
            avatar_url=None, role="staff", is_active=True, password_set=True,
        )
        token = "test-csrf-token"
        with self.client.session_transaction() as session:
            session["user_id"] = staff.id
            session[CSRF_SESSION_KEY] = token
        with (
            patch.dict(app.config, {
                "LOCAL_HARDWARE_ENABLED": False, "AUTO_START_SORTING_ON_LOGIN": True,
            }),
            patch("app.db.session.get", return_value=staff),
            patch("app.start_sorting_runtime") as start_runtime,
            patch("app.stop_sorting_runtime") as stop_runtime,
            patch("app.detect_frame") as detector,
        ):
            for path in ("/api/camera/start", "/api/camera/stop", "/api/detect"):
                with self.subTest(path=path):
                    response = self.client.post(path, headers={"X-CSRF-Token": token})
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("sorting computer", response.get_json()["error"])
            page = self.client.get("/sorting-sessions")
            self.assertIn(b'data-hardware-enabled="false"', page.data)
            self.assertIn(b'Operate the Machine on the Sorting Computer', page.data)
        start_runtime.assert_not_called()
        stop_runtime.assert_not_called()
        detector.assert_not_called()

    def test_unsafe_app_request_requires_csrf_token(self) -> None:
        response = self.client.post("/register")
        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_api_returns_json_instead_of_redirect(self) -> None:
        response = self.client.get("/api/dashboard/stats")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Authentication is required.")

    def test_sales_routes_are_not_registered(self) -> None:
        routes = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertNotIn("/sales", routes)
        self.assertNotIn("/api/sales", routes)

    def test_stale_runtime_marker_recovers_stopped_shared_runtime(self) -> None:
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["sorting_runtime_instance"] = SORTING_RUNTIME_INSTANCE
        with (
            patch("app.CAMERA_SESSION.status", return_value={"running": False}),
            patch("app.ESP32_BRIDGE.status", return_value={"running": False}),
            patch(
                "app.start_sorting_runtime",
                return_value=({"running": True}, {"running": True}),
            ) as start_runtime,
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        start_runtime.assert_called_once_with()

    def test_manual_runtime_stop_suppresses_auto_restart(self) -> None:
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["sorting_runtime_auto_start_suppressed"] = True
        with patch("app.start_sorting_runtime") as start_runtime:
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        start_runtime.assert_not_called()

    def test_staff_role_is_rejected_from_admin_api(self) -> None:
        staff = SimpleNamespace(
            id=99,
            username="staff",
            email="staff@example.com",
            display_name="Staff",
            avatar_url=None,
            role="staff",
            is_active=True,
            password_set=True,
        )
        with self.client.session_transaction() as session:
            session["user_id"] = staff.id
            session["sorting_runtime_auto_start_suppressed"] = True
        with patch("app.db.session.get", return_value=staff):
            response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 403)

    def test_unexpected_google_failure_returns_safe_login_error(self) -> None:
        with patch.dict(
            app.config,
            {
                "GOOGLE_CLIENT_ID": "client-id",
                "GOOGLE_CLIENT_SECRET": "client-secret",
            },
        ), patch(
            "app.oauth.google.authorize_access_token",
            side_effect=RuntimeError("provider unavailable"),
        ), patch("app.app.logger.exception"):
            response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))
        with self.client.session_transaction() as session:
            self.assertIn("temporarily unavailable", session["login_error"])


if __name__ == "__main__":
    unittest.main()
