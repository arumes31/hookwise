import json
from datetime import datetime, timezone, timedelta
from hookwise.models import WebhookLog, WebhookConfig, User
from hookwise.extensions import db
from werkzeug.security import generate_password_hash

def test_get_stats_unauthorized(client):
    response = client.get("/api/stats")
    assert response.status_code == 302 # Redirect to login

def test_get_stats_authorized(client, app):
    with app.app_context():
        # Create a user
        user = User(username="admin", password_hash=generate_password_hash("password"))
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["_fresh"] = True

    # Initially all zeros/defaults
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.get_json()
    assert data["created_today"] == 0
    assert data["updated_today"] == 0
    assert data["closed_today"] == 0
    assert data["failed_today"] == 0
    assert data["success_rate"] == 100
    assert data["avg_processing_time"] == 0

    with app.app_context():
        # Create some logs
        config = WebhookConfig(name="Test Config", is_draft=False)
        db.session.add(config)
        db.session.flush()

        today = datetime.now(timezone.utc)

        # Created ticket
        log1 = WebhookLog(
            config_id=config.id,
            status="processed",
            action="create",
            created_at=today,
            processing_time=1.5
        )
        # Updated ticket
        log2 = WebhookLog(
            config_id=config.id,
            status="processed",
            action="update",
            created_at=today,
            processing_time=2.5
        )
        # Failed attempt
        log3 = WebhookLog(
            config_id=config.id,
            status="failed",
            created_at=today,
            processing_time=0.5
        )

        db.session.add_all([log1, log2, log3])
        db.session.commit()

    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.get_json()
    assert data["created_today"] == 1
    assert data["updated_today"] == 1
    assert data["closed_today"] == 0
    assert data["failed_today"] == 1
    # total_today = 3 (log1, log2, log3)
    # successful_attempts = 2 (log1, log2 - both processed)
    # success_rate = 2/3 * 100 = 66.7
    assert data["success_rate"] == 66.7
    # avg_proc = (1.5 + 2.5) / 2 = 2.0
    assert data["avg_processing_time"] == 2.0
