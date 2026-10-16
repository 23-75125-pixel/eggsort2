"""Offline guards for the opt-in disposable write profile."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from config import ConfigurationError, build_app_config, disposable_write_database_url, production_database_url
from flask import Flask
from extensions import db
import database


GOOD_URL = "postgresql://eggsor_phase2b_runner:dummy@127.0.0.1:55432/eggsor_p2b_disposable"
PRODUCTION_URL = "postgresql://postgres.gtsymkfcoslnobitdheu:dummy@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"


class DisposableConfigTests(unittest.TestCase):
    def config(self, **extra):
        values = {
            "LOCAL_TEST_MODE": "0", "PHASE2B_WRITE_TEST_MODE": "1",
            "PHASE2B_WRITE_TEST_DATABASE_URL": GOOD_URL,
            "SECRET_KEY": "s" * 32,
            "SUPABASE_DB_URL": "not-a-test-url",
            "DATABASE_URL": "also-not-a-test-url",
        }
        values.update(extra)
        with patch.dict(os.environ, values, clear=True):
            return build_app_config()

    def test_only_exact_disposable_target_and_runner(self):
        config = self.config()
        self.assertEqual(config["SQLALCHEMY_DATABASE_URI"].database, "eggsor_p2b_disposable")
        self.assertEqual(config["SQLALCHEMY_DATABASE_URI"].username, "eggsor_phase2b_runner")
        self.assertFalse(config["LOCAL_HARDWARE_ENABLED"])
        self.assertFalse(config["AUTO_START_SORTING_ON_LOGIN"])
        self.assertFalse(config["LIFECYCLE_WRITES_ENABLED"])
        self.assertEqual(config["SQLALCHEMY_ENGINE_OPTIONS"]["execution_options"]
                         ["schema_translate_map"], {None: "eggsor_p2b_test"})

    def test_wrong_host_database_user_query_and_missing_url_fail_closed(self):
        bad = [
            GOOD_URL.replace("127.0.0.1", "localhost"),
            GOOD_URL.replace("55432", "5432"),
            GOOD_URL.replace("eggsor_p2b_disposable", "eggsor_restore_test"),
            GOOD_URL.replace("eggsor_phase2b_runner", "postgres"),
            GOOD_URL + "?sslmode=disable", "",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                self.config(PHASE2B_WRITE_TEST_DATABASE_URL=value)

    def test_readonly_and_write_modes_are_mutually_exclusive(self):
        with self.assertRaises(ConfigurationError):
            self.config(LOCAL_TEST_MODE="1")

    def test_wrong_url_error_redacts_password(self):
        with self.assertRaises(ConfigurationError) as caught:
            disposable_write_database_url(
                "postgresql://postgres:a-private-test-value@127.0.0.1:55432/eggsor_restore_test")
        self.assertNotIn("a-private-test-value", str(caught.exception))

    def test_existing_readonly_mode_unchanged(self):
        with patch.dict(os.environ, {
            "LOCAL_TEST_MODE": "1", "PHASE2B_WRITE_TEST_MODE": "0",
            "LOCAL_TEST_DATABASE_URL": "postgresql://postgres@127.0.0.1:55432/eggsor_restore_test",
            "SECRET_KEY": "s" * 32,
        }, clear=True):
            config = build_app_config()
        self.assertEqual(config["SQLALCHEMY_DATABASE_URI"].database, "eggsor_restore_test")
        self.assertIn("default_transaction_read_only=on",
                      config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["options"])
        self.assertFalse(config["LIFECYCLE_WRITES_ENABLED"])

    def test_disposable_engine_creation_is_lazy_and_schema_mapped(self):
        app = Flask("offline-disposable-engine")
        app.config.from_mapping(self.config())
        db.init_app(app)
        with app.app_context():
            self.assertEqual(db.engine.url.database, "eggsor_p2b_disposable")
            self.assertEqual(db.engine.get_execution_options()["schema_translate_map"],
                             {None: "eggsor_p2b_test"})

    def test_disposable_initialization_only_verifies_existing_schema(self):
        app = Flask("offline-disposable-init")
        app.config["LOCAL_TEST_MODE"] = False
        app.config["PHASE2B_WRITE_TEST_MODE"] = True
        with patch.object(database, "verify_disposable_test_schema") as verify, \
             patch.object(database.db, "create_all") as create, \
             patch.object(database, "_migrate_user_columns") as migrate, \
             patch.object(database, "_ensure_admin") as admin:
            database.initialize_database(app, admin_email="unused", admin_google_sub="")
        verify.assert_called_once()
        create.assert_not_called()
        migrate.assert_not_called()
        admin.assert_not_called()

    def test_disposable_application_import_skips_dotenv_and_bootstrap(self):
        environment = dict(os.environ,
            LOCAL_TEST_MODE="0", PHASE2B_WRITE_TEST_MODE="1",
            PHASE2B_WRITE_TEST_DATABASE_URL=GOOD_URL,
            SECRET_KEY="s" * 32, SUPABASE_DB_URL="ignored-production-placeholder")
        code = '''
from unittest.mock import patch
from sqlalchemy.engine import Engine
with patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv read')), \\
     patch('database.verify_disposable_test_schema') as verify, \\
     patch('database.db.create_all', side_effect=AssertionError('bootstrap create')), \\
     patch.object(Engine, 'connect', side_effect=AssertionError('unexpected connection')):
    import app
    verify.assert_called_once()
    assert app.app.config['PHASE2B_WRITE_TEST_MODE']
    assert not app.app.config['LOCAL_HARDWARE_ENABLED']
    assert not app.app.config['LIFECYCLE_WRITES_ENABLED']
'''
        result = subprocess.run([sys.executable, "-c", code], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_mode_pins_project_and_keeps_sorting_off_by_default(self):
        with patch.dict(os.environ, {
            "LOCAL_TEST_MODE": "0", "PHASE2B_WRITE_TEST_MODE": "0",
            "EGGSOR_PRODUCTION_DB_MODE": "1", "DATABASE_URL": PRODUCTION_URL,
            "SECRET_KEY": "s" * 32,
        }, clear=True):
            config = build_app_config()
        self.assertEqual(config["SQLALCHEMY_DATABASE_URI"].database, "postgres")
        self.assertTrue(config["EGGSOR_PRODUCTION_DB_MODE"])
        self.assertFalse(config["LIFECYCLE_WRITES_ENABLED"])
        self.assertEqual(config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["sslmode"], "require")

    def test_production_target_or_mode_mismatch_fails_closed(self):
        for value in (PRODUCTION_URL.replace("gtsymkfcoslnobitdheu", "otherproject"),
                      PRODUCTION_URL.replace("5432", "6543"),
                      PRODUCTION_URL.replace("/postgres", "/eggsor_restore_test"),
                      PRODUCTION_URL.replace("aws-0-ap-northeast-2.pooler.supabase.com", "127.0.0.1")):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                production_database_url(value)
        with patch.dict(os.environ, {
            "LOCAL_TEST_MODE": "1", "EGGSOR_PRODUCTION_DB_MODE": "1",
            "LOCAL_TEST_DATABASE_URL": "postgresql://postgres@127.0.0.1:55432/eggsor_restore_test",
            "DATABASE_URL": PRODUCTION_URL, "SECRET_KEY": "s" * 32,
        }, clear=True), self.assertRaises(ConfigurationError):
            build_app_config()

    def test_production_initialization_only_verifies_existing_schema(self):
        app = Flask("offline-production-init")
        app.config.update(LOCAL_TEST_MODE=False, PHASE2B_WRITE_TEST_MODE=False,
                          EGGSOR_PRODUCTION_DB_MODE=True)
        with patch.object(database, "verify_production_relationship_schema") as verify, \
             patch.object(database.db, "create_all") as create, \
             patch.object(database, "_migrate_user_columns") as migrate, \
             patch.object(database, "_ensure_admin") as admin:
            database.initialize_database(app, admin_email="unused", admin_google_sub="")
        verify.assert_called_once()
        create.assert_not_called()
        migrate.assert_not_called()
        admin.assert_not_called()

    def test_production_application_import_has_no_bootstrap_writes(self):
        environment = dict(os.environ,
            LOCAL_TEST_MODE="0", PHASE2B_WRITE_TEST_MODE="0",
            EGGSOR_PRODUCTION_DB_MODE="1",
            EGGSOR_PRODUCTION_SORTING_ENABLED="0",
            DATABASE_URL=PRODUCTION_URL, SECRET_KEY="s" * 32)
        environment.pop("SUPABASE_DB_URL", None)
        code = '''
from unittest.mock import patch
from sqlalchemy.engine import Engine
with patch('dotenv.load_dotenv', return_value=False), \\
     patch('database.verify_production_relationship_schema') as verify, \\
     patch('database.db.create_all', side_effect=AssertionError('bootstrap create')), \\
     patch.object(Engine, 'connect', side_effect=AssertionError('unexpected connection')):
    import app
    verify.assert_called_once()
    assert app.app.config['EGGSOR_PRODUCTION_DB_MODE']
    assert not app.app.config['LIFECYCLE_WRITES_ENABLED']
'''
        result = subprocess.run([sys.executable, "-c", code], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
