import json
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User, WebhookConfig, WebhookLog


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
        # Create an admin user
        admin = User(username="admin", password_hash="dummy", role="admin")
        db.session.add(admin)
        db.session.commit()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis globally for these tests to avoid connection errors."""
    # We must patch it where it is IMPORTED in hookwise.tasks AND hookwise.api
    # Actually hookwise.__init__ imports it from .tasks
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock

@pytest.fixture
def auth_client(client, app):
    with client.session_transaction() as sess:
        sess['user_id'] = 'test-user-id'
        sess['username'] = 'admin'
        sess['role'] = 'admin'
    return client

@pytest.fixture
def sample_log(app, client):
    with app.app_context():
        config = WebhookConfig(
            name="Test Config",
            bearer_token="test-token",
            customer_id_default="TESTCO",
            board="Test Board"
        )
        db.session.add(config)
        db.session.flush()

        log = WebhookLog(
            config_id=config.id,
            request_id="original-req-id-very-long",
            payload=json.dumps({"test": "data"}),
            status="processed"
        )
        db.session.add(log)
        db.session.commit()
        return log.id

@patch("hookwise.api.process_webhook_task.delay")
@patch("hookwise.api.log_to_web")
def test_replay_webhook_success(mock_log_to_web, mock_delay, auth_client, sample_log):
    """Test successful replay of a webhook."""
    response = auth_client.post(f"/history/replay/{sample_log}")

    assert response.status_code == 200
    assert response.json["status"] == "success"
    assert "replay_" in response.json["request_id"]

    mock_delay.assert_called_once()
    args, kwargs = mock_delay.call_args
    assert args[1] == {"test": "data"}
    assert args[2].startswith("replay_")

    mock_log_to_web.assert_called_once()

def test_replay_webhook_not_found(auth_client):
    """Test replay with non-existent log ID."""
    response = auth_client.post("/history/replay/non-existent-id")
    assert response.status_code == 404

def test_replay_webhook_unauthorized(client, sample_log):
    """Test replay without authentication."""
    response = client.post(f"/history/replay/{sample_log}")
    # Should redirect to login
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

@patch("hookwise.api.process_webhook_task.delay")
def test_replay_webhook_invalid_json(mock_delay, auth_client, app):
    """Test replay with invalid JSON payload in log."""
    with app.app_context():
        config = WebhookConfig(name="Test Config")
        db.session.add(config)
        db.session.flush()
        log = WebhookLog(
            config_id=config.id,
            request_id="req-1",
            payload="invalid-json",
            status="failed"
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id

    response = auth_client.post(f"/history/replay/{log_id}")
    assert response.status_code == 500
    assert response.json["status"] == "error"
    mock_delay.assert_not_called()
