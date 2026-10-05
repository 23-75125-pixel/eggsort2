import os
import unittest
from unittest.mock import patch

from config import (
    ConfigurationError,
    build_app_config,
    env_bool,
    env_int,
    normalize_database_url,
)


class ConfigurationTests(unittest.TestCase):
    def test_boolean_values_are_explicit(self) -> None:
        with patch.dict(os.environ, {"FEATURE_FLAG": "yes"}, clear=False):
            self.assertTrue(env_bool("FEATURE_FLAG"))
        with patch.dict(os.environ, {"FEATURE_FLAG": "sometimes"}, clear=False):
            with self.assertRaises(ConfigurationError):
                env_bool("FEATURE_FLAG")

    def test_integer_range_is_validated(self) -> None:
        with patch.dict(os.environ, {"PORT": "0"}, clear=False):
            with self.assertRaises(ConfigurationError):
                env_int("PORT", 5000, minimum=1, maximum=65535)

    def test_database_url_is_normalized(self) -> None:
        self.assertEqual(
            normalize_database_url("postgres://host/db?pgbouncer=true"),
            "postgresql://host/db",
        )

    def test_application_config_uses_secure_cookie_for_https(self) -> None:
        environment = {
            "SECRET_KEY": "s" * 32,
            "PUBLIC_BASE_URL": "https://eggsort.example",
            "MAIL_USE_TLS": "1",
            "MAIL_USE_SSL": "0",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = build_app_config()
        self.assertTrue(config["SESSION_COOKIE_HTTPONLY"])
        self.assertTrue(config["SESSION_COOKIE_SECURE"])
        self.assertEqual(config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertEqual(config["TRUSTED_HOSTS"], ["eggsort.example"])


if __name__ == "__main__":
    unittest.main()
