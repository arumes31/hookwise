import json
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture
def app():
    # Mock redis_client before creating app
    with patch("hookwise.extensions.redis_client") as mock_redis, \
         patch("hookwise.api.redis_client") as mock_api_redis, \
         patch("hookwise.tasks.redis_client") as mock_tasks_redis:
        mock_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        mock_tasks_redis.get.return_value = None

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
def auth_client(client, app):
    with client.session_transaction() as sess:
        sess['user_id'] = 'test_user'
        sess['username'] = 'admin'
        sess['role'] = 'admin'
    return client

@patch("hookwise.api.redis_client")
def test_dry_run_basic(mock_redis, auth_client, app):
    mock_redis.get.return_value = None
    with app.app_context():
        config = WebhookConfig(
            name="Test Dry Run",
            trigger_field="$.status",
            open_value="down",
            close_value="up",
            customer_id_default="TESTCO",
            board="Test Board"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    payload = {"status": "down", "monitor": {"name": "Test Monitor"}}
    resp = auth_client.post(f"/endpoint/dry-run/{config_id}", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["action"] == "create_ticket"
    assert data["alert_type"] == "DOWN"
    assert "Test Monitor" in data["ticket_summary"]

@patch("hookwise.api.redis_client")
def test_dry_run_maintenance(mock_redis, auth_client, app):
    mock_redis.get.return_value = None
    with app.app_context():
        config = WebhookConfig(name="Maint Test")
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    with patch("hookwise.tasks.is_in_maintenance", return_value=True):
        resp = auth_client.post(f"/endpoint/dry-run/{config_id}", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["action"] == "skip"
        assert data["reason"] == "maintenance_window"

@patch("hookwise.api.redis_client")
def test_dry_run_json_mapping(mock_redis, auth_client, app):
    mock_redis.get.return_value = None
    with app.app_context():
        config = WebhookConfig(
            name="Mapping Test",
            json_mapping=json.dumps({
                "summary": "$.monitor.name is $.status",
                "description": "Details: $.msg"
            }),
            ticket_prefix="Alert:"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    payload = {
        "status": "down",
        "monitor": {"name": "Server1"},
        "msg": "Connection timeout"
    }
    resp = auth_client.post(f"/endpoint/dry-run/{config_id}", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ticket_summary"] == "Alert: Server1 is down"
    assert data["description"] == "Details: Connection timeout"

@patch("hookwise.api.redis_client")
def test_dry_run_routing_rules(mock_redis, auth_client, app):
    mock_redis.get.return_value = None
    with app.app_context():
        config = WebhookConfig(
            name="Routing Test",
            routing_rules=json.dumps([
                {
                    "path": "$.severity",
                    "regex": "critical",
                    "overrides": {"board": "Urgent Board"}
                }
            ]),
            board="Normal Board"
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    payload = {"severity": "This is critical", "status": "down"}
    resp = auth_client.post(f"/endpoint/dry-run/{config_id}", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["board"] == "Urgent Board"

@patch("hookwise.api.redis_client")
def test_dry_run_invalid_json(mock_redis, auth_client, app):
    mock_redis.get.return_value = None
    with app.app_context():
        config = WebhookConfig(name="Invalid JSON Test")
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    resp = auth_client.post(
        f"/endpoint/dry-run/{config_id}",
        data="not json",
        headers={"Content-Type": "application/json"}
    )
    # The current code returns 200 even with empty data if JSON is invalid but silent=True
    assert resp.status_code == 200
