from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import process_webhook_task


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["WTF_CSRF_ENABLED"] = False
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@patch("hookwise.tasks.handle_webhook_logic")
@patch("hookwise.tasks.logger")
def test_process_webhook_task_max_retries_exceeded(mock_logger, mock_handle_logic, app, client):
    """Test that process_webhook_task moves to DLQ when max retries exceeded."""

    mock_handle_logic.side_effect = Exception("Some fatal error")

    # Mock self for the Celery task
    mock_self = MagicMock()
    mock_self.request.retries = 5
    mock_self.max_retries = 5

    with app.app_context():
        # Create a config
        config = WebhookConfig(name="Test Config", bearer_token="token")
        db.session.add(config)
        db.session.commit()

        # Create a log entry
        request_id = "test-request-id"
        log_entry = WebhookLog(
            config_id=config.id,
            request_id=request_id,
            payload="{}",
            status="queued"
        )
        db.session.add(log_entry)
        db.session.commit()

        # Access the original function directly
        orig_func = process_webhook_task.run.__func__
        orig_func(
            mock_self,
            config.id,
            {},
            request_id
        )

        # Verify the log entry was updated to 'dlq'
        updated_log = WebhookLog.query.filter_by(request_id=request_id).first()
        assert updated_log.status == "dlq"
        assert "Max retries exceeded: Some fatal error" in updated_log.error_message
        assert updated_log.retry_count == 5

        # Verify logger was called
        mock_logger.error.assert_called_with("Task failed (Attempt 5/5): Some fatal error")

@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_retry_path(mock_handle_logic, app, client):
    """Test that process_webhook_task triggers retry when below max retries."""

    mock_handle_logic.side_effect = Exception("Retryable error")

    # Mock self for the Celery task
    mock_self = MagicMock()
    mock_self.request.retries = 2
    mock_self.max_retries = 5
    # Celery's self.retry usually raises an exception to stop task execution
    mock_self.retry.side_effect = Exception("Retry Exception")

    with app.app_context():
        orig_func = process_webhook_task.run.__func__
        with pytest.raises(Exception, match="Retry Exception"):
            orig_func(
                mock_self,
                "some-id",
                {},
                "some-req-id"
            )

        mock_self.retry.assert_called_once()
        args, kwargs = mock_self.retry.call_args
        assert kwargs["exc"].args[0] == "Retryable error"
        assert "countdown" in kwargs
