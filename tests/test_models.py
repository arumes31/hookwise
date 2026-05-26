from datetime import datetime

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import AuditLog, GlobalMapping, User, WebhookConfig, WebhookLog


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app

@pytest.fixture
def db_session(app):
    with app.app_context():
        db.create_all()
        yield db.session
        db.session.remove()
        db.drop_all()

def test_user_creation(db_session):
    user = User(
        username="testuser",
        password_hash="hashed_password",
        role="user"
    )
    db_session.add(user)
    db_session.commit()

    assert user.id is not None
    assert len(user.id) == 36
    assert user.username == "testuser"
    assert user.password_hash == "hashed_password"
    assert user.role == "user"
    assert user.is_2fa_enabled is False
    assert isinstance(user.created_at, datetime)

def test_user_to_dict(db_session):
    user = User(
        username="testuser",
        role="admin"
    )
    db_session.add(user)
    db_session.commit()

    d = user.to_dict()
    assert d["id"] == user.id
    assert d["username"] == "testuser"
    assert d["role"] == "admin"
    assert "created_at" in d

def test_webhook_config_creation(db_session):
    config = WebhookConfig(
        name="Test Config",
        customer_id_default="CUST1"
    )
    db_session.add(config)
    db_session.commit()

    assert config.id is not None
    assert config.name == "Test Config"
    assert config.customer_id_default == "CUST1"
    assert config.bearer_token is not None
    assert config.trigger_field == "heartbeat.status"
    assert config.is_enabled is True
    assert config.is_pinned is False
    assert config.ai_rca_enabled is False
    assert isinstance(config.created_at, datetime)

def test_webhook_config_to_dict(db_session):
    config = WebhookConfig(name="Test Config")
    db_session.add(config)
    db_session.commit()

    d = config.to_dict()
    assert d["id"] == config.id
    assert d["name"] == "Test Config"
    assert "bearer_token" not in d

    d_with_token = config.to_dict(include_token=True)
    assert d_with_token["bearer_token"] == config.bearer_token

def test_webhook_log_creation(db_session):
    config = WebhookConfig(name="Test Config")
    db_session.add(config)
    db_session.commit()

    log = WebhookLog(
        config_id=config.id,
        request_id="req-123",
        payload='{"test": "data"}',
        status="processed",
        action="create",
        ticket_id=12345
    )
    db_session.add(log)
    db_session.commit()

    assert log.id is not None
    assert log.config_id == config.id
    assert log.request_id == "req-123"
    assert log.status == "processed"
    assert log.action == "create"
    assert log.ticket_id == 12345
    assert log.config.name == "Test Config"

def test_webhook_log_to_dict(db_session):
    config = WebhookConfig(name="Test Config")
    db_session.add(config)
    db_session.commit()

    log = WebhookLog(
        config_id=config.id,
        request_id="req-123",
        payload='{"test": "data"}',
        status="processed"
    )
    db_session.add(log)
    db_session.commit()

    d = log.to_dict()
    assert d["id"] == log.id
    assert d["config_id"] == config.id
    assert d["config_name"] == "Test Config"
    assert d["payload"] == '{"test": "data"}'

def test_audit_log_creation(db_session):
    log = AuditLog(
        action="update",
        user="admin",
        details="Updated config"
    )
    db_session.add(log)
    db_session.commit()

    assert log.id is not None
    assert log.action == "update"
    assert log.user == "admin"
    assert log.details == "Updated config"
    assert isinstance(log.created_at, datetime)

def test_audit_log_to_dict(db_session):
    log = AuditLog(action="delete", user="admin")
    db_session.add(log)
    db_session.commit()

    d = log.to_dict()
    assert d["id"] == log.id
    assert d["action"] == "delete"
    assert d["user"] == "admin"

def test_global_mapping_creation(db_session):
    mapping = GlobalMapping(
        tenant_value="tenant1",
        company_id="COMP1",
        description="Test Mapping"
    )
    db_session.add(mapping)
    db_session.commit()

    assert mapping.id is not None
    assert mapping.tenant_value == "tenant1"
    assert mapping.company_id == "COMP1"
    assert mapping.description == "Test Mapping"
    assert isinstance(mapping.created_at, datetime)
    assert isinstance(mapping.updated_at, datetime)

def test_global_mapping_to_dict(db_session):
    mapping = GlobalMapping(tenant_value="tenant2", company_id="COMP2")
    db_session.add(mapping)
    db_session.commit()

    d = mapping.to_dict()
    assert d["id"] == mapping.id
    assert d["tenant_value"] == "tenant2"
    assert d["company_id"] == "COMP2"
