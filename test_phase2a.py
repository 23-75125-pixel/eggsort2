"""Offline tests: no database connection, credentials, or fixture writes."""
import os
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy import BigInteger, Integer
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, TIMESTAMP
from sqlalchemy.orm import configure_mappers

from config import ConfigurationError, build_app_config, local_test_database_url
import database
from extensions import db
from models import AuditLog, EggRecord, SortingSession, User

LOCAL_URL = "postgresql://postgres@127.0.0.1:55432/eggsor_restore_test"


class LocalConfigurationTests(unittest.TestCase):
    def config(self, **overrides):
        values = {"SECRET_KEY": "s" * 32, "LOCAL_TEST_MODE": "1",
                  "LOCAL_TEST_DATABASE_URL": LOCAL_URL,
                  "SUPABASE_DB_URL": "postgresql://invalid-production.invalid/prod",
                  "DATABASE_URL": "postgresql://invalid-production.invalid/prod"}
        values.update(overrides)
        with patch.dict(os.environ, values, clear=True):
            return build_app_config()

    def test_production_values_cannot_override_local_target(self):
        config = self.config(LOCAL_HARDWARE_ENABLED="1", AUTO_START_SORTING_ON_LOGIN="1")
        self.assertEqual(config["SQLALCHEMY_DATABASE_URI"].host, "127.0.0.1")
        self.assertFalse(config["LOCAL_HARDWARE_ENABLED"])
        self.assertFalse(config["AUTO_START_SORTING_ON_LOGIN"])
        args = config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]
        self.assertEqual(args["hostaddr"], "127.0.0.1")
        self.assertEqual(args["dbname"], "eggsor_restore_test")
        self.assertIn("default_transaction_read_only=on", args["options"])

    def test_missing_target_never_falls_back(self):
        with self.assertRaises(ConfigurationError):
            self.config(LOCAL_TEST_DATABASE_URL="")

    def test_forgotten_local_mode_cannot_bootstrap_postgres(self):
        with self.assertRaises(ConfigurationError):
            self.config(LOCAL_TEST_MODE="0")

    def test_invalid_targets_and_connection_overrides_rejected(self):
        for url in ("sqlite:///:memory:", LOCAL_URL.replace("127.0.0.1", "localhost"),
                    LOCAL_URL.replace("127.0.0.1", "db.invalid.supabase.co"),
                    LOCAL_URL.replace("55432", "5432"),
                    LOCAL_URL.replace("eggsor_restore_test", "postgres"),
                    LOCAL_URL + "?host=remote.invalid", LOCAL_URL + "?service=production",
                    LOCAL_URL + "?options=-c+default_transaction_read_only=off"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                local_test_database_url(url)

    def test_invalid_mode_is_not_silently_disabled(self):
        with self.assertRaises(ConfigurationError):
            self.config(LOCAL_TEST_MODE="typo")

    def test_errors_do_not_echo_credentials(self):
        with self.assertRaises(ConfigurationError) as error:
            local_test_database_url("postgresql://postgres:private-marker@remote.invalid/db")
        self.assertNotIn("private-marker", str(error.exception))


class ModelTests(unittest.TestCase):
    def test_types_rules_and_cascades(self):
        configure_mappers()
        for model, rule in ((SortingSession, "SET NULL"), (AuditLog, "SET NULL"),
                            (EggRecord, "RESTRICT")):
            fk = next(iter(model.__table__.foreign_key_constraints))
            self.assertEqual(fk.ondelete, rule)
        self.assertIsInstance(SortingSession.user_id.type, Integer)
        self.assertTrue(AuditLog.user_id.nullable)
        for relationship in (User.sorting_sessions.property, User.audit_logs.property,
                             SortingSession.egg_records.property):
            self.assertNotIn("delete", relationship.cascade)
            self.assertNotIn("delete-orphan", relationship.cascade)
            self.assertEqual(relationship.passive_deletes, "all")

    def test_relationship_collections_and_nullable_history(self):
        user = User(username="offline", password="disabled")
        first = SortingSession(session_ref="A", status="historical")
        second = SortingSession(session_ref="B", status="historical")
        user.sorting_sessions.extend([first, second])
        first.egg_records.extend([EggRecord(), EggRecord()])
        user.audit_logs.extend([AuditLog(), AuditLog()])
        self.assertIs(first.user, user)
        self.assertIs(first.egg_records[0].sorting_session, first)
        self.assertEqual(len(user.audit_logs), 2)
        self.assertIsNone(SortingSession(session_ref="legacy").user_id)

    def test_status_checks_and_existing_serialization(self):
        from datetime import datetime, timezone
        self.assertEqual(SortingSession.status.server_default.arg, "starting")
        names = {c.name for c in SortingSession.__table__.constraints}
        self.assertIn("ck_sorting_session_status", names)
        audit = AuditLog(id=1, actor="System", event_type="test", description="test",
                         created_at=datetime.now(timezone.utc))
        self.assertNotIn("user_id", audit.to_dict())
        self.assertEqual(audit.to_dict()["actor"], "System")


class InitializationTests(unittest.TestCase):
    def test_local_initialization_never_bootstraps_or_repairs(self):
        app = Flask("offline-test")
        app.config["LOCAL_TEST_MODE"] = True
        with patch.object(database, "verify_local_test_schema") as verify, \
             patch.object(database.db, "create_all") as create, \
             patch.object(database, "_migrate_user_columns") as migrate, \
             patch.object(database, "_ensure_admin") as admin:
            database.initialize_database(app, admin_email="unused", admin_google_sub="")
        verify.assert_called_once()
        create.assert_not_called()
        migrate.assert_not_called()
        admin.assert_not_called()

    def inspector(self):
        inspector = MagicMock()
        inspector.get_table_names.return_value = list(db.metadata.tables)
        def columns(name, **kwargs):
            return [{"name": c.name, "type": DOUBLE_PRECISION() if c.name == "confidence" else TIMESTAMP(timezone=True) if hasattr(c.type, "timezone") else c.type,
                     "nullable": c.nullable} for c in db.metadata.tables[name].columns]
        inspector.get_columns.side_effect = columns
        inspector.get_pk_constraint.side_effect = lambda name, **kwargs: {
            "constrained_columns": [c.name for c in db.metadata.tables[name].primary_key.columns]}
        def fks(name, **kwargs):
            return [{"name": c.name, "constrained_columns": [c.elements[0].parent.name],
                     "referred_table": c.elements[0].column.table.name,
                     "referred_columns": [c.elements[0].column.name],
                     "referred_schema": "public", "options": {"ondelete": c.ondelete}}
                    for c in db.metadata.tables[name].foreign_key_constraints]
        inspector.get_foreign_keys.side_effect = fks
        return inspector

    def test_verified_schema_accepts_postgres_float_reflection(self):
        database.verify_model_schema(self.inspector())

    def test_missing_schema_and_wrong_deletion_rules_fail(self):
        inspector = self.inspector()
        inspector.get_table_names.return_value = ["user"]
        with self.assertRaises(ConfigurationError):
            database.verify_model_schema(inspector)
        inspector = self.inspector()
        inspector.get_foreign_keys.side_effect = None
        inspector.get_foreign_keys.return_value = []
        with self.assertRaises(ConfigurationError):
            database.verify_model_schema(inspector)

    def test_bigint_schema_drift_rejected(self):
        inspector = self.inspector()
        original = inspector.get_columns.side_effect
        def drift(name, **kwargs):
            columns = original(name, **kwargs)
            if name == "user":
                columns[0]["type"] = BigInteger()
            return columns
        inspector.get_columns.side_effect = drift
        with self.assertRaises(ConfigurationError):
            database.verify_model_schema(inspector)

    def test_live_target_inspection_is_read_only_and_rejects_wrong_server(self):
        fake_db = MagicMock()
        fake_db.engine.url = local_test_database_url(LOCAL_URL)
        connection = fake_db.engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.one.return_value = ('127.0.0.1', 5432, 'postgres')
        with patch.object(database, 'db', fake_db), patch.object(database, 'verify_model_schema') as schema:
            with self.assertRaises(ConfigurationError):
                database.verify_local_test_schema()
            schema.assert_not_called()
        statements = [str(c.args[0]) for c in connection.execute.call_args_list]
        self.assertEqual(statements[0], 'SET TRANSACTION READ ONLY')
        self.assertTrue(all(s.startswith(('SET TRANSACTION READ ONLY', 'SELECT')) for s in statements))
        connection.commit.assert_not_called()

    def test_application_local_import_skips_dotenv_and_all_bootstrap_writes(self):
        import subprocess
        import sys
        environment = dict(os.environ, LOCAL_TEST_MODE='1', LOCAL_TEST_DATABASE_URL=LOCAL_URL,
                           SECRET_KEY='s' * 32)
        code = '''
from unittest.mock import patch
from sqlalchemy.engine import Engine
with patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv read')), \\
     patch('database.verify_local_test_schema') as verify, \\
     patch('database.db.create_all', side_effect=AssertionError('create tables')), \\
     patch('database._ensure_admin', side_effect=AssertionError('admin write')), \\
     patch.object(Engine, 'connect', side_effect=AssertionError('unexpected connection')):
    import app
    verify.assert_called_once()
    assert app.app.config['LOCAL_TEST_MODE']
    assert not app.app.config['AUTO_START_SORTING_ON_LOGIN']
print('Local import performed no database connections or writes.')
'''
        result = subprocess.run([sys.executable, '-c', code], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
