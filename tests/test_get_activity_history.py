import pytest
from unittest.mock import patch
from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookLog, WebhookConfig
from datetime import datetime, timezone
import os

@pytest.fixture
def app():
    # Ensure GUI_PASSWORD is set for app startup
    os.environ["GUI_PASSWORD"] = "admin"
    app = create_app()
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as mock_tasks,          patch("hookwise.api.redis_client") as mock_api:
        mock_tasks.get.return_value = None
        mock_api.get.return_value = None
        yield (mock_tasks, mock_api)

def test_get_activity_history_empty(client, app):
    with app.app_context():
        with client.session_transaction() as sess:
            sess['user_id'] = 'test-user'
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        response = client.get('/api/activity/history')
        assert response.status_code == 200
        assert response.json == []

def test_get_activity_history_with_logs(client, app):
    with app.app_context():
        config = WebhookConfig(name="Test Config")
        db.session.add(config)
        db.session.commit()

        log1 = WebhookLog(
            config_id=config.id,
            request_id="req1",
            payload='{"key": "value"}',
            status="processed",
            action="create",
            ticket_id=123,
            created_at=datetime.now(timezone.utc)
        )
        log2 = WebhookLog(
            config_id=config.id,
            request_id="req2",
            payload='invalid json',
            status="failed",
            error_message="Some error",
            created_at=datetime.now(timezone.utc)
        )
        db.session.add_all([log1, log2])
        db.session.commit()

        with client.session_transaction() as sess:
            sess['user_id'] = 'test-user'
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        response = client.get('/api/activity/history')
        assert response.status_code == 200
        data = response.json
        assert len(data) == 2

        log_failed = next(l for l in data if l['level'] == 'error')
        assert log_failed['message'] == "Some error"
        assert log_failed['payload'] == {"raw": "invalid json"}

        log_processed = next(l for l in data if l['level'] == 'warning')
        assert log_processed['message'] == "Created NEW ticket (ID: 123)"
        assert log_processed['payload'] == {"key": "value"}
