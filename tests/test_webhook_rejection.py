from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


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
        config = WebhookConfig(
            name="Disabled Config",
            is_enabled=False,
            bearer_token="test-token",
            customer_id_default="TESTCO",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()
        return config.id


@patch("hookwise.webhook.log_webhook_received")
@patch("hookwise.webhook.db.session.commit")
@patch("hookwise.webhook.db.session.rollback")
@patch("logging.getLogger")
def test_webhook_rejection_log_failure(
    mock_get_logger, mock_rollback, mock_commit, mock_log_received, client, disabled_config
):
    """Test that failure to log a webhook rejection is handled gracefully."""
    # Setup mock logger
    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Make commit fail
    mock_commit.side_effect = Exception("Database error")

    payload = {"test": "data"}
    response = client.post(f"/w/{disabled_config}", json=payload)

    # Verify response is still 403 (disabled)
    assert response.status_code == 403
    assert response.json["status"] == "error"
    assert response.json["message"] == "Endpoint is disabled"

    # Verify rollback was called
    mock_rollback.assert_called_once()

    # Verify error was logged
    # hookwise/webhook.py uses logging.getLogger(__name__)
    mock_get_logger.assert_called_with("hookwise.webhook")
    mock_logger.error.assert_called_with("Failed to log webhook rejection: Database error")
