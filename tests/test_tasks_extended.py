import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# Set environment variables at the absolute top
db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_tasks_ext_")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="


@pytest.fixture(scope="module")
def app():
    """Module-scoped test application."""
    from hookwise import create_app
    from hookwise.extensions import db

    app_inst = create_app()
    app_inst.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app_inst.config["WTF_CSRF_ENABLED"] = False
    with app_inst.app_context():
        db.create_all()
        yield app_inst
        db.drop_all()

    with app_inst.app_context():
        db.engine.dispose()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass


@pytest.fixture(autouse=True)
def clean_db(app):
    """Clean database before each test."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig, WebhookLog

    with app.app_context():
        db.session.query(WebhookLog).delete()
        db.session.query(WebhookConfig).delete()
        db.session.commit()


def test_timeout_recent_activity(app):
    """Test that recent activity (within timeout) does not trigger alert."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Recent",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=1),
            timeout_hours=2,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                check_webhook_timeouts()
                mock_cw.create_ticket.assert_not_called()


def test_timeout_fallback_to_created_at(app):
    """Test fallback to created_at when last_seen_at is None."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Fallback",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=None,
            timeout_hours=2,
        )
        # Manually set created_at
        c.created_at = now - timedelta(hours=3)
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.create_ticket.return_value = {"id": 101}
                check_webhook_timeouts()
                mock_cw.create_ticket.assert_called_once()
                db.session.refresh(c)
                assert c.timeout_ticket_id == 101


def test_timeout_triggers_first_alert(app):
    """Test first alert creation on timeout."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig, WebhookLog
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="First",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=3),
            timeout_hours=2,
            customer_id_default="CO",
            board="B",
            status="S",
            priority="P",
            ticket_type="T",
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.create_ticket.return_value = {"id": 202}
                check_webhook_timeouts()
                mock_cw.create_ticket.assert_called_once()
                db.session.refresh(c)
                assert c.timeout_ticket_id == 202
                log = WebhookLog.query.filter_by(config_id=c.id).first()
                assert log is not None
                assert "Created timeout ticket #202" in log.error_message


def test_timeout_repeat_alert_suppressed(app):
    """Test repeat alert suppression within the interval."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Suppressed",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            timeout_hours=24,
            last_stale_alert_at=now - timedelta(hours=1),
            timeout_ticket_id=303,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                check_webhook_timeouts()
                mock_cw.get_ticket.assert_not_called()
                mock_cw.create_ticket.assert_not_called()


def test_timeout_repeat_alert_ticket_still_open(app):
    """Test repeat interval reached but ticket is still open in ConnectWise."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Open",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            timeout_hours=2,
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=404,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.get_ticket.return_value = {"id": 404, "closedFlag": False}
                check_webhook_timeouts()
                mock_cw.get_ticket.assert_called_once_with(404)
                mock_cw.create_ticket.assert_not_called()


def test_timeout_repeat_alert_ticket_closed(app):
    """Test repeat alert when previous ticket is manually closed."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Closed",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            timeout_hours=2,
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=505,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.get_ticket.return_value = {"id": 505, "closedFlag": True}
                mock_cw.create_ticket.return_value = {"id": 606}
                check_webhook_timeouts()
                mock_cw.get_ticket.assert_called_once_with(505)
                mock_cw.create_ticket.assert_called_once()
                db.session.refresh(c)
                assert c.timeout_ticket_id == 606


def test_timeout_ticket_not_found(app):
    """Test ticket deletion handling."""
    from hookwise.client import TicketNotFoundError
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="Missing",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            timeout_hours=2,
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=707,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.get_ticket.side_effect = TicketNotFoundError("NF")
                mock_cw.create_ticket.return_value = {"id": 808}
                check_webhook_timeouts()
                mock_cw.get_ticket.assert_called_once_with(707)
                mock_cw.create_ticket.assert_called_once()
                db.session.refresh(c)
                assert c.timeout_ticket_id == 808


def test_timeout_connectwise_error(app):
    """Test handling of transient ConnectWise errors."""
    from hookwise.client import ConnectWiseError
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        now = datetime.now(timezone.utc)
        c = WebhookConfig(
            name="CWE",
            timeout_alerts_enabled=True,
            is_enabled=True,
            is_draft=False,
            last_seen_at=now - timedelta(hours=10),
            timeout_hours=2,
            last_stale_alert_at=now - timedelta(hours=5),
            timeout_ticket_id=909,
        )
        db.session.add(c)
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                mock_cw.get_ticket.side_effect = ConnectWiseError("Err")
                check_webhook_timeouts()
                mock_cw.get_ticket.assert_called_once_with(909)
                mock_cw.create_ticket.assert_not_called()
                db.session.refresh(c)
                assert c.timeout_ticket_id == 909


def test_timeout_no_activity_at_all(app):
    """Test skip when no activity dates are recorded yet."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig
    from hookwise.tasks import check_webhook_timeouts

    with app.app_context():
        c = WebhookConfig(name="NoAct", timeout_alerts_enabled=True, is_enabled=True, is_draft=False, last_seen_at=None)
        db.session.add(c)
        db.session.commit()
        c.created_at = None
        db.session.commit()

        with patch("hookwise.tasks.cw_client") as mock_cw:
            with patch("hookwise.tasks.redis_client"):
                check_webhook_timeouts()
                mock_cw.create_ticket.assert_not_called()
