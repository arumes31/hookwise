import re
from datetime import datetime, timedelta, timezone

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
    assert "<title>Dashboard · HookWise</title>" in html
    assert re.search(r'href="/"[^>]*aria-current="page"', html)
    assert re.search(r'id="kpi-total-endpoints">\s*2\s*</strong>', html)
    assert re.search(r'id="kpi-active-endpoints">\s*1\s*</strong>', html)
    assert re.search(r'id="kpi-events-24h">\s*3\s*</strong>', html)
    assert re.search(r'id="kpi-failures-24h">\s*2\s*</strong>', html)
    assert re.search(r'id="notification-count"[^>]*>\s*3\s*</span>', html)
    assert 'id="hw-rail-griff"' not in html
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


def test_history_marks_utc_timestamps_for_browser_localization(app, client):
    """Render the same UTC instant that the endpoint delivery drawer localizes."""
    _authenticate(client)
    with app.app_context():
        endpoint = WebhookConfig(id="timezone-endpoint", name="Timezone endpoint")
        log = WebhookLog(
            config_id=endpoint.id,
            request_id="timezone-request",
            payload="{}",
            status="processed",
            created_at=datetime(2026, 9, 16, 18, 20, 5, tzinfo=timezone.utc),
        )
        db.session.add_all([endpoint, log])
        db.session.commit()

    html = client.get("/history").get_data(as_text=True)

    assert 'class="hw-local-datetime"' in html
    assert 'datetime="2026-09-16T18:20:05Z"' in html
    assert 'data-utc-label="2026-09-16 18:20:05 UTC"' in html


def test_history_ticket_links_use_configured_connectwise_web_url(app, client, monkeypatch):
    """Render history ticket links with the configured PSA browser host."""
    _authenticate(client)
    monkeypatch.setenv("CW_URL", "https://api.test.com/v4_6_release/apis/3.0")
    monkeypatch.setenv("CW_WEB_URL", "https://psa.test.com")

    with app.app_context():
        endpoint = WebhookConfig(id="ticket-link", name="Ticket link")
        log = WebhookLog(
            config_id=endpoint.id,
            request_id="ticket-link-request",
            payload="{}",
            status="processed",
            ticket_id="405505",
            created_at=datetime.now(timezone.utc),
        )
        db.session.add_all([endpoint, log])
        db.session.commit()

    response = client.get("/history")
    html = response.get_data(as_text=True)
    expected_url = (
        "https://psa.test.com/v4_6_release/services/system_io/Service/fv_sr100_request.rails?service_recid=405505"
    )

    assert response.status_code == 200
    assert expected_url.replace("&", "&amp;") in html
    assert "/service/tickets/405505" not in html
    assert (
        'name="hookwise-ticket-url-template" '
        'content="https://psa.test.com/v4_6_release/services/system_io/Service/'
        'fv_sr100_request.rails?service_recid={ticket_id}"'
    ) in html


def test_webhook_rates_show_success_and_failure_semantics(app, client):
    """Expose distinct successful and failed 24-hour delivery rates."""
    _authenticate(client)
    now = datetime.now(timezone.utc)

    with app.app_context():
        endpoint = WebhookConfig(id="rate-endpoint", name="Rate endpoint")
        db.session.add(endpoint)
        db.session.add_all(
            [
                WebhookLog(
                    config_id=endpoint.id,
                    request_id=f"rate-{status}",
                    payload="{}",
                    status=status,
                    created_at=now,
                )
                for status in ("processed", "skipped", "failed")
            ]
        )
        db.session.commit()

    response = client.get("/webhooks")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "SUCCESS / FAILED 24H" in html
    assert 'data-success-rate24="66.7"' in html
    assert 'data-failure-rate24="33.3"' in html
    assert 'class="hw-rate hw-rate--ok"' in html
    assert 'class="hw-rate hw-rate--crit"' in html


def test_login_uses_full_navigation_so_document_title_updates(client):
    response = client.get("/login")

    assert response.status_code == 200
    login_html = response.get_data(as_text=True)
    assert 'id="login-form"' in login_html
    assert 'hx-boost="false"' in login_html
