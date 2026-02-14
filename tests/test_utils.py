"""Tests for utility functions: encryption, jsonpath, masking, auth."""
import os
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.utils import (
    check_auth,
    decrypt_string,
    encrypt_string,
    mask_secrets,
    resolve_jsonpath,
)


@pytest.fixture
def app():
    from cryptography.fernet import Fernet
    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ.setdefault('ENCRYPTION_KEY', Fernet.generate_key().decode())
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
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
    os.environ.pop('GUI_USERNAME', None)
    os.environ.pop('GUI_PASSWORD', None)
    assert check_auth("anything", "anything") is True
