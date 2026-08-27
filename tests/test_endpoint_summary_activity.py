from datetime import datetime, timedelta, timezone

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.utils import encrypt_string


@pytest.fixture
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id="summary-user", username="admin", role="admin")


def test_endpoint_summary_is_safe_and_reports_card_telemetry(app, client):
    _login(client)
    now = datetime.now(timezone.utc)
    with app.app_context():
        config = WebhookConfig(
            id="summary-endpoint",
            name="Acme Monitoring",
            board="Service Desk",
            customer_id_default="Acme",
                bearer_token=encrypt_string("very-secret-token-abcdef"),
                bearer_token_last4="cdef",
            last_seen_at=now - timedelta(hours=48),
            timeout_alerts_enabled=True,
            timeout_hours=24,
            config_health_status="WARNING",
        )
        db.session.add(config)
        db.session.add_all(
            [
                WebhookLog(
                    config_id=config.id, request_id="request-a", payload="{}", status="processed", processing_time=0.25
                ),
                WebhookLog(
                    config_id=config.id,
                    request_id="request-b",
                    payload="{}",
                    status="failed",
                    error_message="delivery denied",
                    retry_count=2,
                ),
                WebhookLog(config_id=config.id, request_id="request-c", payload="{}", status="queued"),
                WebhookLog(
                    config_id=config.id,
                    request_id="outside-summary-window",
                    payload="{}",
                    status="failed",
                    created_at=now - timedelta(days=31),
                ),
            ]
        )
        db.session.commit()

    response = client.get("/api/endpoints/summary?token_suffix=cdef")
    assert response.status_code == 200
    body = response.get_json()
    endpoint = body["endpoints"][0]
    assert body["token_matches"] == ["summary-endpoint"]
    assert "bearer_token" not in str(body)
    assert endpoint["queue_depth"] == 1
    assert endpoint["retry_count"] == 0
    assert endpoint["is_stale"] is True
    assert endpoint["is_unhealthy"] is True
    assert endpoint["uptime"] == 50.0
    assert endpoint["last_error"] == "delivery denied"
    assert endpoint["activity_count"] == 3


def test_activity_stream_filters_and_omits_payloads(app, client):
    _login(client)
    with app.app_context():
        config = WebhookConfig(id="activity-endpoint", name="Activity", board="NOC")
        db.session.add(config)
        db.session.add_all(
            [
                WebhookLog(
                    config_id=config.id,
                    request_id="failure-request",
                    payload='{"secret":"no"}',
                    status="failed",
                    action="update",
                    error_message="Timed out",
                ),
                WebhookLog(
                    config_id=config.id, request_id="success-request", payload="{}", status="processed", action="create"
                ),
            ]
        )
        db.session.commit()

    response = client.get("/api/activity/stream?severity=failure&board=NOC&limit=10")
    assert response.status_code == 200
    events = response.get_json()["events"]
    assert len(events) == 1
    assert events[0]["request_id"] == "failure-request"
    assert events[0]["level"] == "danger"
    assert "payload" not in events[0]


def test_annotation_reports_invalid_pinned_type(app, client):
    _login(client)
    with app.app_context():
        config = WebhookConfig(id="annotation-endpoint", name="Annotations")
        log = WebhookLog(config_id=config.id, request_id="annotation-request", payload="{}", status="processed")
        db.session.add_all([config, log])
        db.session.commit()
        log_id = log.id

    response = client.put(
        f"/api/activity/events/{log_id}/annotation",
        json={"text": "valid", "is_pinned": "yes"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "is_pinned must be a boolean."}
