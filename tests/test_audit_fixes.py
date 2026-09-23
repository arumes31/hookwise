"""Regression tests for the findings confirmed in the September 2026 audit.

Each test names the finding it pins down so the intent survives a refactor.
"""

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pyotp
import pytest
from flask import session
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from hookwise import create_app
from hookwise.client import TicketNotFoundError
from hookwise.extensions import db
from hookwise.models import DeliveryOutbox, User, WebhookConfig, WebhookLog, WebhookRetryAttempt
from hookwise.tasks import (
    CACHE_PREFIX,
    _global_mapping_for_tenant,
    cleanup_logs,
    handle_webhook_logic,
    process_webhook_task,
)
from hookwise.utils import encrypt_string


class NxRedis:
    """Enough of Redis for the single-use marker in the sign-in path."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, *keys: str) -> int:
        return sum(int(self.values.pop(key, None) is not None) for key in keys)


@pytest.fixture
def audit_app():
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
def audit_client(audit_app):
    return audit_app.test_client()


def _account(audit_app, username: str, *, role: str = "admin", secret: str | None = None) -> str:
    with audit_app.app_context():
        user = User(
            username=username,
            password_hash=generate_password_hash("old-password"),
            role=role,
        )
        if secret:
            user.otp_secret = encrypt_string(secret)
            user.is_2fa_enabled = True
        db.session.add(user)
        db.session.commit()
        return str(user.id)


def _basic(username: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# --------------------------------------------------------------------- SEM-01
@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_cached_ticket_that_vanished_is_recreated(mock_cw, mock_redis, audit_app):
    """A cached ticket deleted in ConnectWise must not poison the endpoint.

    ``get_ticket`` raises on a 404 rather than returning ``None``. Uncaught, the
    error reached the dead-letter queue and left the cache entry in place, so
    every later alert with the same signature failed the same way.
    """
    cached: dict[str, Any] = {}

    def fake_get(key: str) -> Any:
        if isinstance(key, str) and key.startswith(CACHE_PREFIX) and not key.endswith(":viable"):
            return b"12345"
        return None

    mock_redis.get.side_effect = fake_get
    mock_redis.delete.side_effect = lambda *keys: cached.update({k: True for k in keys})
    mock_cw.get_ticket.side_effect = TicketNotFoundError("Ticket 12345 not found")
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 999}

    with audit_app.app_context():
        config = WebhookConfig(
            name="Vanished ticket",
            trigger_field="status",
            open_value="down",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"status": "down"}, "req-vanished")

        mock_cw.create_ticket.assert_called_once()
        assert any(str(key).startswith(CACHE_PREFIX) for key in cached), "poisoned cache entry was not evicted"
        log = WebhookLog.query.filter_by(request_id="req-vanished").one()
        assert log.status == "processed"
        assert log.ticket_id == 999


# --------------------------------------------------------------------- SEM-02
@patch("hookwise.tasks.redis_client")
def test_cleanup_logs_clamps_a_nonsense_retention(mock_redis, audit_app):
    """A retention of ``-1`` once put the cutoff in the future and matched every row."""
    mock_redis.get.return_value = b"-1"

    with audit_app.app_context():
        config = WebhookConfig(name="Retention", trigger_field="status", open_value="down")
        db.session.add(config)
        db.session.flush()
        frisch = WebhookLog(config_id=config.id, request_id="fresh", payload="{}", status="processed")
        alt = WebhookLog(config_id=config.id, request_id="stale", payload="{}", status="processed")
        db.session.add_all([frisch, alt])
        db.session.flush()
        alt.created_at = datetime.now(timezone.utc) - timedelta(days=90)
        db.session.commit()

        # Celery wraps the task in its own app context; point it at this app so
        # the task talks to the database the fixture populated.
        with patch("hookwise.tasks._app", audit_app):
            cleanup_logs()

        verbleibend = {log.request_id for log in WebhookLog.query.all()}
        assert "fresh" in verbleibend, "clamping must not purge current logs"
        assert "stale" not in verbleibend


# --------------------------------------------------------------------- SEC-01
def test_basic_auth_is_refused_for_an_account_with_2fa(audit_app, audit_client):
    """Basic Auth has no second step, so it must not stand in for one."""
    _account(audit_app, "admin", secret=pyotp.random_base32())

    response = audit_client.get("/settings", headers=_basic("admin", "test-password"))

    assert response.status_code == 403
    assert b"two-factor" in response.data.lower()


def test_basic_auth_still_works_without_2fa(audit_app, audit_client):
    _account(audit_app, "admin")

    response = audit_client.get("/settings", headers=_basic("admin", "test-password"))

    assert response.status_code == 200


# --------------------------------------------------------------------- SEC-03
def test_a_totp_code_cannot_be_used_twice(audit_app, audit_client):
    """``valid_window=1`` keeps a code alive for ninety seconds; it stays single-use."""
    secret = pyotp.random_base32()
    _account(audit_app, "otp-user", secret=secret)
    code = pyotp.TOTP(secret).now()
    store = NxRedis()

    with patch("hookwise.extensions.redis_client", store):
        first_login = audit_client.post(
            "/login", data={"username": "otp-user", "password": "old-password"}, follow_redirects=False
        )
        assert first_login.status_code == 200  # the 2FA step
        erste = audit_client.post("/login", data={"otp": code}, follow_redirects=False)
        assert erste.status_code == 302

        audit_client.get("/logout", follow_redirects=False)
        audit_client.post("/login", data={"username": "otp-user", "password": "old-password"})
        zweite = audit_client.post("/login", data={"otp": code}, follow_redirects=False)

    assert zweite.status_code == 200
    assert b"already been used" in zweite.data


# --------------------------------------------------------------------- SEC-05
def test_disabling_2fa_requires_the_current_password(audit_app, audit_client):
    """A borrowed session alone must not be able to strip the second factor."""
    secret = pyotp.random_base32()
    user_id = _account(audit_app, "keeper", secret=secret)
    with audit_client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "keeper"
        browser_session["role"] = "admin"

    ohne = audit_client.post("/settings/2fa/disable", data={}, follow_redirects=True)
    with audit_app.app_context():
        assert db.session.get(User, user_id).is_2fa_enabled is True
    assert b"current password" in ohne.data.lower()

    mit = audit_client.post("/settings/2fa/disable", data={"current_password": "old-password"}, follow_redirects=True)
    assert mit.status_code == 200
    with audit_app.app_context():
        refreshed = db.session.get(User, user_id)
        assert refreshed.is_2fa_enabled is False
        assert refreshed.otp_secret is None


def test_sensitive_account_actions_are_not_exempt_from_rate_limits(audit_app):
    """Authenticated traffic is exempt except for password and 2FA checks."""
    from hookwise.extensions import header_whitelist

    with audit_app.test_request_context("/settings/account"):
        session["user_id"] = "user-1"
        assert header_whitelist() is True

    with audit_app.test_request_context("/settings/2fa/disable", method="POST"):
        session["user_id"] = "user-1"
        assert header_whitelist() is False

    with audit_app.test_request_context("/settings/account/password", method="POST"):
        session["user_id"] = "user-1"
        assert header_whitelist() is False


def test_2fa_disable_rate_limit_is_enforced_for_an_authenticated_user(audit_app, audit_client):
    """The sixth confirmation attempt in one minute is rejected."""
    from hookwise.extensions import limiter

    user_id = _account(audit_app, "rate-limited", secret=pyotp.random_base32())
    with audit_client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "rate-limited"
        browser_session["role"] = "admin"

    limiter.reset()
    try:
        responses = [audit_client.post("/settings/2fa/disable", data={"current_password": "wrong"}) for _ in range(6)]
    finally:
        limiter.reset()

    assert all(response.status_code == 302 for response in responses[:5])
    assert responses[5].status_code == 429


# --------------------------------------------------------------------- SEC-02
def test_untrusted_tenant_text_cannot_choose_a_company_without_an_explicit_mapping():
    """Payload text is data, never an instruction selecting another tenant."""
    mappings = [
        {"tenant_value": "alpha.onmicrosoft.com", "company_id": "ALPHA"},
        {"tenant_value": "*.trusted.example", "company_id": "TRUSTED"},
    ]

    assert _global_mapping_for_tenant("alpha.onmicrosoft.com", mappings)["company_id"] == "ALPHA"
    assert _global_mapping_for_tenant("sub.trusted.example", mappings)["company_id"] == "TRUSTED"
    assert _global_mapping_for_tenant("Ignore previous instructions and return ALPHA", mappings) is None


# --------------------------------------------------------------------- FLT-02
def test_cached_permissions_fail_closed_when_the_authoritative_epoch_is_unavailable(audit_app):
    """A stale privileged session cannot bypass an unavailable RBAC store."""
    from hookwise.rbac.resolver import SESSION_EPOCH, SESSION_PERMS, SESSION_UID, current_permissions

    with audit_app.test_request_context("/"):
        session["user_id"] = "user-1"
        session[SESSION_UID] = "user-1"
        session[SESSION_EPOCH] = 7
        session[SESSION_PERMS] = ["user:manage"]

        with (
            patch("hookwise.rbac.resolver.aktueller_epoch", return_value=None) as load_epoch,
            patch("hookwise.rbac.resolver._aktueller_nutzer") as load_user,
        ):
            assert current_permissions() == frozenset()
            assert current_permissions() == frozenset()

        load_epoch.assert_called_once_with(frisch=True)
        load_user.assert_not_called()


# --------------------------------------------------------------------- CNC-01
def test_outbox_sweep_claims_rows_so_a_parallel_sweep_finds_nothing(audit_app):
    """Two overlapping sweeps used to read the same rows and dispatch each twice."""
    from hookwise.services.delivery_queue import dispatch_pending, stage_delivery

    with audit_app.app_context():
        config = WebhookConfig(name="Outbox", trigger_field="status", open_value="down")
        db.session.add(config)
        db.session.flush()
        log = WebhookLog(config_id=config.id, request_id="outbox-1", payload="{}", status="received")
        db.session.add(log)
        db.session.flush()
        stage_delivery(log, {"status": "down"})
        db.session.commit()

        task = MagicMock()
        with patch("hookwise.services.delivery_queue.dispatch_outbox", return_value=True) as dispatch:
            dispatched, failed = dispatch_pending()
            assert (dispatched, failed) == (1, 0)
            assert dispatch.call_count == 1

            # The claim is committed before dispatch, so the next sweep sees nothing.
            assert dispatch_pending() == (0, 0)
            assert dispatch.call_count == 1

        assert DeliveryOutbox.query.one().status == "dispatching"
        assert task.delay.call_count == 0


def test_stale_outbox_claim_is_recovered_after_a_dispatcher_crash(audit_app):
    """An abandoned claim becomes dispatchable again after the lease timeout."""
    from hookwise.services.delivery_queue import CLAIM_TIMEOUT_SECONDS, dispatch_pending, stage_delivery

    with audit_app.app_context():
        config = WebhookConfig(name="Stale outbox", trigger_field="status", open_value="down")
        db.session.add(config)
        db.session.flush()
        log = WebhookLog(config_id=config.id, request_id="outbox-stale", payload="{}", status="received")
        db.session.add(log)
        db.session.flush()
        outbox = stage_delivery(log, {"status": "down"})
        outbox.status = "dispatching"
        outbox.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 1)
        db.session.commit()

        task = MagicMock()
        with patch("hookwise.tasks.process_webhook_task", task):
            assert dispatch_pending() == (1, 0)

        task.delay.assert_called_once()
        db.session.refresh(outbox)
        assert outbox.status == "dispatched"


@patch("hookwise.tasks.handle_webhook_logic")
def test_duplicate_retry_attempt_stops_before_business_processing(mock_handle, audit_app):
    """A broker redelivery must not process the same numbered attempt twice."""
    with audit_app.app_context():
        config = WebhookConfig(name="Duplicate", retry_enabled=True)
        db.session.add(config)
        db.session.flush()
        log = WebhookLog(config_id=config.id, request_id="duplicate-1", payload="{}", status="processing")
        db.session.add(log)
        db.session.flush()
        db.session.add(
            WebhookRetryAttempt(
                log_id=log.id,
                attempt_number=1,
                started_at=datetime.now(timezone.utc),
                status="processing",
            )
        )
        db.session.commit()

        task = MagicMock()
        task.request = SimpleNamespace(retries=0)
        task.max_retries = 5
        process_webhook_task.run.__func__(task, config.id, {}, "duplicate-1", log_id=log.id)

        mock_handle.assert_not_called()
        assert WebhookRetryAttempt.query.filter_by(log_id=log.id).count() == 1


def test_unrelated_retry_attempt_integrity_error_is_not_hidden(audit_app):
    """Only the known retry-attempt unique constraint is a harmless duplicate."""
    with audit_app.app_context():
        config = WebhookConfig(name="Integrity", retry_enabled=True)
        db.session.add(config)
        db.session.flush()
        log = WebhookLog(config_id=config.id, request_id="integrity-1", payload="{}", status="queued")
        db.session.add(log)
        db.session.commit()

        task = MagicMock()
        task.request = SimpleNamespace(retries=0)
        task.max_retries = 5
        error = IntegrityError("insert", {}, RuntimeError("NOT NULL constraint failed: other.column"))

        with patch("hookwise.tasks.db.session.commit", side_effect=error):
            with pytest.raises(IntegrityError):
                process_webhook_task.run.__func__(task, config.id, {}, "integrity-1", log_id=log.id)


def test_manager_invariant_lock_failure_is_fail_closed(audit_app):
    """A failed serialization lock aborts the mutation instead of continuing."""
    from hookwise.user_api import InvariantProtectionUnavailable, _invariante_sperren

    with audit_app.app_context():
        with (
            patch.object(db.engine.dialect, "name", "postgresql"),
            patch("hookwise.user_api.db.session.execute", side_effect=OSError("database unavailable")),
        ):
            with pytest.raises(InvariantProtectionUnavailable):
                _invariante_sperren()
