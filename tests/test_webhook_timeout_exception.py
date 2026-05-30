import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app():
    from hookwise import create_app
    from hookwise.extensions import db

    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["GUI_PASSWORD"] = "test-password"

    app = create_app()
    with app.app_context():
        db.create_all()
        yield app

def test_check_webhook_timeouts_outer_exception(app):
    """Test that the outer exception handler in check_webhook_timeouts works correctly."""
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        with patch("hookwise.tasks.logger") as mock_logger:
            with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
                    # Cause an exception when calling filter_by
                    mock_query.filter_by.side_effect = Exception("Database connection lost")

                    check_webhook_timeouts()

                    # Verify logger.error was called with the expected message
                    mock_logger.error.assert_any_call("Webhook timeout check task failed: Database connection lost")

                    # Verify db.session.rollback was called.
                    # It might be called by create_app's admin user check, so use assert_called()
                    mock_rollback.assert_called()

def test_check_webhook_timeouts_inner_exception(app):
    """Test that the inner loop exception handler in check_webhook_timeouts works correctly."""
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        with patch("hookwise.tasks.logger") as mock_logger:
            with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
                    mock_config = MagicMock()
                    mock_config.name = "Exception Test"
                    mock_config.timeout_alerts_enabled = True
                    mock_config.timeout_hours = 2
                    mock_config.is_enabled = True
                    mock_config.is_draft = False
                    mock_config.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=3)
                    mock_config.last_stale_alert_at = None
                    mock_config.timeout_ticket_id = None

                    mock_query.filter_by.return_value.all.return_value = [mock_config]

                    with patch("hookwise.tasks.cw_client") as mock_cw:
                        # Mock cw_client.create_ticket to raise an exception
                        mock_cw.create_ticket.side_effect = Exception("CW API Down")

                        check_webhook_timeouts()

                        # Verify inner logger.error was called
                        expected_msg = "Error processing timeout for endpoint 'Exception Test': CW API Down"
                        mock_logger.error.assert_any_call(expected_msg)

                        # Verify db.session.rollback was called
                        mock_rollback.assert_called()

def test_check_webhook_timeouts_commit_exception(app):
    """Test that an exception during commit in the loop is handled."""
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        with patch("hookwise.tasks.logger") as mock_logger:
            with patch("hookwise.tasks.db.session.rollback") as mock_rollback:
                with patch("hookwise.tasks.WebhookConfig.query") as mock_query:
                    mock_config = MagicMock()
                    mock_config.name = "Commit Fail Test"
                    mock_config.timeout_alerts_enabled = True
                    mock_config.timeout_hours = 2
                    mock_config.is_enabled = True
                    mock_config.is_draft = False
                    mock_config.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=3)
                    mock_config.last_stale_alert_at = None
                    mock_config.timeout_ticket_id = None

                    mock_query.filter_by.return_value.all.return_value = [mock_config]

                    with patch("hookwise.tasks.cw_client") as mock_cw:
                        mock_cw.create_ticket.return_value = {"id": 123}

                        # Mock commit to fail
                        with patch("hookwise.tasks.db.session.commit") as mock_commit:
                            mock_commit.side_effect = Exception("Commit Failed")

                            check_webhook_timeouts()

                            expected_msg = "Error processing timeout for endpoint 'Commit Fail Test': Commit Failed"
                            mock_logger.error.assert_any_call(expected_msg)
                            mock_rollback.assert_called()
