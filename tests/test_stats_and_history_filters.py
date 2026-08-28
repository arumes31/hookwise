"""Regression tests for the DLQ / no-action stat split and the history
status / source-IP filters.

Locks in the behaviour introduced alongside the dashboard DLQ + No-Action
cards and the Webhook History status / source-IP filters:

* ``/api/stats`` reports ``dlq`` separately from ``failed`` (``failed_today``
  must NOT include dead-lettered logs) and exposes ``dlq_today`` /
  ``non_action_today``.
* ``/history`` filters by ``status`` (exact) and ``source_ip`` (partial).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture(autouse=True)
def mock_redis():
    # check_maintenance (before_request) reads redis; mock it so tests do not
    # require a live Redis server.
    with patch("hookwise.tasks.redis_client") as mock_redis_client:
        mock_redis_client.get.return_value = None
        yield mock_redis_client


@pytest.fixture
def app():
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
        sess["user_id"] = "test-user-id"
        sess["username"] = "admin"
        sess["role"] = "admin"


def _seed(app):
    """Create one config plus one log per status, all timestamped 'now'."""
    with app.app_context():
        cfg = WebhookConfig(name="Cfg", bearer_token="t")
        db.session.add(cfg)
        db.session.commit()
        cfg_id = cfg.id
        now = datetime.now(timezone.utc)

        rows = [
            ("processed", "create", "10.0.0.1"),
            ("processed", "update", "10.0.0.1"),
            ("processed", "close", "10.0.0.2"),
            ("processed", None, "10.0.0.2"),  # processed, no ticket action
            ("skipped", None, "10.0.0.3"),  # skipped -> no action
            ("failed", "create", "192.168.1.9"),  # transient failure
            ("dlq", None, "192.168.1.9"),  # dead-lettered
            ("dlq", "create", "192.168.1.9"),  # dead-lettered
        ]
        for i, (status, action, ip) in enumerate(rows):
            db.session.add(
                WebhookLog(
                    config_id=cfg_id,
                    request_id=f"req-{i}",
                    payload="{}",
                    status=status,
                    action=action,
                    source_ip=ip,
                    created_at=now,
                )
            )
        db.session.commit()
        return cfg_id


def test_stats_splits_dlq_from_failed(client, auth_session, app):
    _seed(app)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()

    # failed_today must exclude dead-lettered logs (the DLQ split).
    assert data["failed_today"] == 1
    assert data["dlq_today"] == 2
    # skipped (1) + processed-with-null-action (1)
    assert data["non_action_today"] == 2
    # existing action buckets unchanged
    assert data["created_today"] == 1
    assert data["updated_today"] == 1
    assert data["closed_today"] == 1


def test_history_status_filter(client, auth_session, app):
    _seed(app)
    resp = client.get("/history?status=dlq")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # 2 dlq rows render; each row carries exactly one data-request-id cell.
    assert body.count("data-request-id=") == 2


def test_history_source_ip_filter_partial(client, auth_session, app):
    _seed(app)
    # partial (ilike) match on the 192.168.x prefix -> failed(1) + dlq(2) = 3
    resp = client.get("/history?source_ip=192.168")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).count("data-request-id=") == 3


def test_history_status_and_source_ip_combined(client, auth_session, app):
    _seed(app)
    resp = client.get("/history?status=dlq&source_ip=192.168.1.9")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).count("data-request-id=") == 2

    # Non-matching combination returns no rows.
    resp = client.get("/history?status=dlq&source_ip=10.0.0.1")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).count("data-request-id=") == 0
