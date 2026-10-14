"""Stale-endpoint alerts must leave a visible trace when ConnectWise refuses them."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_stale_monitor_")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="


@pytest.fixture(scope="module")
def app():
    """Module-wide app on a temporary SQLite file database."""
    from hookwise import create_app
    from hookwise.extensions import db

    app_inst = create_app()
    app_inst.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app_inst.config["WTF_CSRF_ENABLED"] = False
    app_inst.config["TESTING"] = True
    with app_inst.app_context():
        db.create_all()
        yield app_inst
        db.drop_all()

    with app_inst.app_context():
        db.engine.dispose()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass


@pytest.fixture(autouse=True)
def bind_task_app(app):
    """Run Celery tasks against this module's database (see test_tasks_extended)."""
    import hookwise.tasks as tasks_module

    original = tasks_module._app
    tasks_module._app = app
    yield
    tasks_module._app = original


@pytest.fixture(autouse=True)
def clean_db(app):
    """Empty the tables before each test."""
    from hookwise.extensions import db
    from hookwise.models import AuditLog, WebhookConfig, WebhookLog

    with app.app_context():
        db.session.query(WebhookLog).delete()
        db.session.query(AuditLog).delete()
        db.session.query(WebhookConfig).delete()
        db.session.commit()


@pytest.fixture
def stale_config(app):
    """Factory for an endpoint that is 61 minutes into a one-hour timeout."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig

    def make(**overrides):
        """Create and commit an endpoint with the given overrides."""
        values = {
            "name": "ACME - NAS Monitoring",
            "timeout_alerts_enabled": True,
            "is_enabled": True,
            "is_draft": False,
            "timeout_hours": 1,
            "last_seen_at": datetime.now(timezone.utc) - timedelta(minutes=61),
            "board": "Monitoring Basic",
            "status": "New",
        }
        values.update(overrides)
        config = WebhookConfig(**values)
        db.session.add(config)
        db.session.commit()
        return config

    return make


def _run_check(config=None):
    """Run the task; it uses its own session, so reload ``config`` afterwards."""
    from hookwise.extensions import db
    from hookwise.tasks import check_webhook_timeouts

    summary = check_webhook_timeouts()
    if config is not None:
        db.session.refresh(config)
    return summary


def test_rejected_create_is_recorded_in_history_audit_and_endpoint(app, stale_config, monkeypatch):
    """A rejected stale ticket shows up in history, audit, the endpoint and the run summary."""
    from hookwise.client import TicketCreationRejected
    from hookwise.models import AuditLog, WebhookLog

    monkeypatch.setenv("CW_DEFAULT_COMPANY_ID", "ConnectWise_Company_ID_For_Tickets")
    with app.app_context():
        config = stale_config(customer_id_default="")
        with patch("hookwise.tasks.cw_client") as mock_cw, patch("hookwise.tasks.redis_client") as mock_redis:
            mock_cw.create_ticket.side_effect = TicketCreationRejected(
                "ConnectWise rejected ticket creation (HTTP 400): Company not found", retryable=False
            )
            summary = _run_check(config)

        log = WebhookLog.query.filter_by(config_id=config.id).one()
        assert log.status == "failed"
        assert log.action == "create"
        assert log.source_ip == "system"
        assert log.error_type == "TicketCreationRejected"
        assert "HTTP 400" in log.error_message and "Company not found" in log.error_message
        payload = json.loads(log.payload)
        assert payload["company"] == "ConnectWise_Company_ID_For_Tickets"
        assert payload["company_source"] == "CW_DEFAULT_COMPANY_ID"
        assert payload["retryable"] is False
        assert payload["check_run_id"] == summary["run_id"] == log.correlation_id

        assert AuditLog.query.filter_by(action="timeout_error", config_id=config.id).count() == 1
        assert config.stale_check_result == "create_failed"
        assert "HTTP 400" in config.stale_check_error
        assert config.stale_checked_at is not None
        assert config.timeout_ticket_id is None
        assert config.last_stale_alert_at is None

        assert summary["status"] == "partial_failed"
        assert summary["failed"] == 1 and summary["due"] == 1 and summary["created"] == 0
        stored_key, stored_summary = mock_redis.set.call_args.args
        assert stored_key == "hookwise_timeout_check_last_run"
        assert json.loads(stored_summary)["failures"][0]["result"] == "create_failed"


def test_unknown_create_outcome_is_recorded(app, stale_config):
    """An unknown create outcome is recorded as a failed history entry."""
    from hookwise.client import TicketCreationOutcomeUnknown
    from hookwise.models import WebhookLog

    with app.app_context():
        config = stale_config(customer_id_default="ACME")
        with patch("hookwise.tasks.cw_client") as mock_cw, patch("hookwise.tasks.redis_client"):
            mock_cw.create_ticket.side_effect = TicketCreationOutcomeUnknown("no response was received")
            _run_check(config)

        log = WebhookLog.query.filter_by(config_id=config.id).one()
        assert log.status == "failed"
        assert log.error_type == "TicketCreationOutcomeUnknown"
        assert config.stale_check_result == "create_failed"


def test_repeat_note_exception_is_recorded(app, stale_config):
    """An exception while adding the repeat note is recorded, not only logged."""
    from hookwise.models import WebhookLog

    with app.app_context():
        config = stale_config(
            customer_id_default="ACME",
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_stale_alert_at=datetime.now(timezone.utc) - timedelta(hours=2),
            timeout_ticket_id=4711,
        )
        with patch("hookwise.tasks.cw_client") as mock_cw, patch("hookwise.tasks.redis_client"):
            mock_cw.get_ticket.return_value = {"id": 4711, "closedFlag": False}
            mock_cw.add_ticket_note.side_effect = RuntimeError("connection reset")
            summary = _run_check(config)

        log = WebhookLog.query.filter_by(config_id=config.id).one()
        assert log.status == "failed"
        assert log.action == "update"
        assert log.ticket_id == 4711
        assert "connection reset" in log.error_message
        assert config.stale_check_result == "repeat_failed"
        assert summary["failed"] == 1


def test_live_notice_failure_keeps_created_ticket(app, stale_config):
    """A broken live notification does not roll back a ticket that already exists."""
    from hookwise.models import WebhookLog

    with app.app_context():
        config = stale_config(customer_id_default="ACME")
        with (
            patch("hookwise.tasks.cw_client") as mock_cw,
            patch("hookwise.tasks.redis_client"),
            patch("hookwise.utils.log_to_web", side_effect=RuntimeError("websocket down")),
        ):
            mock_cw.create_ticket.return_value = {"id": 9001}
            summary = _run_check(config)

        assert config.timeout_ticket_id == 9001
        assert config.stale_check_result == "alert_created"
        log = WebhookLog.query.filter_by(config_id=config.id).one()
        assert log.status == "processed" and log.ticket_id == 9001
        assert summary["status"] == "ok" and summary["created"] == 1


def test_alert_fires_exactly_at_threshold(app, stale_config):
    """The alert fires when the elapsed time equals the timeout."""
    import hookwise.tasks as tasks_module

    fixed_now = datetime(2026, 10, 12, 15, 49, 51, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            """Always return the frozen time."""
            return fixed_now

    with app.app_context():
        stale_config(customer_id_default="ACME", last_seen_at=fixed_now - timedelta(hours=1))
        with (
            patch("hookwise.tasks.cw_client") as mock_cw,
            patch("hookwise.tasks.redis_client"),
            patch.object(tasks_module, "datetime", FrozenDatetime),
        ):
            mock_cw.create_ticket.return_value = {"id": 1}
            _run_check()
            mock_cw.create_ticket.assert_called_once()


def test_healthy_endpoint_records_not_due(app, stale_config):
    """A healthy endpoint records 'not_due' and the run reports ok."""
    with app.app_context():
        config = stale_config(last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        with patch("hookwise.tasks.cw_client") as mock_cw, patch("hookwise.tasks.redis_client"):
            summary = _run_check(config)
            mock_cw.create_ticket.assert_not_called()

        assert config.stale_check_result == "not_due"
        assert config.stale_check_error is None
        assert summary == {**summary, "status": "ok", "checked": 1, "due": 0, "failed": 0}


def test_endpoint_deleted_mid_run_does_not_stop_the_others(app, stale_config):
    """A failure that cannot be recorded must not abort the remaining endpoints."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig

    with app.app_context():
        stale_config(name="Deleted during run", customer_id_default="ACME")
        stale_config(name="Still monitored", customer_id_default="ACME")

        deleted: list[str] = []

        def create_ticket(**kwargs):
            """Delete the first endpoint mid-run and fail; succeed for the next one."""
            if not deleted:
                deleted.append(kwargs["monitor_name"])
                # Another request deletes the endpoint while its alert fails, so
                # storing the failure and reloading the endpoint both fail.
                with db.engine.begin() as conn:
                    conn.execute(
                        db.text("DELETE FROM webhook_config WHERE name = :name"), {"name": kwargs["monitor_name"]}
                    )
                raise RuntimeError("ConnectWise unavailable")
            return {"id": 77}

        with patch("hookwise.tasks.cw_client") as mock_cw, patch("hookwise.tasks.redis_client"):
            mock_cw.create_ticket.side_effect = create_ticket
            summary = _run_check()

        assert summary["status"] == "partial_failed"
        assert summary["checked"] == 2 and summary["created"] == 1 and summary["failed"] == 1
        remaining = WebhookConfig.query.one()
        assert remaining.timeout_ticket_id == 77


