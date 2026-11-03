"""Regression tests for the November 2026 security audit findings."""

import base64
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from hookwise import _init_db_data, create_app
from hookwise.extensions import db
from hookwise.models import User, WebhookConfig, WebhookLog
from hookwise.utils import encrypt_string


class MemoryRedis:
    """Small Redis subset for the session registry."""

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
        self.sets.setdefault(key, set()).add(value)
        return 1

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, value: str) -> int:
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def expire(self, _key: str, _ttl: int) -> bool:
        return True


@pytest.fixture
def audit_app():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(audit_app):
    return audit_app.test_client()


@pytest.fixture
def session_redis():
    fake = MemoryRedis()
    with patch("hookwise.extensions.redis_client", fake):
        yield fake


def _account(app, username: str, role: str = "viewer", password: str = "pw-123456") -> str:
    with app.app_context():
        user = User(username=username, password_hash=generate_password_hash(password), role=role)
        db.session.add(user)
        db.session.commit()
        return str(user.id)


def _basic(username: str, password: str = "pw-123456") -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# ------------------------------------------------- Basic Auth authorization
def test_basic_auth_enforces_route_permission_before_the_handler(audit_app, client, monkeypatch):
    """A low-privilege Basic caller must not reach guard-only routes such as user creation."""
    _account(audit_app, "viewer-basic", role="viewer")
    monkeypatch.setenv("GUI_BASIC_AUTH", "true")

    response = client.post(
        "/api/users",
        json={"username": "evil-admin", "password": "evil-password", "roles": ["admin"]},
        headers=_basic("viewer-basic"),
    )

    assert response.status_code == 403
    with audit_app.app_context():
        assert User.query.filter_by(username="evil-admin").first() is None


def test_basic_auth_never_issues_a_session_cookie(audit_app, client, monkeypatch):
    _account(audit_app, "admin-basic", role="admin")
    monkeypatch.setenv("GUI_BASIC_AUTH", "true")

    response = client.get("/api/activity/history", headers=_basic("admin-basic"))

    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers
    follow_up = client.get("/api/activity/history")
    assert follow_up.status_code == 401


# ------------------------------------------------------- session lifecycle
def test_legacy_cookie_is_rejected_after_account_wide_revocation(audit_app, client, session_redis):
    user_id = _account(audit_app, "legacy-user", role="admin")
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "legacy-user"
    assert client.get("/api/activity/history").status_code == 200

    with client.session_transaction() as browser_session:
        browser_session.pop("session_id", None)
    session_redis.values[f"hookwise:user-sessions-revoked-at:{user_id}"] = repr(time.time())

    assert client.get("/api/activity/history").status_code == 401


def test_disabled_account_loses_its_registered_session(audit_app, client, session_redis):
    user_id = _account(audit_app, "soon-disabled", role="admin")
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["session_id"] = "registered-session"
        browser_session["session_started_at"] = time.time()
    session_redis.values["hookwise:user-session:registered-session"] = json.dumps(
        {"id": "registered-session", "user_id": user_id, "last_seen_at": "2999-01-01T00:00:00+00:00"}
    )
    with audit_app.app_context():
        account = db.session.get(User, user_id)
        assert account is not None
        account.is_active = False
        db.session.commit()

    # /metrics has no route permission; only session validation protects it.
    response = client.get("/metrics")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_missing_registry_record_is_not_recreated_after_revocation(audit_app, client, session_redis):
    user_id = _account(audit_app, "revoked-later", role="admin")
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["session_id"] = "gone-session"
        browser_session["session_started_at"] = time.time() - 100
    session_redis.values[f"hookwise:user-sessions-revoked-at:{user_id}"] = repr(time.time())

    assert client.get("/api/activity/history").status_code == 401


def test_revocation_markers_outlive_the_cookie_lifetime(audit_app):
    from hookwise.user_sessions import _retention_seconds

    with audit_app.test_request_context():
        assert _retention_seconds() > audit_app.permanent_session_lifetime.total_seconds()


# ------------------------------------------------------- token suffix oracle
def test_token_suffix_search_without_reveal_uses_only_the_hint():
    from hookwise.endpoint_summary import _token_suffix_matches

    config: Any = SimpleNamespace(bearer_token_last4="WXYZ", bearer_token=encrypt_string("secret-token-WXYZ"))
    with patch("hookwise.endpoint_summary.decrypt_string") as decrypt:
        assert _token_suffix_matches(config, "wrongWXYZ", may_compare_secret=False)
        decrypt.assert_not_called()
    assert not _token_suffix_matches(config, "wrongWXYZ", may_compare_secret=True)
    assert _token_suffix_matches(config, "token-WXYZ", may_compare_secret=True)


# --------------------------------------------------------- template engine
def test_ticket_description_does_not_reinterpret_webhook_values():
    from hookwise.tasks import _render_ticket_description

    with patch("hookwise.tasks.resolve_jsonpath", return_value="resolved") as resolve:
        text = _render_ticket_description(
            mapped_description=None,
            description_template="{{ monitor_name }} | {{ msg }} | {$.status} | {{ request_id }}",
            monitor_name="mon {$.secret}",
            message="{{ request_id }}",
            request_id="req-1",
            data={"status": "down"},
        )
    assert text == "mon {$.secret} | {{ request_id }} | resolved | req-1"
    resolve.assert_called_once()


# ------------------------------------------------------------- mappings
def test_mapping_literals_keep_their_neighbourhood_semantics():
    from hookwise.services.routing import apply_mapping

    data = {"a": "x", "b": ""}
    mapped = apply_mapping(data, json.dumps({"summary": "pre $.a mid $.b post", "board": "only $.b text"}))
    assert mapped["summary"] == "pre x mid post"
    assert "board" not in mapped


