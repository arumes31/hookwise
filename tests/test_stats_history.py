from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture
def app():
    from hookwise import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def auth_session(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "123"
        sess["username"] = "admin"
        sess["role"] = "admin"
    return client


@patch("hookwise.tasks.redis_client")
def test_get_stats_history_daily(mock_redis, client, auth_session):
    mock_redis.get.return_value = None
    # Setup: Create some logs
    config = WebhookConfig(name="Test Config", bearer_token="test")
    db.session.add(config)
    db.session.commit()

    now = datetime.now(timezone.utc)
    log1 = WebhookLog(
        config_id=config.id, request_id="1", payload="{}", status="processed", action="create", created_at=now
    )
    log2 = WebhookLog(
        config_id=config.id,
        request_id="2",
        payload="{}",
        status="processed",
        action="update",
        created_at=now - timedelta(days=1),
    )
    db.session.add_all([log1, log2])
    db.session.commit()

    response = client.get("/api/stats/history?period=daily")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 7
    # Check today's data
    today_str = now.strftime("%m-%d")
    today_data = next(item for item in data if item["date"] == today_str)
    assert today_data["created"] == 1
    assert today_data["updated"] == 0


@patch("hookwise.tasks.redis_client")
def test_get_stats_history_weekly(mock_redis, client, auth_session):
    mock_redis.get.return_value = None
    config = WebhookConfig(name="Test Config", bearer_token="test")
    db.session.add(config)
    db.session.commit()

    now = datetime.now(timezone.utc)
    log1 = WebhookLog(
        config_id=config.id, request_id="1", payload="{}", status="processed", action="create", created_at=now
    )
    db.session.add(log1)
    db.session.commit()

    response = client.get("/api/stats/history?period=weekly")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 4

    year, week, _ = now.isocalendar()
    current_week_str = f"W{week}"
    current_week_data = next(item for item in data if item["date"] == current_week_str)
    assert current_week_data["created"] == 1


@patch("hookwise.tasks.redis_client")
def test_get_stats_history_monthly(mock_redis, client, auth_session):
    mock_redis.get.return_value = None
    config = WebhookConfig(name="Test Config", bearer_token="test")
    db.session.add(config)
    db.session.commit()

    now = datetime.now(timezone.utc)
    log1 = WebhookLog(
        config_id=config.id, request_id="1", payload="{}", status="processed", action="create", created_at=now
    )
    db.session.add(log1)
    db.session.commit()

    response = client.get("/api/stats/history?period=monthly")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 6

    month_name = now.strftime("%b")
    current_month_data = next(item for item in data if item["date"] == month_name)
    assert current_month_data["created"] == 1
