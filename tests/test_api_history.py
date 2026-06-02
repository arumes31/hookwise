from datetime import datetime, timezone

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User, WebhookConfig, WebhookLog


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["GUI_PASSWORD"] = "testpass"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def test_get_activity_history(client, app):
    """Test the get_activity_history API endpoint."""
    with app.app_context():
        # Setup test data
        config = WebhookConfig(id="test-config", name="Test Config")
        db.session.add(config)

        user = User(username="admin", password_hash="hash")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

        log1 = WebhookLog(
            config_id="test-config",
            request_id="req-1",
            payload='{"key": "val"}',
            status="processed",
            action="create",
            ticket_id=123,
            created_at=datetime.now(timezone.utc),
        )
        log2 = WebhookLog(
            config_id="test-config",
            request_id="req-2",
            payload="invalid-json",
            status="failed",
            error_message="Something went wrong",
            created_at=datetime.now(timezone.utc),
        )
        db.session.add_all([log1, log2])
        db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "admin"

    response = client.get("/api/activity/history")
    assert response.status_code == 200
    data = response.json
    assert len(data) >= 2

    processed_log = next(log_item for log_item in data if log_item.get("ticket_id") == 123)
    assert processed_log["message"] == "Created NEW ticket (ID: 123)"
    assert processed_log["level"] == "warning"
    assert processed_log["payload"] == {"key": "val"}
    assert processed_log["config_name"] == "Test Config"

    failed_log = next(log_item for log_item in data if log_item["message"] == "Something went wrong")
    assert failed_log["level"] == "error"
    assert failed_log["payload"] == {"raw": "invalid-json"}
