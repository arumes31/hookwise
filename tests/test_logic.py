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
    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    app = create_app()
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_resolve_jsonpath():
    data = {
        "status": "down",
        "monitor": {"name": "Test Server"},
        "details": [{"id": 1, "msg": "Error"}]
    }
    assert resolve_jsonpath(data, "$.status") == "down"
    assert resolve_jsonpath(data, "$.monitor.name") == "Test Server"
    assert resolve_jsonpath(data, "$.details[0].msg") == "Error"
    assert resolve_jsonpath(data, "$.invalid") is None

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
def test_webhook_logic_with_jsonpath(mock_cw, mock_redis, app):
    """Test that JSON mapping fields are resolved and passed to create_ticket."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 42}

    with app.app_context():
        config = WebhookConfig(
            name="Test Mapping",
            json_mapping=json.dumps({
                "summary": "$.alert_name",
                "description": "$.extra_info"
            }),
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
            customer_id_default="TESTCO"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "status": "down",
            "alert_name": "Mapped Server Down",
            "extra_info": "Detailed error message here",
            "monitor": {"name": "TestMonitor"}
        }

        handle_webhook_logic(config_id, data, "req-mapping-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert "Mapped Server Down" in call_kwargs['summary']

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
def test_webhook_logic_with_routing_rules(mock_cw, mock_redis, app):
    """Test that routing rule overrides are applied when regex matches."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 99}

    with app.app_context():
        config = WebhookConfig(
            name="Test Routing",
            routing_rules=json.dumps([
                {
                    "path": "$.severity",
                    "regex": "critical",
                    "overrides": {"board": "Critical Board", "priority": "P1"}
                }
            ]),
            board="Default Board",
            priority="P3",
            trigger_field="status",
            open_value="down",
            close_value="up",
            customer_id_default="TESTCO"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "status": "down",
            "severity": "CRITICAL alert!",
            "monitor": {"name": "ServerX"}
        }

        handle_webhook_logic(config_id, data, "req-routing-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert call_kwargs['board'] == "Critical Board"
        assert call_kwargs['priority'] == "P1"

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
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
            customer_id_default="TESTCO"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "heartbeat": {"status": "1"},
            "monitor": {"name": "TestServer"},
            "msg": "UP"
        }

        handle_webhook_logic(config_id, data, "req-close-1")

        mock_cw.close_ticket.assert_called_once()
        args = mock_cw.close_ticket.call_args
        assert args[0][0] == 42  # ticket_id

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
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
            maintenance_windows=json.dumps([{
                "day": now.strftime("%A"),
                "start": start,
                "end": end
            }]),
            trigger_field="heartbeat.status",
            open_value="0",
            close_value="1",
            board="Test Board"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "heartbeat": {"status": "0"},
            "monitor": {"name": "MaintServer"}
        }

        handle_webhook_logic(config_id, data, "req-maint-1")

        # Should NOT create a ticket during maintenance
        mock_cw.create_ticket.assert_not_called()