def test_check_interval_is_five_minutes():
    """Beat runs the stale check every five minutes."""
    from hookwise.tasks import celery

    entry = celery.conf.beat_schedule["check-timeouts"]
    assert entry["task"] == "hookwise.check_webhook_timeouts"
    assert entry["schedule"] == 300.0


@pytest.mark.parametrize(
    ("endpoint_company", "env_company", "problem"),
    [
        ("ACME", "ConnectWise_Company_ID_For_Tickets", None),
        ("", "DEVCO", None),
        ("", "ConnectWise_Company_ID_For_Tickets", "example company"),
        (None, None, "need a ConnectWise company"),
        ("  ", "", "need a ConnectWise company"),
    ],
)
def test_stale_company_problem(monkeypatch, endpoint_company, env_company, problem):
    """The endpoint company wins; a missing or sample company is reported."""
    from hookwise.services.stale_monitor import stale_company_problem

    if env_company is None:
        monkeypatch.delenv("CW_DEFAULT_COMPANY_ID", raising=False)
    else:
        monkeypatch.setenv("CW_DEFAULT_COMPANY_ID", env_company)
    result = stale_company_problem(endpoint_company)
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result


def test_last_run_status_reports_fresh_stale_and_missing_runs():
    """The run summary reads as ok, partial failure, stale or unknown."""
    from hookwise.services.stale_monitor import last_run_status

    now = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)

    class FakeRedis:
        def __init__(self, value):
            """Hold the value that every get() returns."""
            self.value = value

        def get(self, key):
            """Return the stored value for any key."""
            return self.value

    def run(minutes_ago, status="ok"):
        """Fake Redis holding a run summary that finished the given minutes ago."""
        finished = (now - timedelta(minutes=minutes_ago)).isoformat()
        return FakeRedis(json.dumps({"finished_at": finished, "status": status, "checked": 3, "failed": 0}).encode())

    assert last_run_status(run(4), now)["state"] == "ok"
    assert last_run_status(run(4, "partial_failed"), now)["state"] == "partial_failed"
    assert last_run_status(run(16), now)["state"] == "stale"
    assert last_run_status(FakeRedis(None), now)["state"] == "unknown"
    assert last_run_status(FakeRedis(b"not json"), now)["state"] == "unknown"


