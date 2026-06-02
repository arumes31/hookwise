import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import cleanup_logs, process_webhook_task


@pytest.fixture
def app():
    # Use a unique temporary file for the sqlite database
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_tasks_")
    os.close(fd)

    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["GUI_PASSWORD"] = "testpass"

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False

    # Inject our app into the tasks module to prevent it from creating its own
    import hookwise.tasks

    hookwise.tasks._app = app

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

    # Dispose engine to close all connections
    with app.app_context():
        db.engine.dispose()

    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_dlq(mock_handle, app):
    """Test that task moves to DLQ after max retries."""
    mock_handle.side_effect = Exception("Permanent failure")

    with app.app_context():
        # Setup config and log
        config = WebhookConfig(name="Test Task", bearer_token="token")
        db.session.add(config)
        db.session.commit()

        request_id = "req-dlq-123"
        log_entry = WebhookLog(
            config_id=config.id, request_id=request_id, payload=json.dumps({"test": "data"}), status="queued"
        )
        db.session.add(log_entry)
        db.session.commit()

        # Mock self for the bound task
        mock_self = MagicMock()
        mock_self.request.retries = 5
        mock_self.max_retries = 5

        # Call the task function directly
        process_webhook_task.run.__func__(mock_self, config.id, {"test": "data"}, request_id)

        # Verify status updated to dlq
        db.session.refresh(log_entry)
        assert log_entry.status == "dlq"
        assert "Max retries exceeded" in log_entry.error_message
        assert log_entry.retry_count == 5


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_retry(mock_handle, app):
    """Test that task retries when max retries not reached."""
    mock_handle.side_effect = Exception("Transient failure")

    with app.app_context():
        # Setup config
        config = WebhookConfig(name="Test Task Retry", bearer_token="token")
        db.session.add(config)
        db.session.commit()

        # Mock self for the bound task
        mock_self = MagicMock()
        mock_self.request.retries = 0
        mock_self.max_retries = 5
        mock_self.retry.side_effect = Exception("Retry called")

        # Call the task function directly
        with pytest.raises(Exception, match="Retry called"):
            process_webhook_task.run.__func__(mock_self, config.id, {"test": "data"}, "req-retry-123")

        mock_self.retry.assert_called_once()


@patch("hookwise.tasks.redis_client")
def test_cleanup_logs(mock_redis, app):
    """Test that old logs are cleaned up."""
    mock_redis.get.return_value = None  # Use default 30 days

    with app.app_context():
        config = WebhookConfig(name="Cleanup Test")
        db.session.add(config)
        db.session.commit()

        # Create one old log and one new log
        old_date = datetime.now(timezone.utc) - timedelta(days=31)
        new_date = datetime.now(timezone.utc) - timedelta(days=1)

        old_log = WebhookLog(config_id=config.id, request_id="old", payload="{}", created_at=old_date)
        new_log = WebhookLog(config_id=config.id, request_id="new", payload="{}", created_at=new_date)
        db.session.add_all([old_log, new_log])
        db.session.commit()

        # Run cleanup
        cleanup_logs()

        # Verify only new log remains
        logs = WebhookLog.query.all()
        assert len(logs) == 1
        assert logs[0].request_id == "new"


@patch("hookwise.tasks.redis_client")
def test_cleanup_logs_custom_retention(mock_redis, app):
    """Test cleanup with custom retention from Redis."""
    mock_redis.get.return_value = b"7"

    with app.app_context():
        config = WebhookConfig(name="Cleanup Test Custom")
        db.session.add(config)
        db.session.commit()

        # Create one log older than 7 days, one newer
        old_date = datetime.now(timezone.utc) - timedelta(days=10)
        new_date = datetime.now(timezone.utc) - timedelta(days=5)

        db.session.add(WebhookLog(config_id=config.id, request_id="old", payload="{}", created_at=old_date))
        db.session.add(WebhookLog(config_id=config.id, request_id="new", payload="{}", created_at=new_date))
        db.session.commit()

        cleanup_logs()

        logs = WebhookLog.query.all()
        assert len(logs) == 1
        assert logs[0].request_id == "new"
