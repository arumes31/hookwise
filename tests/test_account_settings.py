"""Account-security separation and revocable-session regression tests."""

from typing import Any
from unittest.mock import patch

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User


class MemoryRedis:
    """Small Redis subset used by the session registry tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    def pipeline(self) -> "MemoryRedis":
        return self

    def execute(self) -> list[Any]:
        return []

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.values[key] = value
        return True

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    def sadd(self, key: str, value: str) -> int:
        values = self.sets.setdefault(key, set())
        previous = len(values)
        values.add(value)
        return int(len(values) != previous)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, value: str) -> int:
        values = self.sets.setdefault(key, set())
        existed = value in values
        values.discard(value)
        return int(existed)

    def expire(self, _key: str, _ttl: int) -> bool:
        return True


@pytest.fixture
def account_app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def account_client(account_app):
    return account_app.test_client()


@pytest.fixture
def session_redis():
    client = MemoryRedis()
    with patch("hookwise.user_sessions._redis", return_value=client):
        yield client


def _user(account_app, username: str, source: str = "local") -> str:
    with account_app.app_context():
        user = User(
            username=username,
            password_hash=generate_password_hash("old-password"),
            auth_source=source,
            upn=f"{username}@example.test" if source == "entra" else None,
        )
        db.session.add(user)
        db.session.commit()
        return str(user.id)


def _login(account_client, username: str) -> None:
    response = account_client.post(
        "/login",
        data={"username": username, "password": "old-password"},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_local_account_settings_offer_password_2fa_and_sessions(account_app, account_client, session_redis):
    _user(account_app, "local-user")
    _login(account_client, "local-user")

    response = account_client.get("/settings/account")

    assert response.status_code == 200
    assert b"Change password" in response.data
    assert b"Set up 2FA" in response.data
    assert b"This browser" in response.data
    assert b'href="/logout" hx-boost="false"' in response.data
    assert b"Managed by Microsoft 365" not in response.data


def test_entra_account_cannot_use_local_password_or_2fa(account_app, account_client, session_redis):
    user_id = _user(account_app, "entra-user", source="entra")
    with account_client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "entra-user"
        browser_session["role"] = "viewer"
        browser_session["auth_source"] = "entra"

    page = account_client.get("/settings/account")
    blocked_password = account_client.post(
        "/settings/account/password",
        data={
            "current_password": "old-password",
            "new_password": "new-password",
            "confirm_password": "new-password",
        },
        follow_redirects=True,
    )
    blocked_2fa = account_client.get("/settings/2fa/setup", follow_redirects=True)

    assert b"Managed by Microsoft 365" in page.data
    assert b'name="current_password"' not in page.data
    assert b"managed in Microsoft 365" in blocked_password.data
    assert b"managed in Microsoft 365" in blocked_2fa.data
    with account_app.app_context():
        refreshed = db.session.get(User, user_id)
        assert refreshed is not None
        assert check_password_hash(refreshed.password_hash, "old-password")
        assert refreshed.is_2fa_enabled is False


def test_password_change_revokes_other_registered_sessions(account_app, account_client, session_redis):
    user_id = _user(account_app, "password-user")
    _login(account_client, "password-user")
    with account_client.session_transaction() as browser_session:
        current_id = browser_session["session_id"]

    other_id = "other-browser-session"
    other_record = (
        '{"id":"other-browser-session","user_id":"' + user_id + '","username":"password-user","auth_source":"local",'
        '"created_at":"2026-09-18T08:00:00+00:00","last_seen_at":"2026-09-18T08:00:00+00:00",'
        '"ip_address":"192.0.2.10","user_agent":"Test browser"}'
    )
    session_redis.setex(f"hookwise:user-session:{other_id}", 100, other_record)
    session_redis.sadd(f"hookwise:user-sessions:{user_id}", other_id)

    response = account_client.post(
        "/settings/account/password",
        data={
            "current_password": "old-password",
            "new_password": "new-password",
            "confirm_password": "new-password",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"1 other session signed out" in response.data
    assert session_redis.get(f"hookwise:user-session:{other_id}") is None
    assert session_redis.get(f"hookwise:revoked-session:{other_id}") == "1"
    assert session_redis.get(f"hookwise:user-session:{current_id}") is not None


def test_revoked_current_session_is_rejected(account_app, account_client, session_redis):
    _user(account_app, "revoked-user")
    _login(account_client, "revoked-user")
    with account_client.session_transaction() as browser_session:
        session_id = browser_session["session_id"]

    session_redis.setex(f"hookwise:revoked-session:{session_id}", 100, "1")
    session_redis.delete(f"hookwise:user-session:{session_id}")

    response = account_client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    with account_client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_logout_uses_full_navigation_and_login_feed_initializes_immediately(account_client):
    page = account_client.get("/login").get_data(as_text=True)

    assert "document.querySelector('.hw-login-split')" in page
    assert "DOMContentLoaded', function" not in page
