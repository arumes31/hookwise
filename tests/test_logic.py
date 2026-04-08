import json
import os
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import handle_webhook_logic
from hookwise.utils import resolve_jsonpath


@pytest.fixture
def app():
    import tempfile

    # Use a unique temporary file for the sqlite database to ensure process isolation
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_")
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
            pass  # Fallback for Windows lock issues in some environments


@pytest.fixture
def client(app):
    return app.test_client()


def test_resolve_jsonpath():
    data = {"status": "down", "monitor": {"name": "Test Server"}, "details": [{"id": 1, "msg": "Error"}]}
    assert resolve_jsonpath(data, "$.status") == "down"
    assert resolve_jsonpath(data, "$.monitor.name") == "Test Server"
    assert resolve_jsonpath(data, "$.details[0].msg") == "Error"
    assert resolve_jsonpath(data, "$.invalid") is None


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_logic_with_jsonpath(mock_cw, mock_redis, app):
    """Test that JSON mapping fields are resolved and passed to create_ticket."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 42}

    with app.app_context():
        config = WebhookConfig(
            name="Test Mapping",
            json_mapping=json.dumps({"summary": "$.alert_name", "description": "$.extra_info"}),
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "status": "down",
            "alert_name": "Mapped Server Down",
            "extra_info": "Detailed error message here",
            "monitor": {"name": "TestMonitor"},
        }

        handle_webhook_logic(config_id, data, "req-mapping-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert "Mapped Server Down" in call_kwargs["summary"]


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_logic_with_routing_rules(mock_cw, mock_redis, app):
    """Test that routing rule overrides are applied when regex matches."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 99}

    with app.app_context():
        config = WebhookConfig(
            name="Test Routing",
            routing_rules=json.dumps(
                [
                    {
                        "path": "$.severity",
                        "regex": "critical",
                        "overrides": {"board": "Critical Board", "priority": "P1"},
                    }
                ]
            ),
            board="Default Board",
            priority="P3",
            trigger_field="status",
            open_value="down",
            close_value="up",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"status": "down", "severity": "CRITICAL alert!", "monitor": {"name": "ServerX"}}

        handle_webhook_logic(config_id, data, "req-routing-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert call_kwargs["board"] == "Critical Board"
        assert call_kwargs["priority"] == "P1"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_close_ticket_on_up_signal(mock_cw, mock_redis, app):
    """Test that an UP signal closes an existing ticket."""
    mock_redis.get.return_value = b"42"  # Cached ticket ID
    mock_cw.close_ticket.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Test Close",
            trigger_field="heartbeat.status",
            open_value="0",
            close_value="1",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"heartbeat": {"status": "1"}, "monitor": {"name": "TestServer"}, "msg": "UP"}

        handle_webhook_logic(config_id, data, "req-close-1")

        mock_cw.close_ticket.assert_called_once()
        args = mock_cw.close_ticket.call_args
        assert args[0][0] == 42  # ticket_id


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_close_ticket_with_custom_status(mock_cw, mock_redis, app):
    """Test that an UP signal closes a ticket with a custom status name."""
    mock_redis.get.return_value = b"123"
    mock_cw.close_ticket.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Test Custom Close",
            trigger_field="status",
            open_value="0",
            close_value="1",
            close_status="Completed",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"status": "1", "monitor": {"name": "CustomServer"}, "msg": "UP"}
        handle_webhook_logic(config_id, data, "req-custom-close-1")

        mock_cw.close_ticket.assert_called_once()
        call_args = mock_cw.close_ticket.call_args
        assert call_args.args[0] == 123  # ticket_id
        call_kwargs = call_args.kwargs
        assert call_kwargs["status_name"] == "Completed"
        mock_redis.delete.assert_called_once()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_maintenance_window_blocks_processing(mock_cw, mock_redis, app):
    """Test that webhooks during a maintenance window are skipped."""
    import json
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")

    with app.app_context():
        config = WebhookConfig(
            name="Test Maintenance",
            maintenance_windows=json.dumps(
                [{"type": "weekly", "days": [now.strftime("%a")], "start": start, "end": end}]
            ),
            trigger_field="heartbeat.status",
            open_value="0",
            close_value="1",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"heartbeat": {"status": "0"}, "monitor": {"name": "MaintServer"}}

        handle_webhook_logic(config_id, data, "req-maint-1")

        # Should NOT create a ticket during maintenance
        mock_cw.create_ticket.assert_not_called()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_timeout_alerts(mock_cw, mock_redis, app):
    """Test that a timeout triggers a ticket and a new webhook closes it."""
    from datetime import datetime, timedelta, timezone

    from hookwise.tasks import check_webhook_timeouts, handle_webhook_logic

    with app.app_context():
        # 1. Create endpoint with 2-hour timeout
        config = WebhookConfig(
            name="Timeout Test",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            board="Test Board",
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        # 2. Run timeout check
        mock_cw.create_ticket.return_value = {"id": 999}
        mock_cw.find_open_ticket.return_value = None  # Ensure it doesn't return a MagicMock
        check_webhook_timeouts()

        # Verify ticket was created
        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 999

        # 3. Simulate new webhook arrival
        mock_cw.close_ticket.return_value = True
        handle_webhook_logic(config_id, {"status": "ok"}, "request-123")

        # Verify ticket was closed
        from unittest.mock import ANY

        mock_cw.close_ticket.assert_called_once_with(999, ANY, status_name=config.close_status)
        db.session.refresh(config)
        assert config.timeout_ticket_id is None


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_maintenance_window_resolves_timeout(mock_cw, mock_redis, app):
    """Test that a webhook during maintenance still resolves an open timeout alert."""
    from datetime import datetime, timedelta, timezone
    from hookwise.tasks import handle_webhook_logic

    with app.app_context():
        # 1. Create endpoint with an open timeout ticket and a maintenance window
        # Current time is ~14:00, maintenance "12:00-16:00" will cover it
        config = WebhookConfig(
            name="Maint Resolution Test",
            timeout_alerts_enabled=True,
            timeout_ticket_id=888,
            maintenance_windows="12:00-16:00",
            is_enabled=True,
            is_draft=False,
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id
        old_last_seen = config.last_seen_at

        # 2. Simulate webhook arrival during maintenance
        mock_cw.close_ticket.return_value = True
        handle_webhook_logic(config_id, {"status": "ok"}, "maint-req-1")

        # 3. Verify:
        # - Ticket was closed
        mock_cw.close_ticket.assert_called_once()

        # - Config state updated
        db.session.refresh(config)
        assert config.timeout_ticket_id is None
        assert config.last_seen_at > old_last_seen

        # - But data was NOT pushed to CW (normal maintenance behavior)
        mock_cw.create_ticket.assert_not_called()
        mock_cw.find_open_ticket.assert_not_called()
