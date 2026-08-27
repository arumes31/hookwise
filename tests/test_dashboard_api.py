from datetime import datetime, timedelta, timezone

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture
def app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id="dashboard-user", username="admin", role="admin")


def _seed(app):
    now = datetime.now(timezone.utc)
    with app.app_context():
        active = WebhookConfig(id="active", name="Active", bearer_token="token", is_enabled=True)
        stale = WebhookConfig(
            id="stale",
            name="Stale",
            bearer_token="token",
            timeout_alerts_enabled=True,
            timeout_hours=1,
            last_seen_at=now - timedelta(hours=2),
        )
        draft = WebhookConfig(id="draft", name="Draft", bearer_token="token", is_draft=True)
        db.session.add_all([active, stale, draft])
        db.session.add_all(
            [
                WebhookLog(
                    config_id="active",
                    request_id="ok",
                    payload="{}",
                    status="processed",
                    processing_time=0.2,
                    created_at=now - timedelta(hours=1),
                ),
                WebhookLog(
                    config_id="active",
                    request_id="skip",
                    payload="{}",
                    status="skipped",
                    processing_time=0.4,
                    created_at=now - timedelta(hours=1),
                ),
                WebhookLog(
                    config_id="stale",
                    request_id="fail",
                    payload="{}",
                    status="failed",
                    processing_time=1.2,
                    created_at=now - timedelta(hours=2),
                ),
                WebhookLog(
                    config_id="stale",
                    request_id="dlq",
                    payload="{}",
                    status="dlq",
                    processing_time=2.4,
                    created_at=now - timedelta(hours=2),
                ),
                WebhookLog(config_id="draft", request_id="draft", payload="{}", status="failed", created_at=now),
            ]
        )
        db.session.commit()


def test_dashboard_overview_returns_live_kpis_deltas_and_exact_filters(app, client):
    _login(client)
    _seed(app)

    response = client.get("/api/dashboard/overview?range=24h")

    assert response.status_code == 200
    data = response.get_json()
    assert data["kpis"] == {
        "total_events": 4,
        "processed_events": 1,
        "successful_events": 2,
        "success_rate": 50.0,
        "average_latency": 1.05,
        "dead_letter_queue": 1,
        "skipped_no_action": 1,
        "failed_events": 2,
        "total_endpoints": 2,
        "active_endpoints": 2,
        "failing_endpoints": 1,
        "stale_endpoints": 1,
    }
    assert data["filters"]["failed_events"] == ["stale"]
    assert data["filters"]["stale_endpoints"] == ["stale"]
    assert "processed_events" in data["deltas"]
    assert data["updated_at"].endswith("+00:00")


def test_dashboard_analytics_has_percentiles_markers_and_timezone(app, client):
    _login(client)
    _seed(app)

    response = client.get("/api/dashboard/analytics?range=24h&timezone=Europe/Vienna")

    assert response.status_code == 200
    data = response.get_json()
    assert data["timezone"] == "Europe/Vienna"
    assert data["points"]
    point = data["points"][0]
    assert {
        "processed",
        "failed",
        "failure_rate",
        "p50",
        "p95",
        "p99",
        "anomaly",
        "busiest",
        "highest_failure",
    } <= point.keys()
    assert any(row["id"] == "active" for row in data["endpoint_activity"])


def test_dashboard_page_contains_live_controls_and_accessible_analytics_table(client):
    _login(client)

    response = client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="dashboard-refresh"' in html
    assert 'id="dashboard-refresh-interval"' in html
    assert 'id="dashboard-kpi-toggles"' in html
    assert 'id="dashboard-chart-table"' in html
    assert "static/js/dashboard.js" in html


@pytest.mark.parametrize(
    "url",
    [
        "/api/dashboard/overview?range=nope",
        "/api/dashboard/analytics?range=24h&timezone=Not/AZone",
        "/api/dashboard/overview?range=custom&from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z",
    ],
)
def test_dashboard_rejects_invalid_bounded_query_params(client, url):
    _login(client)
    response = client.get(url)
    assert response.status_code == 400
    assert "error" in response.get_json()
