"""Tests for utility functions: encryption, jsonpath, masking, auth."""

import os
from unittest.mock import patch

import pytest
from flask import session

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import AuditLog
from hookwise.utils import (
    check_auth,
    decrypt_string,
    encrypt_string,
    log_audit,
    mask_secrets,
    resolve_jsonpath,
)


@pytest.fixture
def app():
    from cryptography.fernet import Fernet

    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


# --- Encryption ---


def test_encrypt_decrypt_roundtrip(app):
    """Encrypt then decrypt should return the original plaintext."""
    with app.app_context():
        plaintext = "s3cret-tok3n-value!"
        encrypted = encrypt_string(plaintext)
        assert encrypted != plaintext
        assert decrypt_string(encrypted) == plaintext


def test_decrypt_unencrypted_returns_input(app):
    """decrypt_string should return the original input if decryption fails."""
    with app.app_context():
        raw = "not-encrypted-at-all"
        assert decrypt_string(raw) == raw


def test_encrypt_decrypt_empty_string(app):
    """Empty string or None should be returned as is."""
    with app.app_context():
        assert encrypt_string("") == ""
        assert decrypt_string("") == ""
        assert encrypt_string(None) is None
        assert decrypt_string(None) is None


def test_encrypt_decrypt_unicode(app):
    """Unicode strings should be handled correctly."""
    with app.app_context():
        unicode_str = "S3crët-T0kën-Välüë-🚀"
        encrypted = encrypt_string(unicode_str)
        assert encrypted != unicode_str
        assert decrypt_string(encrypted) == unicode_str


def test_decrypt_invalid_token(app):
    """Invalid tokens (malformed Fernet) should return the original input."""
    with app.app_context():
        # Looks like Fernet but is invalid/corrupted
        invalid_token = "gAAAAABl-ThisIsInvalidTokenValue-xyz="
        assert decrypt_string(invalid_token) == invalid_token


def test_decrypt_with_different_key(app):
    """Decryption with a different key should return the original input."""
    from cryptography.fernet import Fernet

    with app.app_context():
        other_key = Fernet.generate_key().decode()
        other_f = Fernet(other_key.encode())
        plaintext = "secret-info"
        encrypted_with_other = other_f.encrypt(plaintext.encode()).decode()

        # Should fail to decrypt with app's key and return the cipher_text
        assert decrypt_string(encrypted_with_other) == encrypted_with_other


# --- JSONPath ---


def test_resolve_jsonpath_simple():
    data = {"status": "down", "code": 500}
    assert resolve_jsonpath(data, "$.status") == "down"
    assert resolve_jsonpath(data, "$.code") == 500


def test_resolve_jsonpath_nested():
    data = {"monitor": {"name": "DB Server", "tags": ["prod", "db"]}}
    assert resolve_jsonpath(data, "$.monitor.name") == "DB Server"
    assert resolve_jsonpath(data, "$.monitor.tags[0]") == "prod"


def test_resolve_jsonpath_array():
    data = {"alerts": [{"id": 1, "msg": "down"}, {"id": 2, "msg": "timeout"}]}
    assert resolve_jsonpath(data, "$.alerts[1].msg") == "timeout"


def test_resolve_jsonpath_missing():
    data = {"a": 1}
    assert resolve_jsonpath(data, "$.nonexistent") is None
    assert resolve_jsonpath(data, "$.a.b.c") is None


# --- Masking ---


def test_mask_secrets_dict():
    data = {
        "username": "admin",
        "password": "supersecret",
        "api_key": "key123",
        "token": "tok-abc",
        "safe_field": "visible",
    }
    masked = mask_secrets(data)
    assert masked["safe_field"] == "visible"
    assert masked["password"] == "***"
    assert masked["api_key"] == "***"
    assert masked["token"] == "***"


def test_mask_secrets_nested():
    data = {"outer": {"secret": "hidden", "name": "ok"}}
    masked = mask_secrets(data)
    assert masked["outer"]["secret"] == "***"
    assert masked["outer"]["name"] == "ok"


def test_mask_secrets_list():
    data = [{"password": "p"}, {"name": "n"}]
    masked = mask_secrets(data)
    assert masked[0]["password"] == "***"
    assert masked[1]["name"] == "n"


# --- Auth ---


@patch.dict(os.environ, {"GUI_USERNAME": "admin", "GUI_PASSWORD": "pass123"})
def test_check_auth_valid():
    assert check_auth("admin", "pass123") is True


@patch.dict(os.environ, {"GUI_USERNAME": "admin", "GUI_PASSWORD": "pass123"})
def test_check_auth_invalid():
    assert check_auth("admin", "wrong") is False
    assert check_auth("wrong", "pass123") is False


@patch.dict(os.environ, {}, clear=True)
def test_check_auth_disabled_when_no_env():
    """When GUI_USERNAME/GUI_PASSWORD are not set, auth is disabled."""
    # Remove the keys if they exist
    os.environ.pop("GUI_USERNAME", None)
    os.environ.pop("GUI_PASSWORD", None)
    assert check_auth("anything", "anything") is True


@patch.dict(os.environ, {"GUI_USERNAME": "admin"}, clear=True)
def test_check_auth_disabled_missing_password():
    """Auth should be disabled if GUI_PASSWORD is not set."""
    assert check_auth("any", "any") is True


@patch.dict(os.environ, {"GUI_PASSWORD": "pass"}, clear=True)
def test_check_auth_disabled_missing_username():
    """Auth should be disabled if GUI_USERNAME is not set."""
    assert check_auth("any", "any") is True


@patch.dict(os.environ, {"GUI_USERNAME": "", "GUI_PASSWORD": "pass"}, clear=True)
def test_check_auth_disabled_empty_username():
    """Auth should be disabled if GUI_USERNAME is empty."""
    assert check_auth("any", "any") is True


@patch.dict(os.environ, {"GUI_USERNAME": "admin", "GUI_PASSWORD": ""}, clear=True)
def test_check_auth_disabled_empty_password():
    """Auth should be disabled if GUI_PASSWORD is empty."""
    assert check_auth("any", "any") is True


# --- Audit Logging ---


def test_log_audit_no_context(app):
    """When no request context exists, user should be 'System'."""
    with app.app_context():
        log_audit(action="test_action", details="test details")

        entry = AuditLog.query.filter_by(action="test_action").first()
        assert entry is not None
        assert entry.user == "System"
        assert entry.details == "test details"


def test_log_audit_session_user(app):
    """When a session username exists, it should be used as the user."""
    with app.test_request_context():
        session["username"] = "session_user"
        log_audit(action="session_action", config_id="cfg1")

        entry = AuditLog.query.filter_by(action="session_action").first()
        assert entry is not None
        assert entry.user == "session_user"
        assert entry.config_id == "cfg1"


def test_log_audit_auth_user(app):
    """When Basic Auth is provided, the username should be used."""
    import base64

    auth_header = f"Basic {base64.b64encode(b'auth_user:pass').decode()}"
    with app.test_request_context(headers={"Authorization": auth_header}):
        log_audit(action="auth_action")

        entry = AuditLog.query.filter_by(action="auth_action").first()
        assert entry is not None
        assert entry.user == "auth_user"


def test_log_audit_custom_session_and_no_commit(app):
    """Verify custom DB session is used and commit=False works."""
    with app.app_context():
        log_audit(action="no_commit", commit=False, db_session=db.session)

        # Entry should be in session but not yet committed
        entry = AuditLog.query.filter_by(action="no_commit").first()
        assert entry is not None

        # Rollback should remove it
        db.session.rollback()
        entry = AuditLog.query.filter_by(action="no_commit").first()
        assert entry is None
