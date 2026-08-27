"""Focused regression coverage for delivery lifecycle and retry foundations."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import (
    EndpointTag,
    EventAnnotation,
    SavedHistorySearch,
    User,
    UserPreference,
    WebhookConfig,
    WebhookLog,
    WebhookRetryAttempt,
)
from hookwise.tasks import process_webhook_task


@pytest.fixture
def app():
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    return application


@pytest.fixture
def session(app):
    with app.app_context():
        db.create_all()
        yield db.session
        db.session.remove()
        db.drop_all()


def test_delivery_models_preserve_safe_metadata(session):
    user = User(username="operator", password_hash="hash")
    config = WebhookConfig(name="Endpoint", bearer_token_last4="abcd")
    session.add_all([user, config])
    session.flush()
    log = WebhookLog(
        config_id=config.id,
        request_id="req-1",
        correlation_id="corr-1",
        payload="{}",
        status="queued",
    )
    tag = EndpointTag(name="production")
    session.add_all([log, tag])
    session.flush()
    config.tags.append(tag)
    session.add_all(
        [
            UserPreference(user_id=user.id, dashboard_layout='["events"]'),
            SavedHistorySearch(user_id=user.id, name="Failures", filters='{"status":"failed"}'),
            EventAnnotation(user_id=user.id, log_id=log.id, text="Investigating", is_pinned=True),
        ]
    )
    session.commit()

    assert config.to_dict()["bearer_token_last4"] == "abcd"
    assert config.tags[0].name == "production"
    assert log.to_dict()["correlation_id"] == "corr-1"
    assert UserPreference.query.filter_by(user_id=user.id).one().dashboard_layout == '["events"]'
    assert SavedHistorySearch.query.filter_by(user_id=user.id).one().name == "Failures"
    assert EventAnnotation.query.filter_by(log_id=log.id).one().is_pinned is True


@patch("hookwise.tasks.handle_webhook_logic")
def test_final_retry_moves_stable_log_to_dlq_and_records_sanitized_attempt(mock_handle, session):
    config = WebhookConfig(name="Endpoint", retry_enabled=True, retry_max_attempts=1)
    session.add(config)
    session.flush()
    log = WebhookLog(config_id=config.id, request_id="req-1", payload="{}", status="queued")
    session.add(log)
    session.commit()

    mock_handle.side_effect = RuntimeError("token=should-not-be-stored")
    task = MagicMock()
    task.request = SimpleNamespace(retries=0)
    task.max_retries = 5

    process_webhook_task.run.__func__(task, config.id, {}, "req-1", log_id=log.id)

    session.refresh(log)
    attempt = WebhookRetryAttempt.query.filter_by(log_id=log.id).one()
    assert log.status == "dlq"
    assert log.retry_exhausted_at is not None
    assert log.completed_at is not None
    assert "should-not-be-stored" not in (log.error_message or "")
    assert attempt.status == "dlq"
    assert attempt.error_message == "token=***"
