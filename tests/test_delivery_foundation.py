"""Focused regression coverage for delivery lifecycle and retry foundations."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.client import TicketCreationOutcomeUnknown, TicketCreationRejected
from hookwise.extensions import db
from hookwise.models import (
    DeliveryOutbox,
    EndpointTag,
    EventAnnotation,
    SavedHistorySearch,
    User,
    UserPreference,
    WebhookConfig,
    WebhookLog,
    WebhookRetryAttempt,
)
from hookwise.services.delivery_queue import commit_and_dispatch, stage_delivery
from hookwise.tasks import process_webhook_task


@pytest.fixture
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


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


@patch("hookwise.tasks.handle_webhook_logic")
def test_permanent_connectwise_rejection_goes_directly_to_dlq(mock_handle, session):
    config = WebhookConfig(name="Endpoint", retry_enabled=True, retry_max_attempts=5)
    session.add(config)
    session.flush()
    log = WebhookLog(config_id=config.id, request_id="req-invalid", payload="{}", status="queued")
    session.add(log)
    session.commit()

    mock_handle.side_effect = TicketCreationRejected(
        "ConnectWise rejected ticket creation (HTTP 400): The field severity is invalid.",
        retryable=False,
    )
    task = MagicMock()
    task.request = SimpleNamespace(retries=0)
    task.max_retries = 5

    process_webhook_task.run.__func__(task, config.id, {}, "req-invalid", log_id=log.id)

    session.refresh(log)
    assert log.status == "dlq"
    assert log.retry_count == 0
    assert log.error_message == (
        "Non-retryable failure: ConnectWise rejected ticket creation (HTTP 400): The field severity is invalid."
    )
    task.retry.assert_not_called()


@patch("hookwise.tasks.handle_webhook_logic")
def test_unknown_ticket_outcome_waits_instead_of_immediately_colliding(mock_handle, session):
    config = WebhookConfig(
        name="Endpoint",
        retry_enabled=True,
        retry_max_attempts=5,
        retry_base_delay_seconds=1,
        retry_max_delay_seconds=300,
    )
    session.add(config)
    session.flush()
    log = WebhookLog(config_id=config.id, request_id="req-unknown", payload="{}", status="queued")
    session.add(log)
    session.commit()

    mock_handle.side_effect = TicketCreationOutcomeUnknown("No response")
    task = MagicMock()
    task.request = SimpleNamespace(retries=0)
    task.max_retries = 5
    task.retry.side_effect = RuntimeError("retry scheduled")

    with pytest.raises(RuntimeError, match="retry scheduled"):
        process_webhook_task.run.__func__(task, config.id, {}, "req-unknown", log_id=log.id)

    assert task.retry.call_args.kwargs["countdown"] == 300


@patch("hookwise.tasks.handle_webhook_logic")
def test_deleted_endpoint_marks_log_and_attempt_skipped(mock_handle, session):
    log = WebhookLog(config_id="deleted-endpoint", request_id="req-deleted", payload="{}", status="queued")
    session.add(log)
    session.commit()
    task = MagicMock()
    task.request = SimpleNamespace(retries=0)
    task.max_retries = 5

    process_webhook_task.run.__func__(task, "deleted-endpoint", {}, "req-deleted", log_id=log.id)

    session.refresh(log)
    attempt = WebhookRetryAttempt.query.filter_by(log_id=log.id).one()
    assert log.status == "skipped"
    assert log.error_type == "endpoint_deleted"
    assert log.completed_at is not None
    assert attempt.status == "skipped"
    assert attempt.completed_at is not None
    mock_handle.assert_not_called()


def test_broker_failure_leaves_a_durable_pending_outbox(session):
    config = WebhookConfig(name="Endpoint")
    session.add(config)
    session.flush()
    log = WebhookLog(config_id=config.id, request_id="req-outbox", payload="{}")
    outbox = stage_delivery(log, {"event": "down"})
    task = MagicMock()
    task.delay.side_effect = ConnectionError("broker unavailable")

    assert commit_and_dispatch(outbox, task) is False

    session.expire_all()
    saved_outbox = DeliveryOutbox.query.one()
    saved_log = WebhookLog.query.one()
    assert saved_outbox.status == "pending"
    assert saved_outbox.attempts == 1
    assert saved_log.status == "enqueue_failed"
    assert saved_log.error_message == "Task broker unavailable; durable outbox will retry."
    assert saved_outbox.last_error == "ConnectionError: broker unavailable"


def test_outbox_dispatch_omits_absent_optional_task_arguments(session):
    config = WebhookConfig(name="Endpoint")
    session.add(config)
    session.flush()
    log = WebhookLog(config_id=config.id, request_id="req-compatible", payload="{}")
    outbox = stage_delivery(log, {"event": "up"})
    task = MagicMock()

    assert commit_and_dispatch(outbox, task) is True
    task.delay.assert_called_once_with(config.id, {"event": "up"}, "req-compatible", log_id=log.id)
