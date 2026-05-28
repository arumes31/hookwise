from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as mock_tasks_redis, patch("hookwise.api.redis_client") as mock_api_redis:
        mock_tasks_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        yield


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
def disabled_config(app, client):
    with app.app_context():
        config = WebhookConfig(name="Disabled Config", is_enabled=False, bearer_token="test-token")
        db.session.add(config)
        db.session.commit()
        return config.id


@patch("hookwise.extensions.db.session.commit")
@patch("hookwise.extensions.db.session.rollback")
@patch("logging.getLogger")
def test_webhook_log_rejection_exception(mock_get_logger, mock_rollback, mock_commit, client, disabled_config):
    """Test that an exception during webhook log rejection is handled and logged."""
    # Force commit to raise an exception
    mock_commit.side_effect = Exception("Database error")

    # Mock the logger to verify error logging
    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    response = client.post(f"/w/{disabled_config}", json={"test": "data"})

    assert response.status_code == 403
    assert response.json["status"] == "error"
    assert "disabled" in response.json["message"].lower()

    # Verify rollback was called
    mock_rollback.assert_called_once()

    # Verify error was logged
    mock_logger.error.assert_called()
    args, _ = mock_logger.error.call_args
    assert "Failed to log webhook rejection" in args[0]
    assert "Database error" in args[0]
