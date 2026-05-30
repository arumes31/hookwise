from unittest.mock import patch, MagicMock
import pytest
from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig

@pytest.fixture
def app():
    # Mock redis_client before it's used in create_app / check_maintenance
    with patch("hookwise.extensions.redis_client") as mock_redis,          patch("hookwise.api.redis_client") as mock_api_redis,          patch("hookwise.tasks.redis_client") as mock_tasks_redis:

        mock_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        mock_tasks_redis.get.return_value = None

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        # Ensure GUI_PASSWORD is set for app startup
        import os
        os.environ["GUI_PASSWORD"] = "test-password"
        yield app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_webhook_rejection_logging_exception(client, app):
    """Test that an exception during webhook rejection logging is handled gracefully."""
    with app.app_context():
        config = WebhookConfig(
            name="Disabled Config",
            is_enabled=False
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    # We need to patch the session that hookwise.webhook is using
    with patch("hookwise.webhook.db.session.commit") as mock_commit:
        mock_commit.side_effect = Exception("Database error")

        with patch("hookwise.webhook.db.session.rollback") as mock_rollback:
            # The import logging is inside the except block,
            # but we can still patch logging.getLogger
            with patch("logging.getLogger") as mock_get_logger:
                mock_logger = MagicMock()
                mock_get_logger.return_value = mock_logger

                response = client.post(f"/w/{config_id}", json={"test": "data"})

                assert response.status_code == 403
                assert response.json["message"] == "Endpoint is disabled"

                mock_rollback.assert_called_once()
                mock_logger.error.assert_called()
                error_msg = mock_logger.error.call_args[0][0]
                assert "Failed to log webhook rejection" in error_msg
                assert "Database error" in error_msg
