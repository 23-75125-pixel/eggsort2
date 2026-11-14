"""Startup response contract: real route, isolated app import, no hardware or external DB."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sorting_lifecycle import LifecycleError
import auth_test_support as lab
from security import CSRF_SESSION_KEY


@pytest.fixture
def client():
    user = SimpleNamespace(id=31, username='isolated', email='isolated@example.invalid',
        display_name='Isolated', avatar_url=None, role='admin', is_active=True, password_set=True)
    with patch.dict(lab.app.config, TESTING=True, LOCAL_HARDWARE_ENABLED=True,
                    AUTO_START_SORTING_ON_LOGIN=False), patch.object(lab.db.session, 'get', return_value=user):
        client = lab.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
            session[CSRF_SESSION_KEY] = 'ss31-csrf'
            session['sorting_runtime_auto_start_suppressed'] = True
            session['sorting_runtime_retry_after'] = 123
        yield client


@pytest.mark.parametrize('failure', [
    LifecycleError('Simulated ESP32 bridge startup failure.'),
    LifecycleError('Incomplete sorting work requires recovery before restart.'),
    lab.auth.CameraSessionError('Camera startup unavailable.'),
])
def test_expected_start_failures_are_json_and_keep_recovery_flags(client, failure):
    with patch.object(lab.auth, 'start_sorting_runtime', side_effect=failure), \
         patch.object(lab.auth, '_write_sorting_audit_warning') as audit:
        response = client.post('/api/camera/start', headers={'X-CSRF-Token': 'ss31-csrf'})
    assert response.status_code == 503
    assert response.is_json
    assert response.get_json() == {'error': str(failure)}
    assert b'<html' not in response.data and b'Traceback' not in response.data
    audit.assert_not_called()
    with client.session_transaction() as session:
        assert session['sorting_runtime_auto_start_suppressed'] is True
        assert session['sorting_runtime_retry_after'] == 123


def test_success_contract_unchanged(client):
    camera = {'running': True, 'lifecycle_status': 'starting', 'session_ref': 'test-session'}
    hardware = {'running': True, 'connected': False}
    with patch.object(lab.auth, 'start_sorting_runtime', return_value=(camera, hardware)), \
         patch.object(lab.auth, '_write_sorting_audit_warning', return_value=None):
        response = client.post('/api/camera/start', headers={'X-CSRF-Token': 'ss31-csrf'})
    assert response.status_code == 200
    assert response.get_json() == {'camera': camera, 'hardware': hardware, 'audit_warning': None}
    with client.session_transaction() as session:
        assert 'sorting_runtime_auto_start_suppressed' not in session
        assert 'sorting_runtime_retry_after' not in session


def test_unexpected_programming_errors_are_not_mislabeled_as_expected_failures(client):
    with patch.object(lab.auth, 'start_sorting_runtime', side_effect=ValueError('unexpected bug')):
        with pytest.raises(ValueError, match='unexpected bug'):
            client.post('/api/camera/start', headers={'X-CSRF-Token': 'ss31-csrf'})
