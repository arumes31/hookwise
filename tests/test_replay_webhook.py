import json
from unittest.mock import ANY, patch

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
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def authenticated_client(client, app):
    with app.app_context():
        user = User(username="admin", password_hash="hash", role="admin")
        db.session.add(user)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess["username"] = user.username
            sess["role"] = user.role
    return client


@pytest.fixture
def sample_log(app):
    with app.app_context():
        config = WebhookConfig(name="Test Config")
        db.session.add(config)
        db.session.commit()

        log = WebhookLog(
            config_id=config.id, request_id="original_req_id", payload=json.dumps({"key": "value"}), status="processed"
        )
        db.session.add(log)
        db.session.commit()
        return log.id


@patch("hookwise.api.process_webhook_task.delay")
@patch("hookwise.api.log_to_web")
def test_replay_webhook_success(mock_log_to_web, mock_delay, authenticated_client, sample_log):
    """Test successful replay of a webhook log."""
    response = authenticated_client.post(f"/history/replay/{sample_log}")

    assert response.status_code == 200
    assert response.json["status"] == "success"
    assert "Replay queued" in response.json["message"]

    mock_delay.assert_called_once_with(ANY, {"key": "value"}, ANY)
    mock_log_to_web.assert_called_once()


def test_replay_webhook_unauthorized(client, sample_log):
    """Test that unauthorized replay is redirected or blocked."""
    response = client.post(f"/history/replay/{sample_log}")
    # auth_required typically redirects to login for GET, or might return 302/401
    assert response.status_code in [302, 401]


def test_replay_webhook_not_found(authenticated_client):
    """Test replay of non-existent log ID."""
    # This should trigger a 404
    response = authenticated_client.post("/history/replay/non-existent-id")
    assert response.status_code == 404
