import re
from datetime import datetime, timedelta, timezone

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture
def app():
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _authenticate(client):
    with client.session_transaction() as session:
        session["user_id"] = "dashboard-user"
        session["username"] = "admin"
        session["role"] = "admin"


def test_dashboard_kpis_navigation_and_notifications(app, client):
    _authenticate(client)
    now = datetime.now(timezone.utc)

    with app.app_context():
        unhealthy = WebhookConfig(
            id="unhealthy",
            name="Unhealthy Endpoint",
            bearer_token="token-one",
            is_enabled=True,
            config_health_status="ERROR",
            config_health_message="Connection check failed",
        )
        paused = WebhookConfig(
            id="paused",
            name="Paused Endpoint",
            bearer_token="token-two",
            is_enabled=False,
            config_health_status="OK",
        )
        draft = WebhookConfig(
            id="draft",
            name="Draft Endpoint",
            bearer_token="token-three",
            is_draft=True,
        )
        db.session.add_all([unhealthy, paused, draft])
        db.session.add_all(
            [
                WebhookLog(
                    config_id="unhealthy",
                    request_id="processed-request",
                    payload="{}",
                    status="processed",
                    created_at=now,
                ),
                WebhookLog(
                    config_id="unhealthy",
                    request_id="failed-request",
                    payload="{}",
                    status="failed",
                    error_message="Delivery failed",
                    created_at=now,
                ),
                WebhookLog(
                    config_id="paused",
                    request_id="dlq-request",
                    payload="{}",
                    status="dlq",
                    error_message="Retries exhausted",
                    created_at=now,
                ),
                WebhookLog(
                    config_id="unhealthy",
                    request_id="old-request",
                    payload="{}",
                    status="failed",
                    created_at=now - timedelta(days=2),
                ),
                WebhookLog(
                    config_id="draft",
                    request_id="draft-request",
                    payload="{}",
                    status="failed",
                    created_at=now,
                ),
            ]
        )
        db.session.commit()

    response = client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<title>Endpoints · HookWise</title>" in html
    assert re.search(r'href="/"[^>]*aria-current="page"', html)
    assert re.search(r'id="kpi-total-endpoints">\s*2\s*</strong>', html)
    assert re.search(r'id="kpi-active-endpoints">\s*1\s*</strong>', html)
    assert re.search(r'id="kpi-events-24h">\s*3\s*</strong>', html)
    assert re.search(r'id="kpi-failures-24h">\s*2\s*</strong>', html)
    assert re.search(r'id="notification-count"[^>]*>\s*3\s*</span>', html)
    assert "Unhealthy Endpoint needs attention" in html
    assert "Failed webhook: Unhealthy Endpoint" in html
    assert "Dead-lettered webhook: Paused Endpoint" in html
    assert "draft-request" not in html
    assert "old-request" not in html


def test_history_has_page_title_and_active_navigation(client):
    _authenticate(client)

    response = client.get("/history")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<title>History · HookWise</title>" in html
    assert re.search(r'href="/history"[^>]*aria-current="page"', html)


def test_login_uses_full_navigation_so_document_title_updates(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert '<form method="POST" id="login-form" hx-boost="false">' in response.get_data(as_text=True)
