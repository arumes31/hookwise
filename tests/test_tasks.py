import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import cleanup_logs, run_llm_rca


@pytest.fixture
def app():
    # Set GUI_PASSWORD to avoid RuntimeError on startup
    os.environ["GUI_PASSWORD"] = "test-pass"
    # Ensure DATABASE_URL is set before create_app
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@patch("hookwise.tasks.redis_client")
def test_cleanup_logs(mock_redis, app):
    """Test that cleanup_logs removes old logs based on retention period from Redis."""
    # Set retention to 7 days for the test
    mock_redis.get.return_value = b"7"

    with app.app_context():
        # Create a config
        config = WebhookConfig(name="Test Config", board="Test Board")
        db.session.add(config)
        db.session.commit()

        now = datetime.now(timezone.utc)

        # 1. Log older than 7 days (should be deleted)
        old_log = WebhookLog(
            config_id=config.id,
            request_id="old-req",
            payload="{}",
            status="processed",
            created_at=now - timedelta(days=10)
        )

        # 2. Log newer than 7 days (should be kept)
        new_log = WebhookLog(
            config_id=config.id,
            request_id="new-req",
            payload="{}",
            status="processed",
            created_at=now - timedelta(days=5)
        )

        db.session.add(old_log)
        db.session.add(new_log)
        db.session.commit()

        # Initial check
        assert WebhookLog.query.count() == 2

        # Run cleanup
        # Using .run() to call the task function directly if it's a celery task
        if hasattr(cleanup_logs, "run"):
            cleanup_logs.run()
        else:
            cleanup_logs()

        # Verify
        remaining_logs = WebhookLog.query.all()
        assert len(remaining_logs) == 1
        assert remaining_logs[0].request_id == "new-req"

@patch("hookwise.tasks.redis_client")
def test_cleanup_logs_default_retention(mock_redis, app):
    """Test cleanup_logs using default retention from environment."""
    # No redis value
    mock_redis.get.return_value = None

    with patch.dict(os.environ, {"LOG_RETENTION_DAYS": "15"}):
        with app.app_context():
            # Create a config
            config = WebhookConfig(name="Test Config", board="Test Board")
            db.session.add(config)
            db.session.commit()

            now = datetime.now(timezone.utc)

            # Log older than 15 days
            old_log = WebhookLog(
                config_id=config.id,
                request_id="old-req",
                payload="{}",
                status="processed",
                created_at=now - timedelta(days=20)
            )

            # Log newer than 15 days
            new_log = WebhookLog(
                config_id=config.id,
                request_id="new-req",
                payload="{}",
                status="processed",
                created_at=now - timedelta(days=10)
            )

            db.session.add(old_log)
            db.session.add(new_log)
            db.session.commit()

            if hasattr(cleanup_logs, "run"):
                cleanup_logs.run()
            else:
                cleanup_logs()

            remaining_logs = WebhookLog.query.all()
            assert len(remaining_logs) == 1
            assert remaining_logs[0].request_id == "new-req"


def test_run_llm_rca_success():
    """Test run_llm_rca returns ok status when call_llm succeeds."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = "Everything is fine."
        result = run_llm_rca("config_123", {"key": "value"}, "Template")

        assert result["status"] == "ok"
        assert result["rca"] == "Everything is fine."
        mock_call.assert_called_once()


def test_run_llm_rca_no_response():
    """Test run_llm_rca returns error status when call_llm returns no result."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = None
        result = run_llm_rca("config_123", {"key": "value"}, None)

        assert result["status"] == "error"
        assert "LLM returned no response" in result["rca"]


def test_run_llm_rca_exception():
    """Test run_llm_rca handles exceptions from call_llm."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.side_effect = Exception("Connection failed")
        result = run_llm_rca("config_123", {"key": "value"}, None)

        assert result["status"] == "error"
        assert "LLM error: Exception" in result["rca"]
