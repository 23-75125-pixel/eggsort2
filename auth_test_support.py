"""Disposable auth-only harness: no .env, production engine, mail or hardware.

Loads the real routes under a separate module name. Only authentication tables
are created, avoiding unrelated PostgreSQL-only sorting constraints in SQLite.
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from config import build_app_config
from extensions import db
from models import AuditLog, PasswordResetAttempt, User


def load_auth_app():
    name = "eggsort_auth_test_app"
    if name in sys.modules:
        return sys.modules[name]
    with patch.dict(os.environ, {
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "isolated-auth-tests-only-0123456789abcdef",
        "LOCAL_HARDWARE_ENABLED": "false",
        "AUTO_START_SORTING_ON_LOGIN": "false",
    }, clear=True):
        configuration = build_app_config()
    configuration.update(TESTING=True)
    camera = ModuleType("camera_session")
    camera.CAMERA_SESSION = Mock()
    camera.CameraSessionError = type("CameraSessionError", (RuntimeError,), {})
    bridge = ModuleType("esp32_bridge")
    bridge.ESP32_BRIDGE = Mock()
    detection = ModuleType("detection_service")
    detection.DetectorUnavailableError = type("DetectorUnavailableError", (RuntimeError,), {})
    detection.InvalidFrameError = type("InvalidFrameError", (ValueError,), {})
    detection.detect_frame = Mock(side_effect=AssertionError("Inference forbidden in auth tests"))
    for target in (camera.CAMERA_SESSION, bridge.ESP32_BRIDGE):
        target.start.side_effect = AssertionError("Hardware forbidden in auth tests")
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with (
            patch.dict(sys.modules, {"camera_session": camera, "esp32_bridge": bridge,
                                     "detection_service": detection}),
            patch("dotenv.load_dotenv"),
            patch("config.build_app_config", return_value=configuration),
            patch("database.initialize_database"),
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
        ):
            spec.loader.exec_module(module)
        with module.app.app_context():
            assert db.engine.url.get_backend_name() == "sqlite"
            assert db.engine.url.database in (None, "", ":memory:"), "Auth tests require memory-only SQLite"
            db.metadata.create_all(db.engine, tables=[User.__table__, AuditLog.__table__,
                                                      PasswordResetAttempt.__table__])
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


auth = load_auth_app()
app = auth.app
