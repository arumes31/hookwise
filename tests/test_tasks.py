import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import cleanup_logs


@pytest.fixture
def app():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_tasks_")
    os.close(fd)

    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["GUI_PASSWORD"] = "testpass"

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False

    # Inject app into tasks module to prevent redundant app creation
    import hookwise.tasks
    hookwise.tasks._app = app

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

    # Reset tasks._app
    hookwise.tasks._app = None

@patch("hookwise.tasks.redis_client")
def test_cleanup_logs_default_retention(mock_redis, app):
    """Test cleanup_logs with default 30-day retention."""
    mock_redis.get.return_value = None  # No redis override

    with app.app_context():
        # Create a config
        config = WebhookConfig(name="Test Config")
        db.session.add(config)
        db.session.commit()

        now = datetime.now(timezone.utc)

        # Log 1: 40 days old (should be deleted)
        log1 = WebhookLog(
            config_id=config.id,
            request_id="old-log",
            payload="{}",
            created_at=now - timedelta(days=40)
        )

        # Log 2: 10 days old (should stay)
        log2 = WebhookLog(
            config_id=config.id,
            request_id="new-log",
            payload="{}",
            created_at=now - timedelta(days=10)
        )

        db.session.add_all([log1, log2])
        db.session.commit()

        # Execute cleanup
        # Use .run() to bypass Celery overhead and app creation logic in __call__
        cleanup_logs.run()

        # Verify
        remaining = WebhookLog.query.all()
        assert len(remaining) == 1
        assert remaining[0].request_id == "new-log"

@patch("hookwise.tasks.redis_client")
def test_cleanup_logs_custom_retention(mock_redis, app):
    """Test cleanup_logs with custom retention from redis."""
    # Set retention to 5 days
    mock_redis.get.return_value = b"5"

    with app.app_context():
        # Create a config
        config = WebhookConfig(name="Test Config Custom")
        db.session.add(config)
        db.session.commit()

        now = datetime.now(timezone.utc)

        # Log 1: 10 days old (should be deleted because retention is 5)
        log1 = WebhookLog(
            config_id=config.id,
            request_id="10-day-log",
            payload="{}",
            created_at=now - timedelta(days=10)
        )

        # Log 2: 2 days old (should stay)
        log2 = WebhookLog(
            config_id=config.id,
            request_id="2-day-log",
            payload="{}",
            created_at=now - timedelta(days=2)
        )

        db.session.add_all([log1, log2])
        db.session.commit()

        # Execute cleanup
        cleanup_logs.run()

        # Verify
        remaining = WebhookLog.query.all()
        assert len(remaining) == 1
        assert remaining[0].request_id == "2-day-log"
