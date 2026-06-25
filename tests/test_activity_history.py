from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture
def app():
    # Mock redis_client before creating the app to avoid connection attempts during startup/hooks
    with patch("hookwise.tasks.redis_client") as mock_redis:
        mock_redis.get.return_value = None
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        yield app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_get_activity_history(mock_tasks_redis, mock_api_redis, client, app):
    mock_api_redis.get.return_value = None
    mock_tasks_redis.get.return_value = None

    with app.app_context():
        # Create a config
        config = WebhookConfig(name="Test Config", id="test-id")
        db.session.add(config)

        # Create some logs
        log1 = WebhookLog(
            config_id="test-id",
            request_id="req-1",
            status="processed",
            action="create",
            ticket_id="123",
            payload='{"key": "val"}',
        )
        log2 = WebhookLog(
            config_id="test-id", request_id="req-2", status="failed", error_message="Boom", payload="not json"
        )
        db.session.add(log1)
        db.session.add(log2)
        db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"

    response = client.get("/api/activity/history")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) >= 2

    # Find log1 in data
    log1_data = next((d for d in data if str(d.get("ticket_id")) == "123"), None)
    assert log1_data is not None, f"Log1 not found in {data}"
    assert log1_data["message"] == "Created NEW ticket (ID: 123)"
    assert log1_data["level"] == "warning"
    assert log1_data["config_name"] == "Test Config"
    assert log1_data["payload"] == {"key": "val"}

    # Find log2 in data
    log2_data = next((d for d in data if d["message"] == "Boom"), None)
    assert log2_data is not None, f"Log2 not found in {data}"
    assert log2_data["level"] == "error"
    assert log2_data["payload"] == {"raw": "not json"}
