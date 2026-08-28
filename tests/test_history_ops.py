import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


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
        session.update(user_id="operator-1", username="operator", role="admin")


def _seed(app):
    with app.app_context():
        config = WebhookConfig(id="history-ops-endpoint", name="NOC", board="NOC", rate_limit_per_minute=90)
        log = WebhookLog(
            id="history-ops-log",
            config_id=config.id,
            request_id="correlated-request",
            correlation_id="correlation-123",
            payload='{"safe": true}',
            status="dlq",
            error_message="HTTP 429 retry exhausted",
            error_type="rate_limit",
            retry_count=3,
            processing_time=1.5,
            created_at=datetime.now(timezone.utc),
        )
        db.session.add_all([config, log])
        db.session.commit()


def test_advanced_filters_saved_searches_and_operations(app, client):
    _login(client)
    _seed(app)

    filtered = client.get("/api/history/advanced?request_id=correlated&min_retry=2&dlq_only=true")
    assert filtered.status_code == 200
    assert filtered.get_json()["total"] == 1

    rendered = client.get("/history?request_id=correlated&min_retry=2&dlq_only=true")
    assert rendered.status_code == 200
    assert "correlated-request" in rendered.get_data(as_text=True)

    partial = client.get("/history?partial=true&status=dlq")
    partial_html = partial.get_data(as_text=True)
    assert 'data-history-status="dlq"' in partial_html
    assert "history-diagnostics" in partial_html
    assert "history-retry" in partial_html

    created = client.post(
        "/api/history/saved-searches",
        json={"name": "Dead letters", "filters": {"dlq_only": True, "min_retry": 2}},
    )
    assert created.status_code == 201
    assert client.get("/api/history/saved-searches").get_json()[0]["name"] == "Dead letters"

    operations = client.get("/api/history/operations")
    assert operations.status_code == 200
    data = operations.get_json()
    assert data["dead_letter_queue"] == 1
    assert data["endpoint_rate_limits"] == [
        {
            "id": "history-ops-endpoint",
            "name": "NOC",
            "rate_limit_per_minute": 90,
            "current_minute": 0,
            "utilization_percent": 0.0,
        }
    ]

    diagnostics = client.get("/api/history/history-ops-log/diagnostics")
    assert diagnostics.status_code == 200
    diagnostic_data = diagnostics.get_json()
    assert "payload" not in diagnostic_data
    assert "headers" not in diagnostic_data
    assert "payload" not in diagnostic_data["log"]
    assert diagnostic_data["log"]["correlation_id"] == "correlation-123"


@patch("hookwise.history_ops.process_webhook_task.delay")
def test_retry_and_dlq_replay_are_operator_gated(mock_delay, app, client):
    _login(client)
    _seed(app)

    retry = client.post("/api/history/history-ops-log/retry")
    assert retry.status_code == 202
    assert mock_delay.called

    replay = client.post("/api/history/dlq/replay", json={"ids": ["history-ops-log", "missing-log"]})
    assert replay.status_code == 202
    assert replay.get_json()["errors"] == ["missing-log"]


@patch("hookwise.history_ops.process_webhook_task.delay")
def test_retry_preserves_payload_in_storage_and_masks_history_output(mock_delay, app, client):
    _login(client)
    with app.app_context():
        config = WebhookConfig(id="secret-endpoint", name="Secret endpoint")
        original = WebhookLog(
            id="secret-log",
            config_id=config.id,
            request_id="secret-request",
            payload='{"password": "original-value", "event": "down"}',
            status="dlq",
        )
        db.session.add_all([config, original])
        db.session.commit()

    response = client.post("/api/history/secret-log/retry")

    assert response.status_code == 202
    with app.app_context():
        replay = WebhookLog.query.filter_by(replay_of_log_id="secret-log").one()
        assert json.loads(replay.payload)["password"] == "original-value"
        assert json.loads(replay.masked_payload)["password"] == "***"
    mock_delay.assert_called_once()
    rendered = client.get("/history")
    assert "original-value" not in rendered.get_data(as_text=True)
