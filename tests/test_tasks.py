import json
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
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@patch("hookwise.tasks.handle_webhook_logic")
@patch("hookwise.tasks.random.uniform")
def test_process_webhook_task_retry(mock_uniform, mock_handle, app):
    """Test that process_webhook_task retries on failure when under max_retries."""
    mock_handle.side_effect = Exception("Temporary logic failure")
    mock_uniform.return_value = 1.0

    mock_self = MagicMock()
    mock_self.request.retries = 2
    mock_self.max_retries = 5
    mock_self.retry.side_effect = Exception("Retry raised")  # Celery retry raises an exception

    # Use .run.__func__ as per project memory for bound tasks in tests
    with pytest.raises(Exception, match="Retry raised"):
        process_webhook_task.run.__func__(mock_self, config_id="config-1", data={"foo": "bar"}, request_id="req-1")

    mock_self.retry.assert_called_once()
    # Check countdown calculation: (2**retries) * jitter = (2**2) * 1.0 = 4.0
    args, kwargs = mock_self.retry.call_args
    assert kwargs["countdown"] == 4.0


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_dlq(mock_handle, app):
    """Test that process_webhook_task moves to DLQ when max_retries is reached."""
    mock_handle.side_effect = Exception("Final logic failure")

    with app.app_context():
        # Setup config and log
        config = WebhookConfig(id="config-1", name="Test Config")
        db.session.add(config)

        log = WebhookLog(
            request_id="req-dlq-1", config_id="config-1", payload=json.dumps({"foo": "bar"}), status="queued"
        )
        db.session.add(log)
        db.session.commit()

        mock_self = MagicMock()
        mock_self.request.retries = 5
        mock_self.max_retries = 5

        process_webhook_task.run.__func__(
            mock_self, config_id="config-1", data={"foo": "bar"}, request_id="req-dlq-1"
        )

        # Verify log entry moved to dlq
        db.session.refresh(log)
        assert log.status == "dlq"
        assert "Max retries exceeded" in log.error_message
        assert log.retry_count == 5
        mock_self.retry.assert_not_called()


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_success(mock_handle, app):
    """Test successful execution of process_webhook_task."""
    mock_self = MagicMock()
    mock_self.request.retries = 0

    process_webhook_task.run.__func__(
        mock_self, config_id="config-1", data={"foo": "bar"}, request_id="req-success-1"
    )

    mock_handle.assert_called_once_with(
        "config-1", {"foo": "bar"}, "req-success-1", source_ip=None, retry_count=0, headers=None
    )
