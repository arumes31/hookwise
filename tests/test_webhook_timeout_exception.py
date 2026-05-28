import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import check_webhook_timeouts


@pytest.fixture
def app():
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_timeout_")
    os.close(fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    # Set GUI_PASSWORD to avoid critical log
    os.environ["GUI_PASSWORD"] = "test-password"
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
    with app.app_context():
        db.engine.dispose()
    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass


def test_check_webhook_timeouts_exception_handling(app):
    """Test that check_webhook_timeouts handles exceptions correctly."""
    with app.app_context():
        # Mock WebhookConfig.query.filter_by(...).all() to raise an exception
        with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
            mock_query.filter_by.return_value.all.side_effect = Exception("Database error")

            with patch("hookwise.tasks.logger") as mock_logger:
                with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                    check_webhook_timeouts()

                    # Verify logger.error was called
                    mock_logger.error.assert_any_call("Webhook timeout check task failed: Database error")

                    # Verify db.session.rollback was called
                    mock_rollback.assert_called()


def test_check_webhook_timeouts_loop_exception_handling(app):
    """Test that check_webhook_timeouts handles exceptions inside the loop correctly."""
    with app.app_context():
        # Create a config that will be processed
        config = WebhookConfig(
            name="Loop Exception Test",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.session.add(config)
        db.session.commit()

        # We want to trigger an exception inside the loop.
        # Let's mock 'WebhookConfig.query.filter_by(...).all()' to return our config,
        # but then mock something used inside the loop.

        with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
            mock_config = MagicMock(spec=WebhookConfig)
            mock_config.name = "Loop Exception Test"
            mock_config.timeout_alerts_enabled = True
            mock_config.is_enabled = True
            mock_config.is_draft = False
            # Trigger exception when accessing last_seen_at
            type(mock_config).last_seen_at = PropertyMock(side_effect=Exception("Loop failure"))

            mock_query.filter_by.return_value.all.return_value = [mock_config]

            with patch("hookwise.tasks.logger") as mock_logger:
                with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                    check_webhook_timeouts()

                    # Verify loop error was logged
                    mock_logger.error.assert_any_call(
                        "Error processing timeout for endpoint 'Loop Exception Test': Loop failure"
                    )
                    # Verify rollback was called for the loop failure
                    mock_rollback.assert_called()


def test_check_webhook_timeouts_final_exception_handling(app):
    """Test that check_webhook_timeouts handles exceptions in the final block correctly."""
    with app.app_context():
        # Mock WebhookConfig.query.filter_by(...).all() to return empty list
        # then mock logger.info to raise an exception in the final block
        with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
            mock_query.filter_by.return_value.all.return_value = []

            with patch("hookwise.tasks.logger") as mock_logger:
                # Trigger exception in the final info log call
                mock_logger.info.side_effect = Exception("Final log failure")

                with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                    check_webhook_timeouts()

                    # Verify final error was logged
                    mock_logger.error.assert_any_call("Webhook timeout check task failed: Final log failure")
                    # Verify rollback was called
                    mock_rollback.assert_called()
