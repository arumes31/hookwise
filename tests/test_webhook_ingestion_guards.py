"""Ingestion-path guard tests for the /w/<config_id> endpoint.

Covers the rejection paths that protect the worker and ConnectWise:

* invalid Bearer token         -> 401
* invalid HMAC signature       -> 401
* source IP not in allowlist   -> 403
* body over MAX_CONTENT_LENGTH -> 413 (JSON envelope for /w/ paths)
"""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog


@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as m:
        m.get.return_value = None
        yield m


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


def _make_config(app, **kw):
    with app.app_context():
        cfg = WebhookConfig(name="Ingest", bearer_token="plain-token")
        cfg.is_enabled = True
        cfg.bearer_auth_enabled = False
        for k, v in kw.items():
            setattr(cfg, k, v)
        db.session.add(cfg)
        db.session.commit()
        return cfg.id


def test_invalid_bearer_token_rejected(client, app):
    cid = _make_config(app, bearer_auth_enabled=True)
    resp = client.post(f"/w/{cid}", json={"x": 1}, headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.get_json()["status"] == "error"


def test_invalid_hmac_signature_rejected(client, app):
    cid = _make_config(app, hmac_secret="shhh")
    resp = client.post(f"/w/{cid}", json={"x": 1}, headers={"X-HookWise-Signature": "deadbeef"})
    assert resp.status_code == 401


def test_valid_hmac_passes_signature_check(client, app):
    # Sanity check the negative test above: a correct signature must NOT 401.
    cid = _make_config(app, hmac_secret="shhh")
    body = b'{"x": 1}'
    sig = hmac.HMAC(b"shhh", body, hashlib.sha256).hexdigest()
    with patch("hookwise.webhook.process_webhook_task") as task:
        resp = client.post(
            f"/w/{cid}",
            data=body,
            content_type="application/json",
            headers={"X-HookWise-Signature": sig},
        )
    assert resp.status_code == 202
    task.delay.assert_called_once()


def test_source_ip_not_in_allowlist_rejected(client, app):
    # Test client presents 127.0.0.1, which is outside 10.0.0.0/8.
    cid = _make_config(app, trusted_ips="10.0.0.0/8")
    resp = client.post(f"/w/{cid}", json={"x": 1})
    assert resp.status_code == 403
    with app.app_context():
        # The rejection is recorded for the audit trail.
        assert WebhookLog.query.filter_by(config_id=cid, status="failed").count() == 1


def test_oversized_payload_rejected(client, app):
    cid = _make_config(app)
    client.application.config["MAX_CONTENT_LENGTH"] = 1024  # 1 KB cap for the test
    big = "x" * 5000
    resp = client.post(f"/w/{cid}", data='{"blob": "' + big + '"}', content_type="application/json")
    assert resp.status_code == 413
    assert resp.get_json()["message"] == "Payload too large"
