import io
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.api import _routing_regex_matches
from hookwise.extensions import db


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


def login(client):
    with client.session_transaction() as session:
        session["user_id"] = "admin-id"
        session["username"] = "admin"
        session["role"] = "admin"


def test_force_https_uses_only_the_configured_origin(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("HTTPS_ORIGIN", "https://hookwise.example.com")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(
        "/history?next=//attacker.example",
        base_url="http://hookwise.example.com",
    )

    assert response.status_code == 301
    assert response.headers["Location"] == "https://hookwise.example.com/history?next=//attacker.example"


def test_force_https_rejects_an_untrusted_host(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("HTTPS_ORIGIN", "https://hookwise.example.com")
    app = create_app()
    app.config["TESTING"] = True

    response = app.test_client().get("/", base_url="http://attacker.example")

    assert response.status_code == 400
    assert "Location" not in response.headers


def test_routing_regex_timeout_fails_closed(app):
    with (
        app.app_context(),
        patch("hookwise.api.safe_regex.search", side_effect=TimeoutError),
    ):
        assert not _routing_regex_matches("(a+)+$", "a" * 1_000)


@patch("hookwise.api.redis_client")
def test_readyz_does_not_disclose_redis_exception(mock_redis, client):
    mock_redis.ping.side_effect = RuntimeError("redis-password=secret")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json == {"status": "not ready", "reason": "Redis error"}
    assert b"redis-password" not in response.data


@patch("hookwise.tasks.check_webhook_timeouts.delay")
def test_timeout_trigger_does_not_disclose_exception(mock_delay, client):
    login(client)
    mock_delay.side_effect = RuntimeError("broker-password=secret")

    response = client.post("/api/activity/trigger-timeout-check")

    assert response.status_code == 503
    assert response.json == {"status": "error", "message": "Failed to enqueue timeout check"}
    assert b"broker-password" not in response.data


@patch("hookwise.api.redis_client")
def test_clear_cache_does_not_disclose_exception(mock_redis, client):
    login(client)
    mock_redis.scan_iter.side_effect = RuntimeError("redis-password=secret")

    response = client.post("/admin/clear-cache")

    assert response.status_code == 500
    assert response.json == {"status": "error", "message": "Failed to clear cache"}
    assert b"redis-password" not in response.data


def test_restore_does_not_disclose_parser_exception(client):
    login(client)

    response = client.post(
        "/admin/restore",
        data={"backup_file": (io.BytesIO(b"{not-json"), "backup.json")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 500
    assert response.json == {"status": "error", "message": "Configuration import failed"}
    assert b"Expecting property name" not in response.data


def test_debugger_does_not_disclose_parser_exceptions(client):
    login(client)

    response = client.post(
        "/api/debug/process",
        json={
            "payload": {"message": "test"},
            "config": {"json_mapping": "{", "routing_rules": "{"},
        },
    )

    assert response.status_code == 200
    assert "Error parsing JSON Mapping" in response.json["steps"]
    assert "Error parsing Routing Rules" in response.json["steps"]
    assert b"Expecting property name" not in response.data


def test_untrusted_ui_values_are_not_interpolated_as_html():
    root = Path(__file__).parents[1]
    ux = (root / "static/js/ux.js").read_text(encoding="utf-8")
    form = (root / "templates/form.html").read_text(encoding="utf-8")

    assert "messageNode.textContent = String(message)" in ux
    assert "<div>${message}</div>" not in ux
    assert 'value="${path}"' not in form
    assert 'value="${regex}"' not in form
    assert "resultPre.textContent = JSON.stringify" in form
    assert "error.textContent = String(data.message" in form


def test_all_workflow_actions_use_immutable_shas():
    workflows = Path(__file__).parents[1] / ".github/workflows"
    action_ref = re.compile(r"uses:\s+[^@\s]+@([^\s#]+)")

    refs = [
        match.group(1)
        for workflow in workflows.glob("*.yml")
        for match in action_ref.finditer(workflow.read_text(encoding="utf-8"))
    ]

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
