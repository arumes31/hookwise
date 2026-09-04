import re
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


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


@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis to avoid connection errors."""
    with patch("hookwise.tasks.redis_client") as mock_tasks_redis, patch("hookwise.api.redis_client") as mock_api_redis:
        mock_tasks_redis.get.return_value = None
        mock_api_redis.ping.return_value = True
        yield mock_tasks_redis, mock_api_redis


def test_clone_endpoint_not_found(client):
    """Test that cloning a non-existent endpoint returns 404."""
    # Simulate an authenticated session
    with client.session_transaction() as sess:
        sess["user_id"] = "test_user"
        sess["username"] = "testuser"
        sess["role"] = "admin"

    # POST to a non-existent ID
    response = client.post("/endpoint/clone/non-existent-id")

    # Assert that it returns 404
    assert response.status_code == 404


def _authenticate(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test_user"
        sess["username"] = "testuser"
        sess["role"] = "admin"


def _endpoint_form_data(name, **overrides):
    data = {"name": name, "bearer_auth_enabled": "true"}
    data.update(overrides)
    return data


def test_new_endpoint_form_shows_disabled_configuration_auto_link_warning(client):
    _authenticate(client)

    response = client.get("/endpoint/new")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    checkbox = re.search(r'<input[^>]*id="auto_link_configuration_enabled"[^>]*>', html)
    assert checkbox is not None
    assert 'name="auto_link_configuration_enabled"' in checkbox.group(0)
    assert "checked" not in checkbox.group(0)
    assert "assigned ConnectWise company" in html
    assert "affect the ticket's SLA or agreement" in html
    assert "configuration_ip" in html
    assert "configuration_mac" in html


def test_create_endpoint_persists_configuration_auto_link_opt_in_and_safe_default(client, app):
    _authenticate(client)

    enabled_response = client.post(
        "/endpoint/new",
        data=_endpoint_form_data("Enabled", auto_link_configuration_enabled="true"),
    )
    disabled_response = client.post("/endpoint/new", data=_endpoint_form_data("Disabled"))

    assert enabled_response.status_code == 302
    assert disabled_response.status_code == 302
    with app.app_context():
        assert WebhookConfig.query.filter_by(name="Enabled").one().auto_link_configuration_enabled is True
        assert WebhookConfig.query.filter_by(name="Disabled").one().auto_link_configuration_enabled is False


def test_edit_endpoint_enables_configuration_auto_link(client, app):
    with app.app_context():
        config = WebhookConfig(name="Editable")
        db.session.add(config)
        db.session.commit()
        config_id = config.id
    _authenticate(client)

    response = client.post(
        f"/endpoint/edit/{config_id}",
        data=_endpoint_form_data("Editable", auto_link_configuration_enabled="true"),
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(WebhookConfig, config_id).auto_link_configuration_enabled is True


def test_edit_endpoint_form_marks_enabled_configuration_auto_link_checked(client, app):
    with app.app_context():
        config = WebhookConfig(name="Enabled", auto_link_configuration_enabled=True)
        db.session.add(config)
        db.session.commit()
        config_id = config.id
    _authenticate(client)

    response = client.get(f"/endpoint/edit/{config_id}")

    assert response.status_code == 200
    checkbox = re.search(
        r'<input[^>]*id="auto_link_configuration_enabled"[^>]*>',
        response.get_data(as_text=True),
    )
    assert checkbox is not None
    assert "checked" in checkbox.group(0)


def test_clone_endpoint_disables_configuration_auto_link_setting(client, app):
    with app.app_context():
        config = WebhookConfig(name="Original", auto_link_configuration_enabled=True)
        db.session.add(config)
        db.session.commit()
        config_id = config.id
    _authenticate(client)

    response = client.post(f"/endpoint/clone/{config_id}")

    assert response.status_code == 302
    with app.app_context():
        clone = WebhookConfig.query.filter_by(name="Original (Copy)").one()
        assert clone.auto_link_configuration_enabled is False
