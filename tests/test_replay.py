import json
from unittest.mock import ANY, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


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


@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis to avoid connection errors in before_request check_maintenance."""
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock


@pytest.fixture
def sample_config(app, client):
    with app.app_context():
        config = WebhookConfig(
            name="Test Config", bearer_token="test-token", customer_id_default="TESTCO", board="Test Board"
        )
        db.session.add(config)
        db.session.commit()
        return config.id


def test_replay_webhook_success(client, app, sample_config):
    """Test that a valid log can be replayed."""
    with app.app_context():
        log = WebhookLog(
            config_id=sample_config,
            request_id="original-req-id",
            payload=json.dumps({"test": "data"}),
            status="processed"
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id

    with patch("hookwise.api.process_webhook_task.delay") as mock_delay, \
         patch("hookwise.api.log_to_web") as mock_log_to_web:

        with client.session_transaction() as sess:
            sess["user_id"] = "admin-id"
            sess["username"] = "admin"
            sess["role"] = "admin"

        # Testing the new route alias
        response = client.post(f"/api/logs/{log_id}/replay")
        assert response.status_code == 200
        assert response.json["status"] == "success"
        assert "replay_" in response.json["request_id"]

        mock_delay.assert_called_once_with(sample_config, {"test": "data"}, ANY)
        mock_log_to_web.assert_called_once()


def test_replay_webhook_not_found(client):
    """Test that replaying a non-existent log ID returns a 404."""
    with client.session_transaction() as sess:
        sess["user_id"] = "admin-id"
        sess["username"] = "admin"
        sess["role"] = "admin"

    response = client.post("/api/logs/non-existent-id/replay")
    assert response.status_code == 404


def test_replay_webhook_unauthorized(client):
    """Test that the replay endpoint requires authentication."""
    response = client.post("/api/logs/some-id/replay")
    # auth_required typically redirects to login or returns 401/403
    assert response.status_code in [302, 401, 403]


def test_replay_webhook_invalid_payload(client, app, sample_config):
    """Test replaying a log with invalid JSON payload."""
    with app.app_context():
        log = WebhookLog(
            config_id=sample_config,
            request_id="bad-req-id",
            payload="invalid-json{",
            status="failed"
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id

    with client.session_transaction() as sess:
        sess["user_id"] = "admin-id"
        sess["username"] = "admin"
        sess["role"] = "admin"

    response = client.post(f"/api/logs/{log_id}/replay")
    assert response.status_code == 500
    assert response.json["status"] == "error"
