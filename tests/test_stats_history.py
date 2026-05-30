from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture
def app():
    # Mock redis_client in hookwise.extensions before it's used
    with patch("hookwise.extensions.redis_client", MagicMock()):
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        yield app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def sample_logs(app, client):
    with app.app_context():
        config = WebhookConfig(name="Test Config", board="Test Board", bearer_token="test")
        db.session.add(config)
        db.session.commit()

        now = datetime.now(timezone.utc)
        logs = [
            WebhookLog(
                config_id=config.id, action="create", status="processed", created_at=now, request_id="1", payload="{}"
            ),
            WebhookLog(
                config_id=config.id, action="update", status="processed", created_at=now, request_id="2", payload="{}"
            ),
            WebhookLog(
                config_id=config.id, action="close", status="processed", created_at=now, request_id="3", payload="{}"
            ),
            WebhookLog(
                config_id=config.id,
                action="create",
                status="processed",
                created_at=now - timedelta(days=1),
                request_id="4",
                payload="{}",
            ),
            WebhookLog(
                config_id=config.id,
                action="create",
                status="failed",
                created_at=now,
                request_id="5",
                payload="{}",
            ),  # Should be ignored
        ]
        for log in logs:
            db.session.add(log)
        db.session.commit()
        return config


def test_get_stats_history_daily(client, sample_logs):
    # Authenticate
    with client.session_transaction() as sess:
        sess["user_id"] = "admin"

    response = client.get("/api/stats/history?period=daily")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 7
    # Last entry should be today
    assert data[-1]["created"] == 1
    assert data[-1]["updated"] == 1
    assert data[-1]["closed"] == 1
    # Entry before that should be yesterday
    assert data[-2]["created"] == 1
    assert data[-2]["updated"] == 0
    assert data[-2]["closed"] == 0


def test_get_stats_history_weekly(client, sample_logs):
    with client.session_transaction() as sess:
        sess["user_id"] = "admin"

    response = client.get("/api/stats/history?period=weekly")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 4
    # Check that at least one week has the data
    found = False
    for week in data:
        if week["created"] >= 2:  # today + yesterday
            found = True
    assert found


def test_get_stats_history_monthly(client, sample_logs):
    with client.session_transaction() as sess:
        sess["user_id"] = "admin"

    response = client.get("/api/stats/history?period=monthly")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 6
    # Check that current month has the data
    assert data[-1]["created"] >= 2
