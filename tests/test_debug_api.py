import pytest
from flask import session
from hookwise.extensions import db
from hookwise.models import User
from unittest.mock import patch, MagicMock
from werkzeug.security import generate_password_hash


@pytest.fixture
def app():
    # Patch redis_client BEFORE creating the app to avoid connection issues in before_request
    with patch("hookwise.tasks.redis_client") as mock_redis:
        mock_redis.get.return_value = None
        from hookwise import create_app

        app = create_app()
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SECRET_KEY"] = "test-secret"
        app.config["WTF_CSRF_ENABLED"] = False
        yield app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        user = User(username="admin", role="admin", password_hash=generate_password_hash("password"))
        db.session.add(user)
        db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = user.id
                sess["username"] = "admin"
                sess["role"] = "admin"
            yield client
        db.session.remove()
        db.drop_all()


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_debug_process_success(mock_tasks_redis, mock_api_redis, client):
    mock_tasks_redis.get.return_value = None
    mock_api_redis.get.return_value = None

    payload = {
        "payload": {"heartbeat": {"status": 0}, "monitor": {"name": "Test Monitor #CWCOMPANY_ABC"}, "msg": "Test"},
        "config": {
            "trigger_field": "heartbeat.status",
            "open_value": "0",
            "close_value": "1",
            "json_mapping": '{"custom_field": "msg"}',
            "routing_rules": '[{"path": "msg", "regex": "Test", "overrides": {"priority": "High"}}]',
        },
    }

    response = client.post("/api/debug/process", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["results"]["alert_type"] == "OPEN (DOWN)"
    assert data["results"]["custom_field"] == "Test"
    assert data["results"]["priority"] == "High"
    assert data["results"]["company"] == "COMPANY_ABC"
    assert data["results"]["summary"] == "Alert: Test Monitor #CWCOMPANY_ABC"


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_debug_process_no_payload(mock_tasks_redis, mock_api_redis, client):
    mock_tasks_redis.get.return_value = None
    mock_api_redis.get.return_value = None

    response = client.post("/api/debug/process", json={"config": {}})
    assert response.status_code == 400
    assert response.get_json()["message"] == "No sample payload provided"


@patch("hookwise.api.redis_client")
@patch("hookwise.tasks.redis_client")
def test_debug_process_generic_alert(mock_tasks_redis, mock_api_redis, client):
    mock_tasks_redis.get.return_value = None
    mock_api_redis.get.return_value = None

    payload = {
        "payload": {
            "heartbeat": {"status": 2},
            "monitor": {"name": "Test Monitor"},
        },
        "config": {"trigger_field": "heartbeat.status", "open_value": "0", "close_value": "1"},
    }
    response = client.post("/api/debug/process", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["results"]["alert_type"] == "GENERIC"
