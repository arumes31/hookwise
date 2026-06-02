import json
from unittest.mock import ANY, patch

import pytest
from werkzeug.security import generate_password_hash

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
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        user = User(username="testuser", password_hash=generate_password_hash("password"), role="admin")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        username = user.username
        role = user.role

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = username
        sess["role"] = role
    return client


@pytest.fixture
def sample_config(app):
    with app.app_context():
        config = WebhookConfig(name="Test Config", bearer_token="token")
        db.session.add(config)
        db.session.commit()
        return config.id


@pytest.fixture
def sample_log(app, sample_config):
    with app.app_context():
        log = WebhookLog(
            config_id=sample_config,
            request_id="req_1234567890",
            payload=json.dumps({"key": "value"}),
            status="processed",
        )
        db.session.add(log)
        db.session.commit()
        return log.id


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
@patch("hookwise.api.process_webhook_task.delay")
@patch("hookwise.api.log_to_web")
def test_replay_webhook_success(mock_log_web, mock_delay, mock_redis1, mock_redis2, auth_client, sample_log):
    response = auth_client.post(f"/history/replay/{sample_log}")

    assert response.status_code == 200
    assert response.json["status"] == "success"
    assert response.json["message"] == "Replay queued"
    assert "replay_" in response.json["request_id"]

    mock_delay.assert_called_once_with(ANY, {"key": "value"}, response.json["request_id"])
    mock_log_web.assert_called_once()


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_replay_webhook_not_found(mock_redis1, mock_redis2, auth_client):
    response = auth_client.post("/history/replay/non-existent-id")
    assert response.status_code == 404


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_replay_webhook_unauthorized(mock_redis1, mock_redis2, client, sample_log):
    response = client.post(f"/history/replay/{sample_log}")
    assert response.status_code == 302
    assert "/login" in response.location


@pytest.fixture
def invalid_log(app, sample_config):
    with app.app_context():
        log = WebhookLog(
            config_id=sample_config, request_id="req_invalid", payload="invalid json", status="processed"
        )
        db.session.add(log)
        db.session.commit()
        return log.id


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
@patch("hookwise.api.process_webhook_task.delay")
def test_replay_webhook_invalid_json(mock_delay, mock_redis1, mock_redis2, auth_client, invalid_log):
    response = auth_client.post(f"/history/replay/{invalid_log}")
    assert response.status_code == 500
    assert response.json["status"] == "error"
    mock_delay.assert_not_called()
