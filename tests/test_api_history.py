from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture(autouse=True)
def mock_redis():
    # check_maintenance (before_request) reads redis; mock it so tests do
    # not require a live Redis server.
    with patch("hookwise.tasks.redis_client") as mock_redis_client:
        mock_redis_client.get.return_value = None
        yield mock_redis_client


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def auth_session(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 'test-user-id'
        sess['username'] = 'admin'
        sess['role'] = 'admin'

def test_get_activity_history_empty(client, auth_session, app):
    with app.app_context():
        with patch('hookwise.models.WebhookLog.query') as mock_query:
            mock_query.options.return_value.order_by.return_value.limit.return_value.all.return_value = []

            response = client.get("/api/activity/history")
            assert response.status_code == 200
            assert response.json == []

def test_get_activity_history_processed(client, auth_session, app):
    with app.app_context():
        mock_config = WebhookConfig(name="Test Config")
        mock_log = WebhookLog(
            status="processed",
            action="create",
            ticket_id=123,
            payload='{"key": "value"}',
            created_at=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            config=mock_config
        )

        with patch('hookwise.models.WebhookLog.query') as mock_query:
            mock_query.options.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_log]

            response = client.get("/api/activity/history")
            assert response.status_code == 200
            data = response.json
            assert len(data) == 1
            assert data[0]['message'] == "Created NEW ticket (ID: 123)"
            assert data[0]['level'] == "warning"
            assert data[0]['payload'] == {"key": "value"}
            assert data[0]['config_name'] == "Test Config"

def test_get_activity_history_failed(client, auth_session, app):
    with app.app_context():
        mock_log = WebhookLog(
            status="failed",
            error_message="Something went wrong",
            payload='raw data',
            created_at=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            config=None
        )

        with patch('hookwise.models.WebhookLog.query') as mock_query:
            mock_query.options.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_log]

            response = client.get("/api/activity/history")
            assert response.status_code == 200
            data = response.json
            assert data[0]['message'] == "Something went wrong"
            assert data[0]['level'] == "error"
            assert data[0]['payload'] == {"raw": "raw data"}
            assert data[0]['config_name'] == "System"

def test_get_activity_history_skipped(client, auth_session, app):
    with app.app_context():
        mock_log = WebhookLog(
            status="skipped",
            error_message="Skipped: already exists",
            payload=None,
            created_at=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            config=None
        )

        with patch('hookwise.models.WebhookLog.query') as mock_query:
            mock_query.options.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_log]

            response = client.get("/api/activity/history")
            assert response.status_code == 200
            data = response.json
            assert data[0]['message'] == "Skipped: already exists"
            assert data[0]['level'] == "info"
