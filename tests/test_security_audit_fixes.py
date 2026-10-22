"""Regression tests for the October 2026 security audit of HookWise."""

import base64
import hashlib
import json
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from hookwise import create_app
from hookwise.extensions import LIVE_PAYLOAD_ROOM, LIVE_ROOM, db, socketio
from hookwise.models import AuditLog, User, WebhookConfig, WebhookLog
from hookwise.utils import log_to_web, reject_published_encryption_key

from .test_account_settings import MemoryRedis

PUBLISHED_KEY = "OV0PcQMSRDhDsggpvMAukyXoc8Q2vZXo74KjSSLRnsk="
CI_KEY = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_redis():
    store = MemoryRedis()
    with patch("hookwise.user_sessions._redis", return_value=store):
        yield store


def _user(app, username: str = "admin", password: str = "old-password", role: str = "admin") -> str:
    with app.app_context():
        user = User(username=username, password_hash=generate_password_hash(password), role=role)
        db.session.add(user)
        db.session.commit()
        return str(user.id)


def _login(client, username: str = "admin", password: str = "old-password") -> None:
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 302


def _basic(username: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# ------------------------------------------------- Socket.IO live feed (HIGH)
def test_anonymous_socket_is_refused(app, client):
    socket = socketio.test_client(app, flask_test_client=client)
    assert not socket.is_connected()


def test_signed_in_socket_receives_masked_payloads(app, client, session_redis):
    _user(app)
    _login(client)

    socket = socketio.test_client(app, flask_test_client=client)
    assert socket.is_connected()
    socket.get_received()
    log_to_web("queued", data={"host": "srv-01", "token": "s3cret"})

    events = [event for event in socket.get_received() if event["name"] == "new_log"]
    assert len(events) == 1
    assert events[0]["args"][0]["payload"] == {"host": "srv-01", "token": "***"}
    socket.disconnect()


def test_socket_outside_trusted_ips_is_refused(app, client, session_redis, monkeypatch):
    _user(app)
    _login(client)
    monkeypatch.setenv("GUI_TRUSTED_IPS", "10.0.0.0/8")

    socket = socketio.test_client(app, flask_test_client=client)
    assert not socket.is_connected()


def test_live_events_go_to_rooms_and_metadata_room_has_no_payload():
    with patch("hookwise.utils.socketio.emit") as emit:
        log_to_web("queued", data={"host": "srv-01"})

    rooms = {call.kwargs["to"]: call.args[1] for call in emit.call_args_list}
    assert set(rooms) == {LIVE_ROOM, LIVE_PAYLOAD_ROOM}
    assert rooms[LIVE_PAYLOAD_ROOM]["payload"] == {"host": "srv-01"}
    assert rooms[LIVE_ROOM]["payload"] is None


# ---------------------------------------------- Basic-Auth fallback (MEDIUM)
def test_basic_auth_is_off_by_default(app, client, monkeypatch):
    monkeypatch.delenv("GUI_BASIC_AUTH", raising=False)
    _user(app)

    response = client.get("/settings", headers=_basic("admin", "old-password"))

    assert response.status_code != 200


def test_basic_auth_ignores_the_static_gui_password(app, client, monkeypatch):
    """After a password change in the console the bootstrap password must stop working."""
    monkeypatch.setenv("GUI_BASIC_AUTH", "true")
    monkeypatch.setenv("GUI_PASSWORD", "bootstrap-password")
    _user(app, password="rotated-password")

    assert client.get("/settings", headers=_basic("admin", "bootstrap-password")).status_code == 401
    assert client.get("/settings", headers=_basic("admin", "rotated-password")).status_code == 200


def test_failed_basic_auth_is_rate_limited_and_audited(app, client, monkeypatch):
    monkeypatch.setenv("GUI_BASIC_AUTH", "true")
    _user(app)

    statuses = [client.get("/settings", headers=_basic("admin", f"wrong-{i}")).status_code for i in range(6)]

    assert statuses == [401] * 5 + [429]
    # Even the right password is refused while the address is blocked.
    assert client.get("/settings", headers=_basic("admin", "old-password")).status_code == 429
    with app.app_context():
        assert AuditLog.query.filter_by(action="basic_auth_failed").count() == 5


# ------------------------------------------------ Webhook rejections (LOW)
def _endpoint(app, **fields: Any) -> str:
    with app.app_context():
        config = WebhookConfig(name="Endpoint", bearer_token="right-token", **fields)
        db.session.add(config)
        db.session.commit()
        return str(config.id)


def test_rejected_body_is_stored_only_as_digest(app, client):
    config_id = _endpoint(app)
    body = b"not json: password=hunter2"

    response = client.post(
        f"/w/{config_id}", data=body, headers={"Authorization": "Bearer wrong", "Content-Type": "text/plain"}
    )

    assert response.status_code == 401
    with app.app_context():
        stored = json.loads(WebhookLog.query.filter_by(config_id=config_id).one().payload)
    assert stored == {"rejected_body_sha256": hashlib.sha256(body).hexdigest(), "rejected_body_bytes": len(body)}


def test_rejections_do_not_spend_the_endpoint_budget(app, client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_REJECTION_LIMIT", "1000 per minute")
    config_id = _endpoint(app, rate_limit_per_minute=2)

    for _ in range(5):
        client.post(f"/w/{config_id}", json={"a": 1}, headers={"Authorization": "Bearer wrong"})

    with patch("hookwise.webhook.commit_and_dispatch", return_value=True):
        response = client.post(f"/w/{config_id}", json={"a": 1}, headers={"Authorization": "Bearer right-token"})
    assert response.status_code == 202


def test_rejections_are_limited_per_source_address(app, client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_REJECTION_LIMIT", "3 per minute")
    config_id = _endpoint(app)

    statuses = [
        client.post(f"/w/{config_id}", json={"a": 1}, headers={"Authorization": "Bearer wrong"}).status_code
        for _ in range(4)
    ]

    assert statuses == [401, 401, 401, 429]


def test_rejection_logging_is_sampled(app, client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_REJECTION_LIMIT", "1000 per minute")
    config_id = _endpoint(app)

    for _ in range(15):
        client.post(f"/w/{config_id}", json={"a": 1}, headers={"Authorization": "Bearer wrong"})

    with app.app_context():
        assert WebhookLog.query.filter_by(config_id=config_id).count() == 10


# ------------------------------------------------------ Logout (LOW)
def test_logout_is_post_only(app, client, session_redis):
    _user(app)
    _login(client)

    assert client.get("/logout").status_code == 405
    with client.session_transaction() as browser_session:
        assert "user_id" in browser_session

    response = client.post("/logout")
    assert response.status_code == 302
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_login_hashes_even_for_unknown_users(app, client):
    with patch("hookwise.auth.check_password_hash", return_value=False) as check:
        client.post("/login", data={"username": "nobody", "password": "whatever"})
    check.assert_called_once()


# ---------------------------------------------- Master API key (LOW)
def test_master_api_key_is_stored_as_hash_and_not_redisplayed(app, client, session_redis):
    _user(app)
    _login(client)
    stored: dict[str, Any] = {}

    with patch("hookwise.api.redis_client") as redis:
        redis.set.side_effect = lambda key, value: stored.__setitem__(key, value)
        redis.get.side_effect = lambda key: stored.get(key)
        redis.delete.side_effect = lambda key: stored.pop(key, None)
        key = client.post("/admin/generate-api-key").get_json()["api_key"]
        page = client.get("/settings").get_data(as_text=True)

    digest = stored["hookwise_master_api_key_sha256"]
    assert len(digest) == 64 and digest != key
    assert "hookwise_master_api_key" not in stored
    assert key not in page
    assert "shown only once" in page


# ---------------------------------------- Published encryption keys (LOW)
def test_published_compose_key_is_always_refused(monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    with pytest.raises(RuntimeError, match="publicly known"):
        reject_published_encryption_key(PUBLISHED_KEY)


def test_ci_key_is_refused_outside_tests(monkeypatch):
    monkeypatch.setenv("TESTING", "false")
    with pytest.raises(RuntimeError, match="publicly known"):
        reject_published_encryption_key(CI_KEY)
    monkeypatch.setenv("TESTING", "true")
    reject_published_encryption_key(CI_KEY)
    reject_published_encryption_key(Fernet.generate_key().decode())


def test_create_app_refuses_a_published_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", PUBLISHED_KEY)
    with pytest.raises(RuntimeError, match="publicly known"):
        create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


def test_rotate_encryption_key_reencrypts_stored_secrets(app, monkeypatch):
    import hookwise.utils as utils

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr(utils, "_fernet_instance", Fernet(old_key.encode()))
    with app.app_context():
        config = WebhookConfig(name="Rotate", bearer_token="bearer-plain", hmac_secret="hmac-plain")
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    monkeypatch.setattr(utils, "_fernet_instance", None)
    monkeypatch.setenv("ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("OLD_ENCRYPTION_KEY", old_key)
    result = CliRunner().invoke(app.cli, ["rotate-encryption-key"])

    assert result.exit_code == 0, result.output
    with app.app_context():
        db.session.expire_all()
        rotated = db.session.get(WebhookConfig, config_id)
        assert rotated is not None
        assert utils.decrypt_string(rotated.bearer_token) == "bearer-plain"
        assert utils.decrypt_string(rotated.hmac_secret) == "hmac-plain"
