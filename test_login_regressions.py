"""Real authentication routes; disposable SQLite and forbidden external I/O."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from auth_test_support import auth, app, db, AuditLog, PasswordResetAttempt, User
from security import AttemptLimiter


class LoginRegressionTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.session.remove)
        self.assertEqual(db.engine.url.get_backend_name(), "sqlite")
        self.assertIn(db.engine.url.database, (None, "", ":memory:"))
        AuditLog.query.delete()
        PasswordResetAttempt.query.delete()
        User.query.delete()
        db.session.commit()
        app.config.update(TESTING=True, LOCAL_HARDWARE_ENABLED=False,
                          AUTO_START_SORTING_ON_LOGIN=False)
        self.user = User(username="operator", email="operator@example.invalid",
                         display_name="Operator", role="staff", is_active=True,
                         password_set=True, password=generate_password_hash("CorrectPassword123!"))
        db.session.add(self.user)
        db.session.commit()
        self.client = app.test_client()
        for target, options in (
            ("send_password_reset", {}),
            ("stop_sorting_runtime", {"return_value": ({}, {})}),
            ("LOGIN_LIMITER", {"new": AttemptLimiter()}),
        ):
            p = patch.object(auth, target, **options)
            result = p.start()
            setattr(self, target, result)
            self.addCleanup(p.stop)
        p = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        p.start()
        self.addCleanup(p.stop)

    def token(self, path="/login"):
        response = self.client.get(path)
        match = re.search(rb'name="csrf-token" content="([^"]+)"', response.data)
        self.assertIsNotNone(match, response.data[:200])
        return match.group(1).decode()

    def login(self, identifier="operator", password="CorrectPassword123!", remember=False):
        return self.client.post("/login", data={
            "_csrf_token": self.token(), "identifier": identifier, "password": password,
            **({"remember-me": "on"} if remember else {}),
        })

    def forgot(self, email=None, **kwargs):
        return self.client.post("/forgot-password", data={
            "_csrf_token": self.token("/forgot-password"),
            "email": self.user.email if email is None else email,
        }, **kwargs)

    def reset_link(self, seconds=1800):
        token = "isolated-reset-token"
        self.user.password_reset_token_hash = auth.invite_token_hash(token)
        self.user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        db.session.commit()
        return "/reset-password/" + token

    def test_username_email_case_and_remember(self):
        for identifier in (" Operator ", " OPERATOR@EXAMPLE.INVALID "):
            with self.subTest(identifier=identifier):
                self.client = app.test_client()
                response = self.login(identifier, remember=True)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/dashboard"))
                with self.client.session_transaction() as session:
                    self.assertEqual(session["user_id"], self.user.id)
                    self.assertTrue(session.permanent)
        self.client = app.test_client()
        self.login()
        with self.client.session_transaction() as session:
            self.assertFalse(session.permanent)

    def test_invalid_inactive_unconfigured_and_unknown_are_generic(self):
        cases = [(True, True, "operator", "wrong"), (False, True, "operator", "CorrectPassword123!"),
                 (True, False, "operator", "CorrectPassword123!"), (True, True, "unknown", "wrong")]
        for active, configured, identifier, password in cases:
            self.user.is_active, self.user.password_set = active, configured
            db.session.commit()
            response = self.login(identifier, password)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Invalid username/email or password.", response.data)
            with self.client.session_transaction() as session:
                self.assertNotIn("user_id", session)

    def test_username_and_email_share_five_attempt_limit(self):
        for i in range(5):
            response = self.login("operator" if i % 2 else self.user.email, "wrong")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.login().status_code, 429)

    def test_password_login_audit_failure_still_succeeds(self):
        with patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")):
            response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_failed_login_audit_failure_does_not_authenticate(self):
        with patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")):
            response = self.login(password="wrong")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_google_success_audit_failure_still_succeeds(self):
        with (
            patch.object(auth.oauth.google, "authorize_access_token", return_value={"userinfo": {
                "email": self.user.email, "sub": "isolated-google-sub", "email_verified": True,
            }}),
            patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")),
        ):
            response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], self.user.id)

    def test_logout_audit_failure_still_stops_and_clears(self):
        self.login()
        token = self.token("/dashboard")
        with patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")):
            response = self.client.post("/logout", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.stop_sorting_runtime.assert_called_once_with()
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_logout_stop_failure_reports_unverified_shutdown_and_clears(self):
        self.login()
        token = self.token("/dashboard")
        self.stop_sorting_runtime.side_effect = RuntimeError("test stop failure")
        response = self.client.post("/logout", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"You have been signed out", response.data)
        self.assertIn(b"Hardware shutdown could not be verified", response.data)
        self.assertNotIn(b"You have been logged out.", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
        self.assertEqual(self.client.get("/api/users").status_code, 401)

    def test_logout_stop_and_rollback_failure_still_report_and_clear(self):
        self.login()
        token = self.token("/dashboard")
        self.stop_sorting_runtime.side_effect = RuntimeError("test stop failure")
        with patch.object(auth.db.session, "rollback", side_effect=RuntimeError("test rollback failure")):
            response = self.client.post("/logout", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Hardware shutdown could not be verified", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_both_logout_failures_still_attempt_stop_and_clear(self):
        self.login()
        token = self.token("/dashboard")
        self.stop_sorting_runtime.side_effect = RuntimeError("test stop failure")
        with patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")):
            self.assertEqual(self.client.post("/logout", data={"_csrf_token": token}).status_code, 503)
        self.stop_sorting_runtime.assert_called_once()
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_protected_routes_role_and_inactive_session(self):
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/api/users").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.user.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_forgot_generic_for_unknown_invalid_inactive_and_unconfigured(self):
        for email, active, configured in (("unknown@example.invalid", True, True),
                                          ("bad-email", True, True), ("", True, True),
                                          (self.user.email, False, True),
                                          (self.user.email, True, False)):
            self.user.is_active, self.user.password_set = active, configured
            db.session.commit()
            response = self.forgot(email)
            self.assertEqual(response.status_code, 302)
            page = self.client.get(response.location)
            self.assertIn(b"If an active account uses that email", page.data)
        self.send_password_reset.assert_not_called()

    def test_reset_email_contains_token_not_stored_in_plaintext(self):
        self.forgot(" OPERATOR@EXAMPLE.INVALID ")
        url = self.send_password_reset.call_args.args[1]
        token = url.rsplit("/", 1)[1]
        db.session.refresh(self.user)
        self.assertEqual(self.user.password_reset_token_hash, auth.invite_token_hash(token))
        self.assertNotEqual(self.user.password_reset_token_hash, token)

    def test_mail_failure_preserves_generic_response(self):
        self.send_password_reset.side_effect = auth.InvitationDeliveryError("test delivery failure")
        response = self.forgot()
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"If an active account uses that email", self.client.get(response.location).data)

    def test_reset_throttles_identifier_window_and_expiry(self):
        with patch.object(auth, "PASSWORD_RESET_COOLDOWN", timedelta(0)), patch.object(
            auth, "PASSWORD_RESET_MAX_PER_IDENTIFIER", 2
        ):
            for _ in range(3):
                self.forgot()
            self.assertEqual(self.send_password_reset.call_count, 2)
            PasswordResetAttempt.query.update({PasswordResetAttempt.created_at:
                datetime.now(timezone.utc) - auth.PASSWORD_RESET_WINDOW - timedelta(seconds=1)})
            db.session.commit()
            self.forgot()
            self.assertEqual(self.send_password_reset.call_count, 3)

    def test_reset_ip_ignores_forwarded_headers(self):
        with app.test_request_context("/", environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
                                     headers={"X-Forwarded-For": "198.51.100.9"}):
            self.assertEqual(auth.password_reset_client_ip(), "203.0.113.10")
        self.assertEqual(auth.password_reset_identifier_hash(" A@EXAMPLE.COM "),
                         auth.password_reset_identifier_hash("a@example.com"))

    def test_reset_validation_does_not_consume_token(self):
        path = self.reset_link()
        for password, confirmation in (("short", "short"), ("a" * 129, "a" * 129),
                                       ("ValidPassword!", "different")):
            response = self.client.post(path, data={"_csrf_token": self.token(path),
                "password": password, "password_confirmation": confirmation})
            self.assertEqual(response.status_code, 200)
            db.session.refresh(self.user)
            self.assertIsNotNone(self.user.password_reset_token_hash)

    def test_reset_expired_and_unknown_tokens_rejected(self):
        path = self.reset_link(seconds=-1)
        self.assertEqual(self.client.get(path).status_code, 400)
        self.assertEqual(self.client.get("/reset-password/not-valid").status_code, 400)

    def test_reset_account_disabled_after_link_open_is_rejected(self):
        path = self.reset_link()
        token = self.token(path)
        self.user.is_active = False
        db.session.commit()
        response = self.client.post(path, data={"_csrf_token": token,
            "password": "NewPassword123!", "password_confirmation": "NewPassword123!"})
        self.assertEqual(response.status_code, 400)
        db.session.refresh(self.user)
        self.assertTrue(check_password_hash(self.user.password, "CorrectPassword123!"))

    def test_reset_success_single_use_and_new_password_login(self):
        path = self.reset_link()
        response = self.client.post(path, data={"_csrf_token": self.token(path),
            "password": "NewPassword123!", "password_confirmation": "NewPassword123!"})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.user)
        self.assertIsNone(self.user.password_reset_token_hash)
        self.assertTrue(check_password_hash(self.user.password, "NewPassword123!"))
        self.assertEqual(self.client.get(path).status_code, 400)
        self.assertEqual(self.login(password="CorrectPassword123!").status_code, 200)
        self.assertEqual(self.login(password="NewPassword123!").status_code, 302)

    def test_reset_token_consumed_during_request_cannot_be_reused(self):
        path = self.reset_link()
        token = self.token(path)
        original = auth.generate_password_hash
        def consume_before_update(password):
            User.query.filter_by(id=self.user.id).update({User.password_reset_token_hash: None})
            db.session.commit()
            return original(password)
        with patch.object(auth, "generate_password_hash", side_effect=consume_before_update):
            response = self.client.post(path, data={"_csrf_token": token,
                "password": "NewPassword123!", "password_confirmation": "NewPassword123!"})
        self.assertEqual(response.status_code, 400)
        db.session.refresh(self.user)
        self.assertTrue(check_password_hash(self.user.password, "CorrectPassword123!"))

    def test_reset_audit_failure_does_not_report_password_change_as_failed(self):
        path = self.reset_link()
        token = self.token(path)
        with patch.object(auth, "write_audit_log", side_effect=SQLAlchemyError("test outage")):
            response = self.client.post(path, data={"_csrf_token": token,
                "password": "NewPassword123!", "password_confirmation": "NewPassword123!"})
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
