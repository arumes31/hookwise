from unittest.mock import MagicMock, patch

import pytest

from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture
def app():
    # Properly mock all possible redis_client locations
    with patch("hookwise.tasks.redis_client") as m1, \
         patch("hookwise.api.redis_client") as m2, \
         patch("hookwise.metrics.redis_client") as m3, \
         patch("hookwise.commands.redis_client") as m4:

        m1.get.return_value = None
        m2.get.return_value = None
        m3.get.return_value = None
        m4.get.return_value = None

        from hookwise import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["GUI_PASSWORD"] = "testpass"
        yield app

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
            bearer_token="test-token"
        )
        db.session.add(config)
        db.session.commit()
        return config.id

@patch("hookwise.webhook.log_webhook_received")
@patch("hookwise.webhook.db.session.commit")
@patch("hookwise.webhook.db.session.rollback")
@patch("logging.getLogger")
def test_log_rejection_exception_handling(
    mock_get_logger, mock_rollback, mock_commit, mock_log_received, client, disabled_config
):
    # Setup mock logger
    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Force commit to fail
    mock_commit.side_effect = Exception("Database error")

    # Send request to disabled endpoint
    response = client.post(f"/w/{disabled_config}")

    # Verify response is still 403 (Endpoint is disabled)
    assert response.status_code == 403
    assert response.json["message"] == "Endpoint is disabled"

    # Verify rollback was called
    mock_rollback.assert_called_once()

    # Verify error was logged
    mock_logger.error.assert_called_with("Failed to log webhook rejection: Database error")
