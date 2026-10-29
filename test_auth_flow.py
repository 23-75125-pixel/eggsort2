import re
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from auth_test_support import auth, app, db, AuditLog, PasswordResetAttempt, User

create_staff_invitation = auth.create_staff_invitation
invitation_is_valid = auth.invitation_is_valid
invite_token_hash = auth.invite_token_hash
from werkzeug.security import check_password_hash, generate_password_hash


class StaffInvitationFlowTests(unittest.TestCase):
    def test_new_invitation_stores_only_hash_and_is_valid(self) -> None:
        user = SimpleNamespace(invite_token_hash=None, invite_expires_at=None)
        token = create_staff_invitation(user)

        self.assertNotEqual(user.invite_token_hash, token)
        self.assertEqual(user.invite_token_hash, invite_token_hash(token))
        self.assertTrue(invitation_is_valid(user))

    def test_expired_or_consumed_invitation_is_invalid(self) -> None:
        expired = SimpleNamespace(
            invite_token_hash="hash",
            invite_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        consumed = SimpleNamespace(
            invite_token_hash=None,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        self.assertFalse(invitation_is_valid(expired))
        self.assertFalse(invitation_is_valid(consumed))

    def test_naive_legacy_expiry_is_treated_as_utc(self) -> None:
        user = SimpleNamespace(
            invite_token_hash="hash",
            invite_expires_at=(
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).replace(tzinfo=None),
        )
        self.assertTrue(invitation_is_valid(user))


class StaffInvitationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = secrets.token_hex(6)
        self.email = f"staff-{suffix}@example.com"
        self.username = f"staff-{suffix}"
        self.password = "StrongPassword123!"
        self.original_config = {
            "AUTO_START_SORTING_ON_LOGIN": app.config[
                "AUTO_START_SORTING_ON_LOGIN"
            ],
            "GOOGLE_CLIENT_ID": app.config["GOOGLE_CLIENT_ID"],
            "GOOGLE_CLIENT_SECRET": app.config["GOOGLE_CLIENT_SECRET"],
        }
        app.config.update(
            TESTING=True,
            AUTO_START_SORTING_ON_LOGIN=False,
            GOOGLE_CLIENT_ID="",
            GOOGLE_CLIENT_SECRET="",
        )
        with app.app_context():
            user = User(
                username=self.username,
                password="not-yet-set",
                email=self.email,
                display_name="Test Staff",
                role="staff",
                is_active=True,
                password_set=False,
            )
            self.token = create_staff_invitation(user)
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        self.client = app.test_client()

    def tearDown(self) -> None:
        with app.app_context():
            PasswordResetAttempt.query.delete()
            AuditLog.query.filter(AuditLog.actor == self.email).delete()
            user = db.session.get(User, self.user_id)
            if user is not None:
                db.session.delete(user)
            db.session.commit()
        app.config.update(self.original_config)

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(rb'name="csrf-token" content="([^"]+)"', response.data)
        if match is None:
            raise AssertionError("CSRF token was not rendered")
        return match.group(1).decode("ascii")

    def test_invite_activation_login_authorization_and_logout(self) -> None:
        invite_page = self.client.get(f"/accept-invite/{self.token}")
        self.assertEqual(invite_page.status_code, 200)
        response = self.client.post(
            f"/accept-invite/{self.token}",
            data={
                "_csrf_token": self._csrf_token(invite_page),
                "password": self.password,
                "password_confirmation": self.password,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))

        with app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertTrue(user.password_set)
            self.assertIsNone(user.invite_token_hash)
            self.assertTrue(check_password_hash(user.password, self.password))

        login_page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "_csrf_token": self._csrf_token(login_page),
                "identifier": self.email,
                "password": self.password,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))

        forbidden = self.client.get("/api/users")
        self.assertEqual(forbidden.status_code, 403)

        dashboard = self.client.get("/dashboard")
        logout = self.client.post(
            "/logout",
            data={"_csrf_token": self._csrf_token(dashboard)},
        )
        self.assertEqual(logout.status_code, 302)
        self.assertTrue(logout.location.endswith("/login"))
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_expired_invitation_cannot_set_password(self) -> None:
        with app.app_context():
            user = db.session.get(User, self.user_id)
            user.invite_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.session.commit()

        invite_page = self.client.get(f"/accept-invite/{self.token}")
        response = self.client.post(
            f"/accept-invite/{self.token}",
            data={
                "_csrf_token": self._csrf_token(invite_page),
                "password": self.password,
                "password_confirmation": self.password,
            },
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertFalse(user.password_set)


class PasswordResetThrottlingTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = secrets.token_hex(6)
        self.email = f"reset-{suffix}@example.com"
        app.config.update(TESTING=True, AUTO_START_SORTING_ON_LOGIN=False)
        with app.app_context():
            user = User(
                username=f"reset-{suffix}",
                password=generate_password_hash("ResetPassword123!"),
                email=self.email,
                display_name="Reset User",
                role="staff",
                is_active=True,
                password_set=True,
            )
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
            PasswordResetAttempt.query.delete()
            db.session.commit()
        self.client = app.test_client()

    def tearDown(self) -> None:
        with app.app_context():
            PasswordResetAttempt.query.delete()
            user = db.session.get(User, self.user_id)
            if user is not None:
                db.session.delete(user)
            db.session.commit()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(rb'name="csrf-token" content="([^"]+)"', response.data)
        if match is None:
            raise AssertionError("CSRF token was not rendered")
        return match.group(1).decode("ascii")

    def _forgot_password(self, email: str, environ_overrides: dict | None = None):
        page = self.client.get("/forgot-password")
        return self.client.post(
            "/forgot-password",
            data={"_csrf_token": self._csrf_token(page), "email": email},
            environ_overrides=environ_overrides or {},
        )

    def test_identifier_cooldown_blocks_repeat_reset_email(self) -> None:
        with patch.object(auth, "send_password_reset") as send_password_reset:
            first = self._forgot_password(self.email)
            second = self._forgot_password(self.email)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(send_password_reset.call_count, 1)
        with app.app_context():
            self.assertEqual(PasswordResetAttempt.query.count(), 2)

    def test_ip_window_limit_blocks_even_with_zero_cooldown(self) -> None:
        with (
            patch.object(auth, "PASSWORD_RESET_COOLDOWN", timedelta(seconds=0)),
            patch.object(auth, "PASSWORD_RESET_WINDOW", timedelta(minutes=10)),
            patch.object(auth, "PASSWORD_RESET_MAX_PER_IDENTIFIER", 50),
            patch.object(auth, "PASSWORD_RESET_MAX_PER_IP", 1),
            patch.object(auth, "send_password_reset") as send_password_reset,
        ):
            first = self._forgot_password(
                self.email,
                environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
            )
            second = self._forgot_password(
                self.email,
                environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
            )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(send_password_reset.call_count, 1)


if __name__ == "__main__":
    unittest.main()