def _authenticate(client):
    """Mark the test client's session as signed in."""
    with client.session_transaction() as sess:
        sess["user_id"] = "test_user"
        sess["username"] = "testuser"
        sess["role"] = "admin"


def test_saving_stale_alerts_without_usable_company_is_refused(app, monkeypatch):
    """Saving stale alerts without a real company is refused; drafts and real companies save."""
    from hookwise.extensions import db
    from hookwise.models import WebhookConfig

    monkeypatch.setenv("CW_DEFAULT_COMPANY_ID", "ConnectWise_Company_ID_For_Tickets")
    with app.app_context():
        config = WebhookConfig(name="Editable")
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    client = app.test_client()
    _authenticate(client)
    form = {"name": "Editable", "bearer_auth_enabled": "true", "timeout_alerts_enabled": "true", "timeout_hours": "1"}
    with patch("hookwise.tasks.redis_client"), patch("hookwise.api.redis_client"):
        refused = client.post(f"/endpoint/edit/{config_id}", data={**form, "customer_id_default": ""})
        draft = client.post(f"/endpoint/edit/{config_id}", data={**form, "customer_id_default": "", "is_draft": "true"})
        accepted = client.post(f"/endpoint/edit/{config_id}", data={**form, "customer_id_default": "ACME"})

    assert refused.status_code == 400
    assert b"example company" in refused.data
    assert draft.status_code == 302
    assert accepted.status_code == 302
    with app.app_context():
        saved = db.session.get(WebhookConfig, config_id)
        assert saved is not None
        assert saved.timeout_alerts_enabled is True
        assert saved.customer_id_default == "ACME"


def test_health_services_reports_stale_monitor(app):
    """/health/services reports the stale check, a partial failure as a warning."""
    client = app.test_client()
    _authenticate(client)
    finished = datetime.now(timezone.utc).isoformat()
    summary = json.dumps({"finished_at": finished, "status": "partial_failed", "checked": 2, "failed": 1})
    with (
        patch("hookwise.api.redis_client") as mock_api_redis,
        patch("hookwise.tasks.redis_client"),
        patch("hookwise.tasks.celery.control.inspect"),
    ):
        mock_api_redis.get.return_value = summary.encode()
        response = client.get("/health/services")

    assert response.status_code == 200
    assert response.json["stale_monitor"] == "warning"
    assert response.json["stale_monitor_detail"]["failed"] == 1
