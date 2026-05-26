import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.client import ConnectWiseError, TicketNotFoundError
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import check_webhook_timeouts

# Pre-set environment variables for process-level consistency
db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_tasks_")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"


@pytest.fixture(scope="session")
def app():
    """Session-wide test application."""
    app = create_app()
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(autouse=True)
def clean_db(app):
    """Clean database before each test."""
    with app.app_context():
        db.session.query(WebhookLog).delete()
        db.session.query(WebhookConfig).delete()
        db.session.commit()


@pytest.fixture
def mock_cw():
    """Mock ConnectWise client."""
    with patch("hookwise.tasks.cw_client") as m:
        yield m


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    with patch("hookwise.tasks.redis_client") as m:
        yield m


def test_timeout_recent_activity(app, mock_cw, mock_redis):
    """Test that recent activity (within timeout) does not trigger an alert."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Recent Activity",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=1),
        )
        db.session.add(config)
        db.session.commit()

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.create_ticket.assert_not_called()


def test_timeout_fallback_to_created_at(app, mock_cw, mock_redis):
    """Test that created_at is used if last_seen_at is None."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Fallback CreatedAt",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=None,
        )
        config.created_at = now - timedelta(hours=3)
        db.session.add(config)
        db.session.commit()

        mock_cw.create_ticket.return_value = {"id": 101}
        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 101


def test_timeout_triggers_first_alert(app, mock_cw, mock_redis):
    """Test that a timeout triggers ticket creation when no prior alert exists."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="First Alert",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=3),
            customer_id_default="TESTCO",
            board="Test Board",
            status="New",
            priority="P1",
            ticket_type="Service",
        )
        db.session.add(config)
        db.session.commit()

        mock_cw.create_ticket.return_value = {"id": 202}

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 202
        assert config.last_stale_alert_at is not None

        log = WebhookLog.query.filter_by(config_id=config.id).first()
        assert log is not None
        assert "Created timeout ticket #202" in log.error_message


def test_timeout_repeat_alert_suppressed(app, mock_cw, mock_redis):
    """Test that no new alert is sent if the repeat interval has not been reached."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Suppressed Repeat",
            timeout_alerts_enabled=True,
            timeout_hours=24,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=30),
            last_stale_alert_at=now - timedelta(hours=1),
            timeout_ticket_id=303,
        )
        db.session.add(config)
        db.session.commit()

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.get_ticket.assert_not_called()
        mock_cw.create_ticket.assert_not_called()


def test_timeout_repeat_alert_ticket_still_open(app, mock_cw, mock_redis):
    """Test that if repeat interval reached but ticket still open, no new ticket is created."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Open Ticket Repeat",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=404,
        )
        db.session.add(config)
        db.session.commit()

        mock_cw.get_ticket.return_value = {"id": 404, "closedFlag": False}

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.get_ticket.assert_called_once_with(404)
        mock_cw.create_ticket.assert_not_called()


def test_timeout_repeat_alert_ticket_closed(app, mock_cw, mock_redis):
    """Test that if repeat interval reached and ticket CLOSED, a new ticket IS created."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Closed Ticket Repeat",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=505,
        )
        db.session.add(config)
        db.session.commit()

        mock_cw.get_ticket.return_value = {"id": 505, "closedFlag": True}
        mock_cw.create_ticket.return_value = {"id": 606}

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.get_ticket.assert_called_once_with(505)
        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 606


def test_timeout_ticket_not_found(app, mock_cw, mock_redis):
    """Test handling of TicketNotFoundError by clearing ID and creating NEW ticket."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="Missing Ticket",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=707,
        )
        db.session.add(config)
        db.session.commit()

        mock_cw.get_ticket.side_effect = TicketNotFoundError("Not Found")
        mock_cw.create_ticket.return_value = {"id": 808}

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.get_ticket.assert_called_once_with(707)
        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 808


def test_timeout_connectwise_error(app, mock_cw, mock_redis):
    """Test handling of ConnectWiseError (transient). Should NOT create new ticket."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        config = WebhookConfig(
            name="CW Error",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=909,
        )
        db.session.add(config)
        db.session.commit()

        mock_cw.get_ticket.side_effect = ConnectWiseError("API Down")

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.get_ticket.assert_called_once_with(909)
        mock_cw.create_ticket.assert_not_called()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 909


def test_timeout_no_activity_at_all(app, mock_cw, mock_redis):
    """Test behavior when both last_seen_at and created_at are missing."""
    with app.app_context():
        config = WebhookConfig(
            name="No Activity",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            last_seen_at=None,
        )
        db.session.add(config)
        db.session.commit()
        config.created_at = None
        db.session.commit()

        with patch("hookwise.tasks.db", db):
            check_webhook_timeouts()

        mock_cw.create_ticket.assert_not_called()