def test_configuration_label_parser_matches_previous_grammar():
    from hookwise.services.configuration_matching import _parse_text_label

    assert _parse_text_label("  - Serial Number: ABC123  ") == ("serial_numbers", "ABC123")
    assert _parse_text_label("mac address = 00:11:22:33:44:55") == ("mac_addresses", "00:11:22:33:44:55")
    assert _parse_text_label("hostname:" + " " * 5000 + "x") is None
    assert _parse_text_label("unrelated: value") is None


# ------------------------------------------------------------ sanitizer
def test_forwarding_sanitizer_masks_in_one_pass_and_bounds_depth():
    from hookwise.services.customer_webhooks import _sanitized_alert

    result = _sanitized_alert({"token": "t", "Headers": {"a": 1}, "nested": [{"password": "p", "ok": 1}]})
    assert result == {"token": "***", "Headers": "***", "nested": [{"password": "***", "ok": 1}]}

    deep: Any = "leaf"
    for _ in range(200):
        deep = {"n": deep}
    flattened = _sanitized_alert(deep)
    depth = 0
    while isinstance(flattened, dict):
        flattened = flattened["n"]
        depth += 1
    assert flattened == "***" and depth < 100


# ---------------------------------------------------------------- backups
def test_backup_token_cannot_be_restored_as_a_credential(audit_app):
    from hookwise.services.backups import BACKUP_PURPOSE_PREFIX, _restored_credential, export_backup, parse_backup
    from hookwise.utils import get_fernet

    token = export_backup()
    assert get_fernet().decrypt(token).startswith(BACKUP_PURPOSE_PREFIX)
    assert parse_backup(token)["format"] == "hookwise-config"
    with pytest.raises(ValueError):
        _restored_credential(token.decode())
    legacy = get_fernet().encrypt(b'{"format":"hookwise-config","configs":[]}').decode()
    with pytest.raises(ValueError):
        _restored_credential(legacy)
    assert _restored_credential(encrypt_string("plain-token")) == "plain-token"


def test_restore_rejects_identifiers_outside_the_generated_alphabet(audit_app):
    from hookwise.services.backups import BackupValidationError, restore_backup

    with pytest.raises(BackupValidationError):
        restore_backup({"configs": [{"id": "x');alert(1);//", "name": "evil", "allow_unauthenticated": True}]})


def test_endpoint_templates_pass_ids_as_data_attributes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "templates"
    for name in ("webhooks.html", "form.html", "history_rows.html", "webhook_detail.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert "('{{ config.id }}'" not in text
        assert "('{{ log.id }}'" not in text
        assert "('{{ log.request_id }}'" not in text


# ------------------------------------------------------- lifecycle checks
@patch("hookwise.tasks.cw_client")
def test_worker_skips_queued_work_for_a_paused_endpoint(mock_cw, audit_app):
    from hookwise.tasks import handle_webhook_logic

    with audit_app.app_context():
        config = WebhookConfig(name="Paused", trigger_field="status", open_value="down", is_enabled=False)
        db.session.add(config)
        db.session.commit()
        handle_webhook_logic(config.id, {"status": "down"}, "req-paused")
        log = WebhookLog.query.filter_by(request_id="req-paused").one()
        assert log.status == "skipped"
    mock_cw.create_ticket.assert_not_called()
    mock_cw.close_ticket.assert_not_called()


def test_replay_is_refused_for_an_archived_endpoint(audit_app):
    from datetime import datetime, timezone

    from hookwise.history_ops import EndpointInactiveError, _queue_replay

    with audit_app.app_context():
        config = WebhookConfig(name="Archived", archived_at=datetime.now(timezone.utc))
        db.session.add(config)
        db.session.commit()
        log = WebhookLog(config_id=config.id, request_id="orig", payload="{}", status="failed")
        db.session.add(log)
        db.session.commit()
        with pytest.raises(EndpointInactiveError):
            _queue_replay(log, {}, "retry")


# ------------------------------------------------------------- bootstrap
def test_routine_admin_bootstrap_does_not_overwrite_a_changed_password(audit_app, monkeypatch):
    monkeypatch.setenv("GUI_PASSWORD", "bootstrap-password")
    with audit_app.app_context():
        db.session.add(User(username="admin", password_hash=generate_password_hash("changed"), role="admin"))
        db.session.commit()

    _init_db_data(audit_app)
    with audit_app.app_context():
        assert check_password_hash(User.query.filter_by(username="admin").one().password_hash, "changed")

    _init_db_data(audit_app, reset_password=True)
    with audit_app.app_context():
        admin = User.query.filter_by(username="admin").one()
        assert check_password_hash(admin.password_hash, "bootstrap-password")


# ------------------------------------------------------------ misc hardening
def test_failure_alert_does_not_read_the_receiver_body():
    import hookwise.tasks as tasks

    source = open(tasks.__file__, encoding="utf-8").read()
    assert "stream=True" in source and "allow_redirects=False" in source


def test_force_https_ignores_a_raw_forwarded_proto_header(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("HTTPS_ORIGIN", "https://hookwise.example.com")
    app = create_app()
    app.config["TESTING"] = True

    response = app.test_client().get(
        "/login", base_url="http://hookwise.example.com", headers={"X-Forwarded-Proto": "https"}
    )

    assert response.status_code == 301


def test_rbac_log_mode_escapes_control_characters():
    from hookwise.rbac.decorators import _log_feld

    assert "\n" not in _log_feld("/customer-webhooks/a\nFAKE LOG LINE/edit")


def test_login_is_throttled_for_signed_in_browsers(audit_app):
    from flask import session

    from hookwise.extensions import header_whitelist

    with audit_app.test_request_context("/login", method="POST"):
        session["user_id"] = "someone"
        assert header_whitelist() is False
