import json
import os
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import process_webhook_task


@pytest.fixture
def app():
    import tempfile

    # Use a unique temporary file for the sqlite database to ensure process isolation
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_tasks_")
    os.close(fd)

    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

    # Dispose engine to close all connections and release the file lock on Windows
    with app.app_context():
        db.engine.dispose()

    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_dlq(mock_handle, app):
    """Test that task moves log to DLQ when max retries are exceeded."""
    mock_handle.side_effect = Exception("Something went wrong")

    with app.app_context():
        config = WebhookConfig(name="Test Config", board="Test Board")
        db.session.add(config)
        db.session.commit()

        log = WebhookLog(
            config_id=config.id, request_id="req-123", payload=json.dumps({"test": "data"}), status="queued"
        )
        db.session.add(log)
        db.session.commit()

        mock_self = MagicMock()
        mock_self.request.retries = 5
        mock_self.max_retries = 5

        # Call the task function directly
        process_webhook_task.run.__func__(mock_self, config.id, {"test": "data"}, "req-123")

        db.session.refresh(log)
        assert log.status == "dlq"
        assert "Max retries exceeded" in log.error_message
        assert log.retry_count == 5


@patch("hookwise.tasks.handle_webhook_logic")
def test_process_webhook_task_retry(mock_handle, app):
    """Test that task raises retry when max retries are not yet reached."""
    mock_handle.side_effect = Exception("Temporary error")

    with app.app_context():
        config = WebhookConfig(name="Test Config", board="Test Board")
        db.session.add(config)
        db.session.commit()

        mock_self = MagicMock()
        mock_self.request.retries = 0
        mock_self.max_retries = 5
        mock_self.retry.side_effect = Exception("Retry raised")  # Celery's retry raises an exception

        with pytest.raises(Exception, match="Retry raised"):
            process_webhook_task.run.__func__(mock_self, config.id, {"test": "data"}, "req-123")

        mock_self.retry.assert_called_once()
        _, kwargs = mock_self.retry.call_args
        assert "exc" in kwargs
