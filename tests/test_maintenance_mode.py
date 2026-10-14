"""Maintenance mode: readable for viewers, switchable only with settings:write."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import AuditLog
from hookwise.rbac.catalog import ALL_PERMISSIONS, LEGACY_ROLE_MAP

VIEWER = LEGACY_ROLE_MAP["viewer"]


@pytest.fixture
def app():
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        RBAC_ENFORCE="on",
    )
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def store():
    """One Redis stand-in shared by the request gate and the admin handlers."""
    values: dict[str, bytes] = {}
    fake = MagicMock()
    fake.get.side_effect = values.get
    fake.set.side_effect = lambda key, value, **_: values.__setitem__(
        key, value.encode() if isinstance(value, str) else value
    )
    fake.ping.return_value = True
    with patch("hookwise.tasks.redis_client", fake), patch("hookwise.api.redis_client", fake):
        yield values


def _sign_in(client, permissions):
    with client.session_transaction() as sess:
        sess["user_id"] = "user-1"
        sess["username"] = "viewer@example.com"
    return (
        patch("hookwise.rbac.decorators.current_permissions", return_value=permissions),
        patch("hookwise.rbac.resolver.current_permissions", return_value=permissions),
    )


def _denials():
    return [a.details for a in AuditLog.query.filter_by(action="perm_denied")]


def test_dashboard_no_longer_probes_maintenance_state(client, store):
    script = Path("static/js/dashboard-page.js").read_text(encoding="utf-8")
    assert "/admin/maintenance" not in script

    guard, template_guard = _sign_in(client, VIEWER)
    with guard, template_guard:
        response = client.get("/")

    assert response.status_code == 200
    assert b"maintenance-toggle" not in response.data
    assert _denials() == []


def test_viewer_reads_state_but_cannot_change_it(client, store):
    guard, template_guard = _sign_in(client, VIEWER)
    with guard, template_guard:
        page = client.get("/admin/maintenance")
        state = client.get("/admin/maintenance", headers={"Accept": "application/json"})
        change = client.post("/admin/maintenance", data={"enabled": "true"})

    assert page.status_code == 200
    assert b"Read-only" in page.data
    assert b'id="maintenance-form"' not in page.data
    assert state.json == {"maintenance_mode": False}
    assert change.status_code == 403
    assert "hookwise_maintenance_mode" not in store
    assert _denials() == ["viewer@example.com lacks settings:write for /admin/maintenance"]


def test_admin_sets_explicit_state_from_the_page(client, store):
    guard, template_guard = _sign_in(client, ALL_PERMISSIONS)
    with guard, template_guard:
        first = client.post("/admin/maintenance", data={"enabled": "true"})
        repeated = client.post("/admin/maintenance", data={"enabled": "true"})
        page = client.get("/admin/maintenance")

    assert first.status_code == repeated.status_code == 302
    assert first.headers["Location"].endswith("/admin/maintenance")
    # A double submit keeps the requested state instead of flipping it back.
    assert store["hookwise_maintenance_mode"] == b"true"
    assert b"Disable maintenance mode" in page.data
    assert AuditLog.query.filter_by(action="maintenance_toggle").count() == 2


def test_json_post_without_state_still_toggles(client, store):
    guard, template_guard = _sign_in(client, ALL_PERMISSIONS)
    with guard, template_guard:
        on = client.post("/admin/maintenance", json={})
        off = client.post("/admin/maintenance", json={})

    assert on.json == {"status": "success", "maintenance_mode": True}
    assert off.json == {"status": "success", "maintenance_mode": False}


def test_maintenance_keeps_sign_in_and_switch_reachable(app, client, store):
    store["hookwise_maintenance_mode"] = b"true"

    anonymous = app.test_client()
    assert anonymous.get("/login").status_code == 200
    blocked = anonymous.get("/")
    assert blocked.status_code == 503
    assert b"Administrator sign-in" in blocked.data
    assert anonymous.post("/w/some-endpoint", json={}).status_code == 503

    guard, template_guard = _sign_in(client, ALL_PERMISSIONS)
    with guard, template_guard:
        notice = client.get("/settings")
        admin_page = client.get("/admin/maintenance")
        switched_off = client.post("/admin/maintenance", data={"enabled": "false"})

    assert notice.status_code == 503
    assert b"Disable maintenance mode" in notice.data
    assert admin_page.status_code == 200
    assert switched_off.status_code == 302
    assert store["hookwise_maintenance_mode"] == b"false"


def test_route_permissions_are_split():
    from hookwise.rbac.routes import ENDPUNKT_RECHTE

    assert ENDPUNKT_RECHTE["main.admin_maintenance"] == "settings:read"
    assert ENDPUNKT_RECHTE["main.maintenance_mode"] == "settings:write"
